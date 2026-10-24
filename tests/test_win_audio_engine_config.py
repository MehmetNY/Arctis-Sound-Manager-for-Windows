"""``win_audio_engine.json``: parsing, resolving device names, and the first-run guess."""
from __future__ import annotations

import json

import pytest

from arctis_sound_manager.win.audio.backend import DeviceInfo
from arctis_sound_manager.win.audio.engine_config import (PROFILES, EngineConfig, load_config, resolve, save_config,
                                                          suggest_config)


def dev(index, name, ins=0, outs=0, host="Windows WASAPI", default_in=False, default_out=False):
    return DeviceInfo(index, name, host, ins, outs, 48000.0, default_in, default_out)


def cable_pair(i, label, suffix):
    """A cable's capture end ('... Output') and render end ('... Input')."""
    return [dev(i, f"{label} Output ({suffix})", ins=2), dev(i + 1, f"{label} Input ({suffix})", outs=2)]


# ── the file ─────────────────────────────────────────────────────────────────

def test_defaults_do_nothing_until_devices_are_chosen():
    cfg = EngineConfig()
    assert not cfg.playback_enabled and not cfg.mic_enabled
    assert cfg.latency == "balanced" and cfg.playback_block == 960 and cfg.microphone_block == 480
    assert all(bus == "headset" for bus in cfg.routing.values()) and cfg.master_limiter


def test_round_trip_through_json(tmp_path):
    cfg = EngineConfig(latency="low", block=1440, mic_block=960, device_latency=0.02,
                       inputs={"game": "CABLE-A Output", "chat": "CABLE-B Output", "media": "", "aux": ""},
                       outputs={"headset": "Arctis", "stream": "CABLE-D Input"},
                       routing={"game": "headset", "chat": "stream", "media": "headset", "aux": "headset"},
                       mic_input="Microphone (Arctis", mic_output="CABLE-C Input", master_limiter=False)
    path = save_config(cfg, tmp_path)
    assert path.name == "win_audio_engine.json" and not list(tmp_path.glob("*.tmp"))
    again = load_config(tmp_path)
    assert again == cfg
    assert again.playback_block == 1440 and again.microphone_block == 960 and again.playback_enabled and again.mic_enabled


def test_a_missing_file_gives_defaults(tmp_path):
    assert load_config(tmp_path) == EngineConfig()


@pytest.mark.parametrize("junk", ["not json at all", "[]", "42", "null", '{"latency": 7}', '{"inputs": "x"}'])
def test_junk_never_raises_and_never_changes_the_defaults(tmp_path, junk):
    (tmp_path / "win_audio_engine.json").write_text(junk)
    assert load_config(tmp_path) == EngineConfig()


def test_bad_values_are_dropped_one_by_one():
    cfg = EngineConfig.from_dict({
        "latency": "turbo", "block": 500, "mic_block": True, "device_latency": "slow",
        "inputs": {"game": " CABLE-A Output ", "chat": 3, "bogus": "x"},
        "outputs": {"headset": "Arctis", "speakers": "Realtek"},
        "routing": {"game": "nowhere", "chat": "stream"},
        "mic": {"input": "mic", "output": None},
        "master_limiter": "yes"})
    assert cfg.latency == "balanced" and cfg.block is None and cfg.mic_block is None and cfg.device_latency == "low"
    assert cfg.inputs["game"] == "CABLE-A Output" and cfg.inputs["chat"] == "" and "bogus" not in cfg.inputs
    assert cfg.outputs == {"headset": "Arctis"}
    assert cfg.routing["game"] == "headset" and cfg.routing["chat"] == "stream"
    assert cfg.mic_input == "mic" and cfg.mic_output == "" and cfg.master_limiter is True


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
    assert EngineConfig().problems() == ["no headset output device is chosen",
                                          "no virtual cable is assigned to any channel"]
    ok = EngineConfig(inputs={"game": "A", "chat": "B", "media": "", "aux": ""}, outputs={"headset": "H"})
    assert ok.problems() == []
    ok.routing["chat"] = "stream"
    assert any("routed to 'stream'" in p for p in ok.problems())
    ok.routing["chat"] = "headset"
    ok.inputs["media"] = "A"
    assert any("two channels" in p for p in ok.problems())
    ok.inputs["media"] = ""
    ok.mic_input = "mic"
    assert any("no virtual cable to send it to" in p for p in ok.problems())


