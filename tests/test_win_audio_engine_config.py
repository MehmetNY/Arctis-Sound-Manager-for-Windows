"""``win_audio_engine.json``: parsing, resolving device names, and the first-run guess."""
from __future__ import annotations

import json

import pytest

from arctis_sound_manager.win.audio.backend import DeviceInfo
from arctis_sound_manager.win.audio.tap import tap_devices
from arctis_sound_manager.win.audio.engine_config import (PROFILES, EngineConfig, is_virtual, load_config, resolve, save_config,
                                                          suggest_config)


def dev(index, name, ins=0, outs=0, host="Windows WASAPI", default_in=False, default_out=False):
    return DeviceInfo(index, name, host, ins, outs, 48000.0, default_in, default_out)


# ── the file ─────────────────────────────────────────────────────────────────

def test_defaults_do_nothing_until_devices_are_chosen():
    cfg = EngineConfig()
    assert not cfg.playback_enabled and cfg.mic_enabled and cfg.schema == 3          # the microphone effect needs no device
    assert cfg.latency == "balanced" and cfg.playback_block == 960 and cfg.microphone_block == 480
    assert all(bus == "headset" for bus in cfg.routing.values()) and cfg.master_limiter


def test_round_trip_through_json(tmp_path):
    cfg = EngineConfig(latency="low", block=1440, mic_block=960, device_latency=0.02,
                       inputs={"game": "ASM Tap game", "chat": "ASM Tap chat", "media": "", "aux": ""},
                       outputs={"headset": "Arctis", "stream": "Second headset"},
                       routing={"game": "headset", "chat": "stream", "media": "headset", "aux": "headset"},
                       mic_fx=False, master_limiter=False)
    path = save_config(cfg, tmp_path)
    assert path.name == "win_audio_engine.json" and not list(tmp_path.glob("*.tmp"))
    again = load_config(tmp_path)
    assert again == cfg
    assert again.playback_block == 1440 and again.microphone_block == 960 and again.playback_enabled and not again.mic_enabled
    assert json.loads(path.read_text())["mic"] == {"fx": False}


def test_a_missing_file_gives_defaults(tmp_path):
    assert load_config(tmp_path) == EngineConfig()


@pytest.mark.parametrize("junk", ["not json at all", "[]", "42", "null", '{"latency": 7}', '{"inputs": "x"}'])
def test_junk_never_raises_and_never_changes_the_defaults(tmp_path, junk):
    (tmp_path / "win_audio_engine.json").write_text(junk)
    assert load_config(tmp_path) == EngineConfig(schema=1)                        # no mark: it is an older build's file


def test_bad_values_are_dropped_one_by_one():
    cfg = EngineConfig.from_dict({
        "latency": "turbo", "block": 500, "mic_block": True, "device_latency": "slow",
        "inputs": {"game": " ASM Tap game ", "chat": 3, "bogus": "x"},
        "outputs": {"headset": "Arctis", "speakers": "Realtek"},
        "routing": {"game": "nowhere", "chat": "stream"},
        "mic": {"fx": "yes"},
        "master_limiter": "yes"})
    assert cfg.latency == "balanced" and cfg.block is None and cfg.mic_block is None and cfg.device_latency == "low"
    assert cfg.inputs["game"] == "ASM Tap game" and cfg.inputs["chat"] == "" and "bogus" not in cfg.inputs
    assert cfg.outputs == {"headset": "Arctis"}
    assert cfg.routing["game"] == "headset" and cfg.routing["chat"] == "stream"
    assert cfg.mic_fx is True and cfg.master_limiter is True


def test_the_microphone_of_an_older_build_named_devices_and_is_forgotten():
    cfg = EngineConfig.from_dict({"schema": 2, "mic": {"input": "Microphone (Arctis", "output": "CABLE-C Input"},
                                  "inputs": {"game": "ASM Tap game"}, "outputs": {"headset": "Arctis"}})
    assert cfg.schema == 2 and cfg.mic_fx is True and not hasattr(cfg, "mic_output")
    assert "CABLE" not in json.dumps(cfg.to_dict())                                   # nothing of the cable survives a save


def test_block_sizes_must_be_multiples_of_the_rnnoise_frame():
    assert EngineConfig.from_dict({"block": 960}).block == 960
    for bad in (0, 240, 481, 100000, -480, 960.0):
        assert EngineConfig.from_dict({"block": bad}).block is None


def test_profiles_trade_latency_for_safety():
    low, balanced, safe = (PROFILES[k] for k in ("low", "balanced", "safe"))
    assert low.block < balanced.block < safe.block
    assert low.approx_latency_ms() < balanced.approx_latency_ms() < safe.approx_latency_ms()
    assert all(p.block % 480 == 0 and p.mic_block % 480 == 0 for p in PROFILES.values())
    assert EngineConfig(latency="low").playback_block == 480
    assert EngineConfig(latency="low", block=1920).playback_block == 1920          # an override wins


