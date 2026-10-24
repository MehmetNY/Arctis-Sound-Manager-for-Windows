"""The engine end to end against in-memory devices: no sound card, no real time."""
from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

from arctis_sound_manager.win.audio.backend import DeviceInfo, FakeBackend
from arctis_sound_manager.win.audio.engine_config import EngineConfig
from arctis_sound_manager.win.audio.graph import CHANNELS, chatmix_gain, volume_gain
from arctis_sound_manager.win.audio.runtime import (Engine, EngineState, StateWatcher, to_channels, SnapshotBox)

RATE = 48000
CHUNK = 480


def dev(index, name, ins=0, outs=0):
    return DeviceInfo(index, name, "Windows WASAPI", ins, outs, 48000.0)


DEVICES = [
    dev(1, "CABLE-A Output (VB-Audio Cable A)", ins=2), dev(2, "CABLE-A Input (VB-Audio Cable A)", outs=2),
    dev(3, "CABLE-B Output (VB-Audio Cable B)", ins=2), dev(4, "CABLE-B Input (VB-Audio Cable B)", outs=2),
    dev(5, "CABLE-C Output (VB-Audio Cable C)", ins=2), dev(6, "CABLE-C Input (VB-Audio Cable C)", outs=2),
    dev(7, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2),
    dev(8, "Headset Microphone (Arctis Nova Pro Wireless)", ins=1),
    dev(9, "Speakers (Realtek(R) Audio)", outs=2),
]


def config(**kw):
    cfg = EngineConfig(inputs={"game": "CABLE-A Output", "chat": "CABLE-B Output", "media": "", "aux": ""},
                       outputs={"headset": "Arctis Nova Pro"},
                       mic_input="Microphone (Arctis", mic_output="CABLE-C Input", latency="low")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def write(path, name, data):
    (path / name).write_text(json.dumps(data))


def spatial_off(path):
    for suffix in ("", "_media", "_aux"):
        write(path, f"sonar_spatial_audio{suffix}.json", {"enabled": False})


class Rig:
    """Drives an Engine in simulated time: 10 ms device ticks, then one pass of the engine's loops."""

    def __init__(self, tmp_path, cfg=None, devices=None, probe=None, hotplugged=()):
        self.tmp = tmp_path
        self.backend = FakeBackend(devices or DEVICES)
        self.backend.hotplugged = list(hotplugged)
        self.t = 0
        # the ports measure the devices' pace with a clock: here the rig's own, so a loaded test machine cannot skew it
        self.engine = Engine(cfg or config(), self.backend, config_dir=tmp_path, probe=probe,
                             port_clock=lambda: self.t / RATE)
        self.out = {"headset": [], "mic": []}
        self.sources = {"game": None, "chat": None, "mic": None}      # callables (frame index array) -> (frames, ch)

    def start(self):
        self.engine.start(threaded=False)

    def stream(self, name, kind):
        return self.backend.stream(name, kind)

    def tick(self, ticks=1, deliver=True):
        idx = np.arange(self.t, self.t + CHUNK)
        for _ in range(ticks):
            idx = np.arange(self.t, self.t + CHUNK)
            for name, key in (("CABLE-A Output", "game"), ("CABLE-B Output", "chat"), ("Microphone", "mic")):
                try:
                    s = self.stream(name, "input")
                except KeyError:
                    continue
                if not s.running or not deliver:
                    continue
                src = self.sources[key]
                s.deliver(src(idx, s.channels) if src else np.zeros((CHANNELS_FRAMES, s.channels)))
            for name, key in (("Arctis", "headset"), ("CABLE-C Input", "mic")):
                try:
                    s = self.stream(name, "output")
                except KeyError:
                    continue
                if s.running:
                    self.out[key].append(s.collect(CHUNK))
            self.engine.playback.step()
            self.engine.mic.step()
            self.t += CHUNK

    def heard(self, seconds=0.5, key="headset"):
        data = np.concatenate(self.out[key]) if self.out[key] else np.zeros((0, 2))
        return data[-int(seconds * RATE):]


CHANNELS_FRAMES = CHUNK


def tone(freq=1000.0, amp=0.1):
    def make(idx, channels):
        x = (np.sin(2 * np.pi * freq * idx / RATE) * amp).astype(np.float32)
        return np.repeat(x[:, None], channels, axis=1)
    return make