def test_playing_into_a_cable_the_engine_reads_would_loop():
    cfg = EngineConfig(inputs={"game": "CABLE-A Output (VB-Audio Cable A)", "chat": "CABLE-B Output", "media": "", "aux": ""},
                       outputs={"headset": "Headset Earphone"})
    assert cfg.problems() == []
    cfg.mic_output = "CABLE-A Input (VB-Audio Cable A)"                       # the other end of the game channel's cable
    assert cfg.problems() == ["the processed microphone plays into the cable that the game channel reads from, "
                              "so the sound would loop"]
    cfg.mic_output = "CABLE-C Input"
    cfg.outputs["stream"] = "CABLE-B Input (VB-Audio Cable B)"
    assert cfg.problems() == ["the stream mix plays into the cable that the chat channel reads from, so the sound would loop"]
    cfg.outputs["stream"] = "CABLE-C Input (VB-Audio Cable C)"
    assert cfg.problems() == ["the processed microphone and the stream mix use the same device"]
    cfg.outputs["stream"] = "CABLE-D Input"
    assert cfg.problems() == []
    cfg.outputs["headset"] = "CABLE-B Input"
    assert len(cfg.problems()) == 1 and "headset" in cfg.problems()[0]


def test_three_things_on_one_device_are_each_reported_against_the_first():
    cfg = EngineConfig(inputs={"game": "CABLE-A Output", "chat": "", "media": "", "aux": ""},
                       outputs={"headset": "CABLE-D Input", "stream": "CABLE-D Input (VB-Audio Cable D)"},
                       mic_output="CABLE-D Input")
    assert cfg.problems() == ["the stream mix and the headset use the same device",
                              "the processed microphone and the headset use the same device"]


def test_unnamed_endpoints_are_not_mistaken_for_loops():
    cfg = EngineConfig(inputs={"game": "", "chat": "", "media": "CABLE Output (VB-Audio Virtual Cable)", "aux": ""},
                       outputs={"headset": "Headset Earphone"}, mic_output="CABLE-A Input (VB-Audio Cable A)")
    assert cfg.problems() == []                                                # 'CABLE' is not 'CABLE-A'
    cfg.mic_input = ""
    cfg.outputs["stream"] = ""
    assert cfg.problems() == []


# ── resolving names ──────────────────────────────────────────────────────────

def test_resolve_finds_devices_and_lists_what_is_missing():
    devices = [*cable_pair(1, "CABLE-A", "VB-Audio Cable A"), dev(5, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2),
               dev(6, "Headset Microphone (Arctis Nova Pro Wireless)", ins=1)]
    cfg = EngineConfig(inputs={"game": "CABLE-A Output", "chat": "CABLE-B Output", "media": "", "aux": ""},
                       outputs={"headset": "Arctis", "stream": "nothing here"},
                       mic_input="Microphone (Arctis", mic_output="CABLE-A Input")
    r = resolve(cfg, devices)
    assert r.inputs["game"].index == 1 and "chat" not in r.inputs
    assert r.outputs["headset"].index == 5 and "stream" not in r.outputs
    assert r.mic_input.index == 6 and r.mic_output.index == 2
    assert len(r.missing) == 2 and "chat" in r.missing[0] and "stream" in r.missing[1]


def test_resolve_ignores_a_mic_with_only_half_configured():
    r = resolve(EngineConfig(mic_input="mic"), [dev(1, "Some Mic", ins=1)])
    assert r.mic_input is None and r.missing == []


# ── the first-run guess ──────────────────────────────────────────────────────

def five_cables():
    devices = [*cable_pair(10, "CABLE", "VB-Audio Virtual Cable")]
    for i, letter in enumerate("ABCD"):
        devices += cable_pair(20 + 2 * i, f"CABLE-{letter}", f"VB-Audio Cable {letter}")
    devices += [dev(40, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2),
                dev(41, "Headset Microphone (Arctis Nova Pro Wireless)", ins=1, default_in=True),
                dev(42, "Speakers (Realtek(R) Audio)", outs=2, default_out=True),
                dev(43, "SteelSeries Sonar - Gaming (SteelSeries Sonar Virtual Audio Device)", outs=2)]
    return devices