def test_problems_are_spelled_out():
    assert EngineConfig().problems() == []                                      # the microphone alone is a set-up
    assert EngineConfig(mic_fx=False).problems() == ["no headset output device is chosen",
                                          "no source is assigned to any channel (the audio helper asm-audiotap.exe is missing)"]
    ok = EngineConfig(inputs={"game": "A", "chat": "B", "media": "", "aux": ""}, outputs={"headset": "H"})
    assert ok.problems() == []
    ok.routing["chat"] = "stream"
    assert any("routed to 'stream'" in p for p in ok.problems())
    ok.routing["chat"] = "headset"
    ok.inputs["media"] = "A"
    assert any("two channels" in p for p in ok.problems())


def test_a_microphone_alone_is_a_valid_setup():
    cfg = EngineConfig(inputs={ch: "" for ch in ("game", "chat", "media", "aux")}, outputs={})
    assert cfg.mic_enabled and not cfg.playback_enabled and cfg.problems() == []
    cfg.outputs = {"headset": "Arctis"}                                         # a headset without any channel is still unfinished
    assert len(cfg.problems()) == 1 and "channel" in cfg.problems()[0]
    cfg = EngineConfig(inputs={ch: "" for ch in ("game", "chat", "media", "aux")}, outputs={}, mic_fx=False)
    assert len(cfg.problems()) == 2                                             # nothing at all: still reported


# ── resolving names ──────────────────────────────────────────────────────────

def test_resolve_finds_devices_and_lists_what_is_missing():
    devices = [*tap_devices(), dev(5, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2)]
    cfg = EngineConfig(inputs={"game": "ASM Tap game", "chat": "CABLE-B Output", "media": "", "aux": ""},
                       outputs={"headset": "Arctis", "stream": "nothing here"})
    r = resolve(cfg, devices)
    assert "game" in r.inputs and "chat" not in r.inputs
    assert r.outputs["headset"].index == 5 and "stream" not in r.outputs
    assert len(r.missing) == 2 and "chat" in r.missing[0] and "stream" in r.missing[1]


# ── the first-run guess ──────────────────────────────────────────────────────

def machine(extra=()):
    return [*tap_devices(),
            dev(40, "Kulaklıklar (Arctis Nova Pro Wireless)", outs=2),
            dev(41, "Mikrofon (Arctis Nova Pro Wireless)", ins=1, default_in=True),
            dev(42, "Speakers (Realtek(R) Audio)", outs=2, default_out=True),
            dev(43, "SteelSeries Sonar - Gaming (SteelSeries Sonar Virtual Audio Device)", outs=2), *extra]


def test_the_guess_is_the_taps_four_channels_and_the_real_headset():
    cfg = suggest_config(machine())
    assert [cfg.inputs[c] for c in ("game", "chat", "media", "aux")] == [f"ASM Tap {c}" for c in ("game", "chat", "media", "aux")]
    assert cfg.outputs["headset"] == "Kulaklıklar (Arctis Nova Pro Wireless)"
    assert cfg.mic_fx and cfg.problems() == []


def test_a_virtual_cable_on_the_pc_is_never_used_or_offered():
    cable = [dev(10, "CABLE Output (VB-Audio Virtual Cable)", ins=2), dev(11, "CABLE Input (VB-Audio Virtual Cable)", outs=2),
             dev(12, "CABLE In 16ch (VB-Audio Virtual Cable)", outs=2)]
    cfg = suggest_config(cable + machine())
    assert cfg.outputs["headset"] == "Kulaklıklar (Arctis Nova Pro Wireless)"
    assert "CABLE" not in json.dumps(cfg.to_dict())
    assert suggest_config([d for d in machine() if "Arctis" not in d.name] + cable).outputs["headset"] == "Speakers (Realtek(R) Audio)"


def test_without_the_audio_helper_there_are_no_channels_and_the_problem_says_so():
    cfg = suggest_config([d for d in machine() if not d.name.startswith("ASM Tap")])
    assert cfg.inputs == EngineConfig().inputs
    assert any("asm-audiotap.exe" in p for p in cfg.problems())


def test_sonar_virtual_devices_are_not_mistaken_for_the_headset():
    devices = [dev(1, "SteelSeries Sonar - Gaming (SteelSeries Sonar Virtual Audio Device)", outs=2),
               dev(2, "Speakers (Realtek(R) Audio)", outs=2, default_out=True)]
    assert suggest_config(devices).outputs["headset"] == "Speakers (Realtek(R) Audio)"


def test_no_devices_no_guess():
    assert suggest_config([]) == EngineConfig()


def test_non_wasapi_listings_are_used_only_when_there_is_nothing_else():
    mme = [dev(1, "Headphones (Arctis Nova Pro Wireless)", outs=2, host="MME")]
    assert suggest_config(mme).outputs["headset"] == "Headphones (Arctis Nova Pro Wireless)"
    both = [*mme, dev(2, "Kulaklıklar (Arctis Nova Pro Wireless)", outs=2)]
    assert suggest_config(both).outputs["headset"] == "Kulaklıklar (Arctis Nova Pro Wireless)"


def test_virtual_means_somebody_elses_audio_software():
    for name in ("CABLE Input (VB-Audio Virtual Cable)", "Voicemeeter Input", "SteelSeries Sonar - Chat", "Virtual Speakers"):
        assert is_virtual(name)
    for name in ("Kulaklıklar (Arctis Nova Pro Wireless)", "Speakers (Realtek(R) Audio)", "ASM Tap game".replace("ASM Tap", "Headset")):
        assert not is_virtual(name)