def level(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


# ── set-up ───────────────────────────────────────────────────────────────────

def test_start_opens_the_devices_and_fills_the_headset_before_the_first_callback(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.start()
    status = rig.engine.status()
    states = {k: v["state"] for k, v in status["endpoints"].items()}
    assert states == {"in:game": "running", "in:chat": "running", "out:headset": "running", "mic:in": "running",
                      "mic:out": "running"}
    assert status["endpoints"]["in:game"]["channels"] == 2 and status["endpoints"]["mic:in"]["channels"] == 1
    headset = rig.engine.playback.master()
    assert headset.fill >= headset.target and rig.engine.mic.master().fill >= rig.engine.mic.master().target
    assert status["running"] and status["playback"]["active"] and status["microphone"]["active"]
    assert status["problems"] == []


def test_a_tone_comes_out_of_the_headset_unchanged_when_nothing_is_switched_on(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.1)
    rig.start()
    rig.tick(150)
    y = rig.heard()
    assert level(y[:, 0]) == pytest.approx(0.1 / np.sqrt(2), rel=0.02) and np.allclose(y[:, 0], y[:, 1])
    ep = rig.engine.status()["endpoints"]
    assert ep["out:headset"]["underruns"] == 0 and ep["in:game"]["underruns"] == 0 and ep["in:game"]["resyncs"] == 0


def test_a_silent_start_is_silent_and_costs_no_underruns(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.tick(100)
    assert level(rig.heard()) == 0.0
    assert rig.engine.status()["endpoints"]["out:headset"]["underruns"] == 0


# ── controls ─────────────────────────────────────────────────────────────────

def test_volume_is_cubic_and_mute_silences(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.2)
    rig.start()
    rig.tick(100)
    full = level(rig.heard(0.3))
    rig.engine.set_volume("game", 50)
    rig.tick(60)
    assert level(rig.heard(0.3)) == pytest.approx(full * volume_gain(50), rel=0.02)
    rig.engine.set_mute("game", True)
    rig.tick(60)
    assert level(rig.heard(0.3)) < 1e-6
    rig.engine.set_mute("game", False)
    rig.tick(60)
    assert level(rig.heard(0.3)) == pytest.approx(full * volume_gain(50), rel=0.02)


def test_chatmix_attenuates_the_chosen_channels_and_chat_separately(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(500, 0.2)
    rig.sources["chat"] = tone(2000, 0.2)
    rig.start()
    rig.tick(100)
    both = rig.heard(0.4)
    rig.engine.set_chatmix(40, 100)                                           # game side pulled down
    rig.tick(80)
    mixed = rig.heard(0.4)
    spec = lambda x, f: abs(np.fft.rfft(x[:, 0] * np.hanning(len(x)))[int(f * len(x) / RATE)])
    assert spec(mixed, 500) / spec(both, 500) == pytest.approx(chatmix_gain(40), rel=0.05)
    assert spec(mixed, 2000) / spec(both, 2000) == pytest.approx(1.0, rel=0.05)
    rig.engine.set_chatmix(100, 40)
    rig.tick(80)
    swapped = rig.heard(0.4)
    assert spec(swapped, 2000) / spec(both, 2000) == pytest.approx(chatmix_gain(40), rel=0.05)
    assert spec(swapped, 500) / spec(both, 500) == pytest.approx(1.0, rel=0.05)


def test_chatmix_channels_decide_who_follows_the_dial(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.start()
    rig.engine.set_chatmix(50, 100, channels=["game", "media", "chat"])        # chat is never "the other side"
    assert rig.engine.chatmix_channels == ["game", "media"]
    strips = rig.engine.graph.strips
    assert strips["game"].controls.chatmix_pct == 50 and strips["media"].controls.chatmix_pct == 50
    assert strips["aux"].controls.chatmix_pct == 100 and strips["chat"].controls.chatmix_pct == 100
    rig.engine.set_chatmix(50, 30, channels=["game"])
    assert strips["media"].controls.chatmix_pct == 100 and strips["chat"].controls.chatmix_pct == 30


def test_bad_controls_are_rejected(tmp_path):
    rig = Rig(tmp_path)
    with pytest.raises(ValueError):
        rig.engine.set_volume("nonsense", 10)
    with pytest.raises(ValueError):
        rig.engine.set_mute("nonsense", True)
    rig.engine.set_volume("game", 500)
    assert rig.engine.volumes["game"] == 100.0
    rig.engine.set_volume("game", -5)
    assert rig.engine.volumes["game"] == 0.0


def test_the_control_commands(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    e = rig.engine
    assert e.handle({"cmd": "ping"}) == {"pong": True}
    e.handle({"cmd": "volume", "channel": "chat", "pct": 33})
    e.handle({"cmd": "mute", "channel": "media", "muted": True})
    e.handle({"cmd": "chatmix", "channels_pct": 70, "chat_pct": 90, "channels": ["game", "aux"]})
    st = e.handle({"cmd": "status"})["status"]
    assert st["volumes"]["chat"] == 33 and st["muted"]["media"] is True
    assert st["chatmix"] == {"channels_pct": 70.0, "chat_pct": 90.0, "channels": ["game", "aux"]}
    assert e.handle({"cmd": "what"})["ok"] is False
    assert e.handle({"cmd": "reload"}) == {}
    json.dumps(st)                                                            # the status must be JSON-able


def test_volumes_saved_by_the_gui_are_picked_up_from_channel_volumes_json(tmp_path):
    write(tmp_path, "channel_volumes.json", {"Arctis_Game": 40, "Arctis_Chat": 75, "Arctis_Media": 10, "other": 3})
    rig = Rig(tmp_path)
    rig.start()
    assert rig.engine.volumes == {"game": 40.0, "chat": 75.0, "media": 10.0, "aux": 100.0, "master": 100.0}
    assert rig.engine.graph.strips["game"].controls.volume_pct == 40.0


def test_the_master_level_is_read_from_channel_volumes_json_too(tmp_path):
    write(tmp_path, "channel_volumes.json", {"Arctis_Game": 40, "Arctis_Master": 35})
    rig = Rig(tmp_path)
    rig.start()
    assert rig.engine.volumes["master"] == 35.0 and rig.engine.volumes["game"] == 40.0


# ── state files ──────────────────────────────────────────────────────────────

def test_state_watcher_waits_for_a_change_to_settle_and_reads_it_once(tmp_path):
    reads = []

    def fake_reader(config_dir, hrir_id, rate):
        reads.append(1)
        from arctis_sound_manager.win.audio.graph import StateSnapshot
        return StateSnapshot()

    watcher = StateWatcher(tmp_path, reader=fake_reader)
    watcher.read()
    assert len(reads) == 1
    assert watcher.poll() is None and watcher.poll() is None                  # nothing changed
    write(tmp_path, "sonar_eq_state_game.json", {"x": 1})
    assert watcher.poll() is None                                             # just changed: maybe still being written
    assert watcher.poll() is not None and len(reads) == 2                     # unchanged since: read it
    assert watcher.poll() is None and len(reads) == 2
    write(tmp_path, "sonar_eq_state_game.json", {"x": 22})                    # same name, new size
    watcher.poll()
    assert watcher.poll() is not None and len(reads) == 3
    (tmp_path / "sonar_eq_state_game.json").unlink()                          # deleting counts too
    watcher.poll()
    assert watcher.poll() is not None and len(reads) == 4


def test_state_watcher_watches_the_files_the_graph_reads(tmp_path):
    names = ["sonar_eq_state_game.json", "sonar_eq_state_chat.json", "sonar_spatial_audio.json",
             "sonar_spatial_audio_media.json", "sonar_micro_state.json", "general_settings.yaml", "channel_volumes.json"]
    watcher = StateWatcher(tmp_path)
    empty = watcher.stamp()
    for n in names:
        (tmp_path / n).write_text("{}")
        assert watcher.stamp() != empty
        empty = watcher.stamp()
    (tmp_path / "unrelated.json").write_text("{}")
    assert watcher.stamp() == empty


def test_changing_a_state_file_changes_the_sound_without_a_restart(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.1)
    rig.start()
    rig.tick(100)
    flat = level(rig.heard(0.3))
    cut = [{"freq": 1000, "gain": -6.0, "q": 1.0, "type": "peakingEQ", "enabled": True}]
    write(tmp_path, "sonar_eq_state_game.json", {"bands": cut, "basses_db": 0, "voix_db": 0, "aigus_db": 0,
                                                  "boost_db": 0, "smart_volume": None})
    assert not rig.engine.poll_state()                                         # first sight: let it settle
    assert rig.engine.poll_state()
    rig.tick(100)
    assert level(rig.heard(0.3)) == pytest.approx(flat * 10 ** (-6 / 20), rel=0.02)    # a 6 dB cut at the tone's frequency
    assert rig.engine.status()["playback"]["errors"] == 0
    (tmp_path / "sonar_eq_state_game.json").unlink()                           # deleting the file means flat again
    rig.engine.poll_state()
    assert rig.engine.poll_state()
    rig.tick(100)
    assert level(rig.heard(0.3)) == pytest.approx(flat, rel=0.02)


def test_enabling_spatial_audio_from_the_files_virtualises_the_game_channel(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.1)
    rig.start()
    rig.tick(60)
    assert rig.engine.status()["playback"]["spatial"] == []
    write(tmp_path, "sonar_spatial_audio.json", {"enabled": True, "immersion": 50, "distance": 0, "auto": False})
    rig.engine.poll_state()
    assert rig.engine.poll_state()
    rig.tick(60)
    assert rig.engine.status()["playback"]["spatial"] == ["game"]
    y = rig.heard(0.3)
    assert level(y) > 0.01 and not np.allclose(y[:, 0], y[:, 1], atol=1e-4)    # HRTF: the two ears differ


# ── microphone ───────────────────────────────────────────────────────────────

def test_the_microphone_goes_through_the_mic_chain_to_the_virtual_cable(tmp_path):
    write(tmp_path, "sonar_micro_state.json", {"bands": [], "basses_db": 0, "voix_db": 6.0, "aigus_db": 0, "boost_db": 0})
    rig = Rig(tmp_path)
    rig.sources["mic"] = tone(2000, 0.05)
    rig.start()
    rig.tick(150)
    y = rig.heard(0.5, "mic")
    assert y.shape[1] == 2 and np.allclose(y[:, 0], y[:, 1])
    gain_db = 20 * np.log10(level(y[:, 0]) / (0.05 / np.sqrt(2)))
    assert gain_db == pytest.approx(6.0, abs=0.3)                              # the "Voix" macro, on the real mic path
    assert rig.engine.status()["endpoints"]["mic:out"]["underruns"] == 0


def test_without_a_microphone_configured_there_is_no_mic_pipeline(tmp_path):
    cfg = config(mic_input="", mic_output="")
    rig = Rig(tmp_path, cfg)
    rig.start()
    assert "mic:in" not in rig.engine.status()["endpoints"] and not rig.engine.status()["microphone"]["active"]
    rig.tick(10)


# ── devices coming and going ─────────────────────────────────────────────────

def test_a_device_that_stops_calling_back_is_closed_and_reopened(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.1)
    rig.start()
    rig.tick(50)
    e = rig.engine
    now = 100.0
    e.supervise(now)                                                         # first look: counts recorded
    rig.tick(10)
    e.supervise(now + 1)                                                     # still alive
    assert e.endpoints["in:game"].state == "running"
    rig.stream("CABLE-A Output", "input").running = False                    # the device goes quiet
    for step in range(3):
        rig.tick(5)
        e.supervise(now + 2 + step)
    ep = e.endpoints["in:game"]
    assert ep.reopened == 1 and ep.state == "running"                        # noticed, closed, reopened on a new stream
    assert "game" in e.playback.inputs
    rig.tick(100)
    assert level(rig.heard(0.3)) > 0.05                                      # and sound again


def test_without_its_headset_the_engine_idles_instead_of_guessing(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path, devices=[d for d in DEVICES if "Arctis Nova Pro" not in d.name or d.max_inputs])   # headset switched off
    rig.start()
    e = rig.engine
    assert e.endpoints["out:headset"].state == "waiting" and "no device matching" in e.endpoints["out:headset"].error
    assert not e.status()["playback"]["active"]
    rig.sources["game"] = tone(1000, 0.1)
    rig.tick(20)                                                               # nothing paces the engine: it just idles
    assert e.playback.step() == 0 and e.playback.stats.blocks == 0
    assert e.status()["endpoints"]["in:game"]["state"] == "running"            # the rest of the set-up is ready and waiting


def test_a_device_plugged_in_after_start_needs_a_refresh_and_a_probe_decides_when(tmp_path):
    spatial_off(tmp_path)
    headset = dev(7, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2)
    shown = {"probe": []}
    probes = []

    def probe():
        probes.append(1)
        return shown["probe"]

    rig = Rig(tmp_path, devices=[d for d in DEVICES if "Arctis" not in d.name], probe=probe, hotplugged=[headset])
    rig.start()
    e = rig.engine
    e.supervise(100.0)                                                        # the probe sees nothing new: no rebuild
    assert rig.backend.refreshes == 0 and len(probes) == 1
    e.supervise(101.0)
    assert len(probes) == 1                                                   # rationed: not again so soon
    shown["probe"] = [headset]
    e.supervise(120.0)
    assert rig.backend.refreshes == 1 and e.endpoints["out:headset"].state == "running"
    assert e.endpoints["in:game"].state == "running"                          # everything was reopened


def test_without_a_probe_a_blind_rebuild_never_interrupts_sound_someone_hears(tmp_path):
    spatial_off(tmp_path)
    headset = dev(7, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2)
    rig = Rig(tmp_path, devices=[d for d in DEVICES if "Arctis Nova Pro" not in d.name or d.max_inputs],
              hotplugged=[headset])                                            # headset off, the mic cable is running
    rig.start()
    rig.engine.supervise(100.0)
    rig.engine.supervise(200.0)
    assert rig.backend.refreshes == 0 and rig.engine.endpoints["out:headset"].state == "waiting"


def test_without_a_probe_and_with_nothing_audible_a_rebuild_looks_for_the_headset(tmp_path):
    spatial_off(tmp_path)
    headset = dev(7, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2)
    rig = Rig(tmp_path, config(mic_input="", mic_output=""),
              devices=[d for d in DEVICES if "Arctis Nova Pro" not in d.name or d.max_inputs], hotplugged=[headset])
    rig.start()
    rig.engine.supervise(100.0)
    assert rig.backend.refreshes == 1 and rig.engine.endpoints["out:headset"].state == "running"
    assert rig.engine.status()["playback"]["active"]
    rig.sources["game"] = tone(1000, 0.1)
    rig.tick(150)
    assert level(rig.heard()) > 0.05


def test_a_device_that_refuses_to_open_is_reported_and_retried(tmp_path):
    rig = Rig(tmp_path)
    rig.backend.fail_open = {"Headset Earphone (Arctis Nova Pro Wireless)"}
    rig.start()
    e = rig.engine
    ep = e.endpoints["out:headset"]
    assert ep.state == "waiting" and "cannot open" in ep.error
    assert e.status()["endpoints"]["out:headset"]["error"]
    rig.backend.fail_open = set()
    e.supervise(ep.next_try - 1)                                              # too early
    assert ep.state == "waiting"
    e.supervise(ep.next_try + 0.1)
    assert ep.state == "running" and ep.reopened == 1


def test_a_reopened_master_output_is_refilled_before_it_plays(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.start()
    e = rig.engine
    ep = e.endpoints["out:headset"]
    e._close(ep)
    ep.state, ep.next_try = "waiting", 0.0
    e.supervise(10.0)
    master = e.playback.master()
    assert master is ep.port and master.fill >= master.target


# ── robustness ───────────────────────────────────────────────────────────────

def test_a_bug_in_the_graph_fails_silent_and_keeps_running(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.1)
    rig.start()
    rig.tick(60)
    real = rig.engine.graph.process
    calls = {"n": 0}

    def flaky(inputs, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("bug")
        return real(inputs, **kwargs)

    rig.engine.graph.process = flaky
    rig.tick(40)
    assert rig.engine.playback.stats.errors == 3
    rig.tick(100)
    assert level(rig.heard(0.3)) > 0.05                                       # recovered by itself
    assert rig.engine.status()["endpoints"]["out:headset"]["underruns"] == 0   # the master was fed silence meanwhile


def test_a_bad_snapshot_does_not_stop_the_audio(tmp_path):
    spatial_off(tmp_path)
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.1)
    rig.start()
    rig.tick(30)
    rig.engine.graph.apply_playback = lambda snap: (_ for _ in ()).throw(ValueError("bad setting"))
    rig.engine.box.publish(rig.engine.box.snapshot)
    rig.tick(80)
    assert level(rig.heard(0.3)) > 0.05


def test_the_processing_load_is_measured(tmp_path):
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.1)
    rig.start()
    rig.tick(100)
    load = rig.engine.status()["playback"]["load"]
    assert 0.0 < load < 5.0


def test_to_channels():
    stereo = np.vstack([np.ones(4), np.zeros(4)])
    assert to_channels(stereo, 2) is stereo
    assert np.array_equal(to_channels(stereo, 1), np.full((1, 4), 0.5))
    wide = to_channels(stereo, 6)
    assert wide.shape == (6, 4) and np.array_equal(wide[:2], stereo) and not np.any(wide[2:])


def test_snapshot_box_counts_versions():
    box = SnapshotBox()
    assert box.version == 0 and box.snapshot is None
    box.publish("a")
    box.publish("b")
    assert box.version == 2 and box.snapshot == "b"


def test_a_stereo_headset_with_more_channels_gets_silence_on_the_extras(tmp_path):
    spatial_off(tmp_path)
    devices = [d if "Arctis Nova Pro" not in d.name or d.max_outputs == 0 else dev(d.index, d.name, outs=8) for d in DEVICES]
    cfg = config()
    rig = Rig(tmp_path, cfg, devices)
    rig.backend._cap = {}
    rig.start()
    ep = rig.engine.status()["endpoints"]["out:headset"]
    assert ep["channels"] == 2                                                # the engine asks for stereo


# ── real threads ─────────────────────────────────────────────────────────────

def test_the_threaded_engine_runs_against_devices_on_a_wall_clock_and_stops_cleanly(tmp_path):
    spatial_off(tmp_path)
    backend = FakeBackend(DEVICES)
    engine = Engine(config(), backend, config_dir=tmp_path)
    engine.start(threaded=True)
    game = backend.stream("CABLE-A Output", "input")
    headset = backend.stream("Arctis", "output")
    mic_in = backend.stream("Microphone", "input")
    cable = backend.stream("CABLE-C Input", "output")
    heard, stop = [], threading.Event()
    sin = tone(1000, 0.1)

    def clock():
        t, k = time.perf_counter(), 0
        while not stop.is_set():
            idx = np.arange(k * CHUNK, (k + 1) * CHUNK)
            game.deliver(sin(idx, game.channels))
            mic_in.deliver(sin(idx, mic_in.channels))
            heard.append(headset.collect(CHUNK))
            cable.collect(CHUNK)
            k += 1
            wait = t + k * CHUNK / RATE - time.perf_counter()
            if wait > 0:
                time.sleep(wait)

    thread = threading.Thread(target=clock, daemon=True)
    thread.start()
    time.sleep(2.0)
    stop.set()
    thread.join()
    began = time.perf_counter()
    engine.stop()
    assert time.perf_counter() - began < 3.0
    y = np.concatenate(heard)[-RATE // 2:]
    assert level(y[:, 0]) > 0.05                                             # the tone really went through the threads
    st = engine.status()
    assert st["playback"]["blocks"] > 30 and st["playback"]["errors"] == 0
    assert not any(t.is_alive() for t in engine._threads)


def test_a_stereo_microphone_is_averaged_to_mono(tmp_path):
    devices = [dev(d.index, d.name, ins=2) if "Microphone" in d.name else d for d in DEVICES]
    rig = Rig(tmp_path, devices=devices)
    rig.backend.min_channels = {"Headset Microphone (Arctis Nova Pro Wireless)": 2}      # a mic that only opens as stereo
    rig.start()
    assert rig.engine.status()["endpoints"]["mic:in"]["channels"] == 2
    rig.sources["mic"] = lambda idx, ch: np.stack([np.full(len(idx), 0.2), np.zeros(len(idx))], axis=1).astype(np.float32)
    rig.tick(150)
    assert level(rig.heard(0.3, "mic")[:, 0]) == pytest.approx(0.1, abs=0.005)       # (0.2 + 0) / 2, not 0.2 and not 0


def test_state_present_at_start_is_applied_before_the_first_block_is_made(tmp_path):
    spatial_off(tmp_path)
    cut = [{"freq": 1000, "gain": -6.0, "q": 1.0, "type": "peakingEQ", "enabled": True}]
    write(tmp_path, "sonar_eq_state_game.json", {"bands": cut, "basses_db": 0, "voix_db": 0, "aigus_db": 0,
                                                  "boost_db": 0, "smart_volume": None})
    rig = Rig(tmp_path)
    rig.sources["game"] = tone(1000, 0.1)
    rig.start()
    assert rig.engine.graph.strips["game"]._eq.sos.shape[0] > 0                    # applied during prefill, not a block later


def test_an_unrelated_file_change_never_snaps_a_slider_back_to_an_old_saved_value(tmp_path):
    spatial_off(tmp_path)
    write(tmp_path, "channel_volumes.json", {"Arctis_Game": 40})
    rig = Rig(tmp_path)
    rig.start()
    assert rig.engine.volumes["game"] == 40.0
    rig.engine.set_volume("game", 70)                                          # live, over the control channel
    rig.engine.set_chatmix(50, 100, channels=["game", "aux"])
    write(tmp_path, "sonar_eq_state_game.json", {"bands": [], "basses_db": 1.0, "voix_db": 0, "aigus_db": 0,
                                                  "boost_db": 0, "smart_volume": None})
    rig.engine.poll_state()
    assert rig.engine.poll_state()                                             # the EQ change was picked up ...
    assert rig.engine.volumes["game"] == 70.0                                  # ... the volume was left alone
    assert rig.engine.chatmix_channels == ["game", "aux"]                      # ... and so was the ChatMix channel list
    write(tmp_path, "channel_volumes.json", {"Arctis_Game": 30})
    rig.engine.poll_state()
    assert rig.engine.poll_state()
    assert rig.engine.volumes["game"] == 30.0                                  # but a changed volumes file does apply


def test_the_chatmix_channel_list_is_only_re_read_when_the_settings_file_changes(tmp_path, monkeypatch):
    watcher = StateWatcher(tmp_path, reader=lambda d, h, r: __import__("arctis_sound_manager.win.audio.graph", fromlist=["x"]).StateSnapshot())
    monkeypatch.setattr(watcher, "_read_chatmix_channels", lambda: ["game", "aux"])
    assert watcher.read().chatmix_channels == ["game", "aux"]                  # the first read always has everything
    write(tmp_path, "sonar_eq_state_game.json", {"x": 1})
    assert watcher.read().chatmix_channels is None
    (tmp_path / "general_settings.yaml").write_text("chatmix_channels: [game]\n")
    assert watcher.read().chatmix_channels == ["game", "aux"]
    assert watcher.read().chatmix_channels is None


def test_every_port_uses_the_engines_port_clock(tmp_path):
    """Tests (and nothing else) swap the clock the ports time the devices' callbacks with."""
    def fake_clock():
        return 0.0
    engine = Engine(config(), FakeBackend(DEVICES), config_dir=tmp_path, port_clock=fake_clock)
    engine.start(threaded=False)
    ports = [ep.port for ep in engine.endpoints.values()] + [engine.mic_tap]
    assert len(ports) >= 5 and all(p is not None and p._clock is fake_clock for p in ports)
    engine.stop()
    assert Engine(config(), FakeBackend(DEVICES), config_dir=tmp_path)._port_clock is time.perf_counter


def test_the_preset_wins_over_the_files_at_start(tmp_path):
    write(tmp_path, "channel_volumes.json", {"Arctis_Game": 40})
    backend = FakeBackend(DEVICES)
    engine = Engine(config(), backend, config_dir=tmp_path)
    engine.preset = {"volumes": {"game": 15.0}, "muted": {"aux": True}, "mix": (50.0, 80.0), "mix_channels": ["game", "media"]}
    engine.start(threaded=False)
    assert engine.volumes["game"] == 15.0 and engine.muted["aux"] is True
    assert engine.mix == (50.0, 80.0) and engine.chatmix_channels == ["game", "media"]
    s = engine.graph.strips
    assert s["game"].controls.volume_pct == 15.0 and s["aux"].controls.muted and s["media"].controls.chatmix_pct == 50.0
    engine.stop()