def test_five_cables_become_four_channels_and_a_microphone_output():
    cfg = suggest_config(five_cables())
    assert cfg.inputs == {"game": "CABLE Output (VB-Audio Virtual Cable)", "chat": "CABLE-A Output (VB-Audio Cable A)",
                          "media": "CABLE-B Output (VB-Audio Cable B)", "aux": "CABLE-C Output (VB-Audio Cable C)"}
    assert cfg.mic_output == "CABLE-D Input (VB-Audio Cable D)"
    assert cfg.outputs["headset"] == "Headset Earphone (Arctis Nova Pro Wireless)"
    assert cfg.mic_input == "Headset Microphone (Arctis Nova Pro Wireless)"
    assert cfg.problems() == []


def test_a_sixth_cable_takes_the_stream_mix_and_never_the_microphones_cable():
    devices = five_cables() + cable_pair(60, "CABLE-E", "VB-Audio Cable E")
    cfg = suggest_config(devices)
    assert cfg.mic_output == "CABLE-D Input (VB-Audio Cable D)"                  # unchanged by the extra cable
    assert cfg.outputs["stream"] == "CABLE-E Input (VB-Audio Cable E)"
    assert cfg.inputs["aux"] == "CABLE-C Output (VB-Audio Cable C)"
    assert cfg.problems() == []


def test_without_a_spare_cable_no_stream_output_is_suggested():
    for devices in (five_cables(), [d for d in five_cables() if "CABLE-D" not in d.name],
                    [d for d in five_cables() if not any(x in d.name for x in ("CABLE-C", "CABLE-D"))]):
        assert "stream" not in suggest_config(devices).outputs


def test_with_fewer_cables_the_last_one_carries_the_microphone():
    devices = [d for d in five_cables() if not any(x in d.name for x in ("CABLE-C", "CABLE-D"))]
    cfg = suggest_config(devices)
    assert [cfg.inputs[c] for c in ("game", "chat", "media", "aux")] == [
        "CABLE Output (VB-Audio Virtual Cable)", "CABLE-A Output (VB-Audio Cable A)", "", ""]
    assert cfg.mic_output == "CABLE-B Input (VB-Audio Cable B)"


def test_one_cable_serves_a_channel_and_there_is_no_microphone_output():
    devices = [*cable_pair(10, "CABLE", "VB-Audio Virtual Cable"), dev(40, "Speakers (Realtek(R) Audio)", outs=2, default_out=True)]
    cfg = suggest_config(devices)
    assert cfg.inputs["game"].startswith("CABLE Output") and cfg.inputs["chat"] == ""
    assert cfg.mic_output == "" and cfg.mic_input == ""
    assert cfg.outputs["headset"] == "Speakers (Realtek(R) Audio)"                # falls back to the default output


def test_a_cable_with_only_one_end_is_not_a_cable():
    devices = [dev(1, "CABLE Output (VB-Audio Virtual Cable)", ins=2), dev(2, "Speakers", outs=2, default_out=True)]
    assert suggest_config(devices).inputs == EngineConfig().inputs


def test_sonar_virtual_devices_are_not_mistaken_for_the_headset():
    devices = [dev(1, "SteelSeries Sonar - Gaming (SteelSeries Sonar Virtual Audio Device)", outs=2),
               dev(2, "Speakers (Realtek(R) Audio)", outs=2, default_out=True)]
    assert suggest_config(devices).outputs["headset"] == "Speakers (Realtek(R) Audio)"


def test_no_devices_no_guess():
    assert suggest_config([]) == EngineConfig()


def test_non_wasapi_listings_are_used_only_when_there_is_nothing_else():
    mme = [dev(i, d.name, d.max_inputs, d.max_outputs, host="MME") for i, d in enumerate(cable_pair(1, "CABLE", "VB-Audio Virtual Cable"))]
    assert suggest_config(mme).inputs["game"].startswith("CABLE Output")
