"""The engine process shell (host + CLI) against in-memory devices."""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from arctis_sound_manager.win.audio import engine_main
from arctis_sound_manager.win.audio.backend import BackendError, DeviceInfo, FakeBackend, device_from_dict, device_to_dict
from arctis_sound_manager.win.audio.control import ControlServer, EngineClient
from arctis_sound_manager.win.audio.engine_config import EngineConfig, save_config
from arctis_sound_manager.win.audio.host import EngineHost, subprocess_probe


def dev(index, name, ins=0, outs=0):
    return DeviceInfo(index, name, "Windows WASAPI", ins, outs, 48000.0, default_output=(outs > 0 and index == 9))


DEVICES = [
    dev(1, "CABLE-A Output (VB-Audio Cable A)", ins=2), dev(2, "CABLE-A Input (VB-Audio Cable A)", outs=2),
    dev(3, "CABLE-B Output (VB-Audio Cable B)", ins=2), dev(4, "CABLE-B Input (VB-Audio Cable B)", outs=2),
    dev(5, "CABLE-C Output (VB-Audio Cable C)", ins=2), dev(6, "CABLE-C Input (VB-Audio Cable C)", outs=2),
    dev(7, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2),
    dev(8, "Headset Microphone (Arctis Nova Pro Wireless)", ins=1),
    dev(9, "Speakers (Realtek(R) Audio)", outs=2),
]


def good_config():
    return EngineConfig(inputs={"game": "CABLE-A Output", "chat": "CABLE-B Output", "media": "", "aux": ""},
                        outputs={"headset": "Arctis Nova Pro"}, mic_input="Microphone (Arctis",
                        mic_output="CABLE-C Input", latency="low")


@pytest.fixture
def host(tmp_path):
    backends = []

    def factory(cfg):
        b = FakeBackend(DEVICES)
        backends.append(b)
        return b

    h = EngineHost(tmp_path, backend_factory=factory, probe=None)
    h.backends = backends
    yield h
    h.stop()


# ── devices as JSON ──────────────────────────────────────────────────────────

def test_devices_survive_a_json_round_trip():
    for d in DEVICES:
        assert device_from_dict(json.loads(json.dumps(device_to_dict(d)))) == d


def test_the_probe_parses_the_other_processes_listing(monkeypatch):
    import subprocess

    class Done:
        returncode = 0
        stdout = json.dumps([device_to_dict(d) for d in DEVICES[:2]])
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    assert subprocess_probe() == DEVICES[:2]
    Done.returncode, Done.stderr = 1, "PortAudio exploded"
    with pytest.raises(RuntimeError, match="PortAudio exploded"):
        subprocess_probe()


# ── idling without a configuration ───────────────────────────────────────────

def test_without_a_configuration_the_host_idles_and_says_why(host):
    assert host.reconfigure() != []
    st = host.status()
    assert st["running"] is False and "no headset output device is chosen" in st["problems"]
    assert host.engine is None and "waiting for a configuration" in st["notes"]
    json.dumps(st)


def test_the_assistant_can_list_devices_and_ask_for_a_guess_while_idle(host):
    host.reconfigure()
    listing = host.handle({"cmd": "devices"})
    assert len(listing["devices"]) == len(DEVICES) and listing["missing"] == []
    guess = host.handle({"cmd": "suggest"})["config"]
    assert guess["outputs"]["headset"].startswith("Headset Earphone") and guess["inputs"]["game"].startswith("CABLE-A")


def test_the_device_list_says_which_configured_devices_cannot_be_found(host, tmp_path):
    cfg = good_config()
    cfg.inputs["media"] = "CABLE-Q Output"
    save_config(cfg, tmp_path)
    host.reconfigure()
    reply = host.handle({"cmd": "devices"})
    assert reply["missing"] == ["media: no capture device matching 'CABLE-Q Output'"]


def test_commands_sent_while_idle_are_remembered_and_replayed(host, tmp_path):
    host.reconfigure()
    host.handle({"cmd": "volume", "channel": "game", "pct": 35})
    host.handle({"cmd": "mute", "channel": "chat", "muted": True})
    host.handle({"cmd": "chatmix", "channels_pct": 60, "chat_pct": 90, "channels": ["game", "media", "chat"]})
    save_config(good_config(), tmp_path)
    assert host.reconfigure() == []
    st = host.engine.status()
    assert st["volumes"]["game"] == 35 and st["muted"]["chat"] is True
    assert st["chatmix"] == {"channels_pct": 60.0, "chat_pct": 90.0, "channels": ["game", "media"]}
    assert host.engine.graph.strips["game"].controls.chatmix_pct == 60.0


# ── running ──────────────────────────────────────────────────────────────────

def test_a_good_configuration_starts_the_engine(host, tmp_path):
    save_config(good_config(), tmp_path)
    assert host.reconfigure() == []
    st = host.status()
    assert st["running"] and st["playback"]["active"] and st["version"].startswith("win-engine")
    assert set(st["endpoints"]) == {"in:game", "in:chat", "out:headset", "mic:in", "mic:out"}


def test_reconfigure_rebuilds_the_engine_from_the_changed_file(host, tmp_path):
    save_config(good_config(), tmp_path)
    host.reconfigure()
    first = host.engine
    host.handle({"cmd": "volume", "channel": "game", "pct": 20})
    cfg = good_config()
    cfg.inputs["media"] = "CABLE-C Output"
    cfg.mic_input = cfg.mic_output = ""
    save_config(cfg, tmp_path)
    reply = host.handle({"cmd": "reconfigure"})
    assert reply["ok"] and reply["problems"] == []
    assert host.engine is not first and "in:media" in host.engine.endpoints and "mic:in" not in host.engine.endpoints
    assert host.engine.volumes["game"] == 20.0                                 # the volume survived the rebuild
    assert not any(t.is_alive() for t in first._threads)                       # the old engine really stopped


def test_a_configuration_that_is_still_wrong_reports_the_problems(host, tmp_path):
    cfg = good_config()
    cfg.outputs["headset"] = ""
    save_config(cfg, tmp_path)
    reply = host.handle({"cmd": "reconfigure"})
    assert reply["ok"] is False and "no headset output device is chosen" in reply["problems"]
    assert host.engine is None


def test_a_missing_sound_library_is_an_error_status_not_a_crash(tmp_path):
    def broken(cfg):
        raise BackendError("sounddevice / PortAudio is not available")
    h = EngineHost(tmp_path, backend_factory=broken, probe=None)
    save_config(good_config(), tmp_path)
    problems = h.reconfigure()
    assert problems == ["sounddevice / PortAudio is not available"]          # the plain message, not "BackendError: ..."
    assert h.status()["running"] is False and h.status()["error"] == problems[0]
    assert h.handle({"cmd": "devices"})["ok"] is False


def test_an_unexpected_failure_while_starting_is_contained(tmp_path, monkeypatch):
    h = EngineHost(tmp_path, backend_factory=lambda cfg: FakeBackend(DEVICES), probe=None)
    save_config(good_config(), tmp_path)
    from arctis_sound_manager.win.audio import host as host_module
    monkeypatch.setattr(host_module.Engine, "start", lambda self, threaded=True: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "RuntimeError: boom" in h.reconfigure()[0]
    assert h.engine is None


def test_out_of_range_values_are_clamped_before_they_are_remembered(host, tmp_path):
    host.handle({"cmd": "volume", "channel": "game", "pct": 500})
    host.handle({"cmd": "volume", "channel": "chat", "pct": -20})
    host.handle({"cmd": "chatmix", "channels_pct": 900, "chat_pct": -1})
    save_config(good_config(), tmp_path)
    host.reconfigure()
    assert host.engine.volumes["game"] == 100.0 and host.engine.volumes["chat"] == 0.0
    assert host.engine.mix == (100.0, 0.0)


def test_unknown_channels_and_commands_are_rejected(host):
    with pytest.raises(ValueError):
        host.handle({"cmd": "volume", "channel": "nope", "pct": 5})
    with pytest.raises(ValueError):
        host.handle({"cmd": "mute", "channel": "nope", "muted": True})
    assert host.handle({"cmd": "frobnicate"})["ok"] is False


def test_shutdown_ends_the_wait(host):
    import threading
    t = threading.Thread(target=lambda: host.wait(poll=0.02), daemon=True)
    t.start()
    host.handle({"cmd": "shutdown"})
    t.join(timeout=2)
    assert not t.is_alive()


def test_an_idle_host_starts_when_a_configuration_appears(host, tmp_path):
    import threading
    host.reconfigure()
    t = threading.Thread(target=lambda: host.wait(poll=0.02), daemon=True)
    t.start()
    save_config(good_config(), tmp_path)
    deadline = time.time() + 3
    while host.engine is None and time.time() < deadline:
        time.sleep(0.02)
    assert host.engine is not None and host.status()["running"]
    host.handle({"cmd": "shutdown"})
    t.join(timeout=2)


def test_the_whole_chain_client_server_host_engine(host, tmp_path):
    save_config(good_config(), tmp_path)
    host.reconfigure()
    server = ControlServer(host.handle, tmp_path)
    server.start()
    try:
        client = EngineClient(tmp_path, timeout=3.0)
        assert client.available()
        client.set_volume("chat", 42)
        client.set_chatmix(55, 100)
        st = client.status()["status"]
        assert st["volumes"]["chat"] == 42 and st["chatmix"]["channels_pct"] == 55.0
        assert client.reconfigure()["ok"]
        assert client.status()["status"]["volumes"]["chat"] == 42            # still there after a rebuild
        assert client.request("devices")["devices"]
    finally:
        server.stop()


# ── the command line ─────────────────────────────────────────────────────────

def test_list_devices_prints_wasapi_first_and_json_on_request():
    lines = []
    backend = FakeBackend([DeviceInfo(0, "MME thing", "MME", 2, 0), *DEVICES])
    assert engine_main.list_devices(backend, False, out=lines.append) == 0
    assert "WASAPI" in lines[0] and "MME thing" in lines[-1]
    out = []
    engine_main.list_devices(backend, True, out=out.append)
    assert len(json.loads(out[0])) == len(DEVICES) + 1


def test_the_benchmark_runs_the_real_graph_and_reports_load(tmp_path):
    result = engine_main.benchmark(tmp_path, block=960, seconds=0.3)
    assert result["block"] == 960 and result["playback_ms_mean"] > 0 and 0 < result["playback_load"] < 50
    assert result["spatial"] == ["game", "media", "aux"]                       # Sonar's default: spatial on
    assert "playback_worst_load" in result and "mic_load" in result


def test_cable_check_hears_the_tone_it_sent(tmp_path):
    backend = FakeBackend(DEVICES)
    capture, render = DEVICES[0], DEVICES[1]
    started = time.perf_counter()
    sent = {"frames": 0}

    def pump():
        # the fake cable: whatever the render end is asked for arrives at the capture end
        want = int((time.perf_counter() - started) * 48000) - sent["frames"]
        if want >= 480:
            want = (want // 480) * 480
            try:
                data = backend.stream("CABLE-A Input", "output").collect(want)
                backend.stream("CABLE-A Output", "input").deliver(data)
                sent["frames"] += want
            except KeyError:
                pass

    r = engine_main.check_cable(backend, capture, render, seconds=1.0, pump=pump)
    assert r["ok"] and r["tone_hz"] == pytest.approx(1000, abs=5) and r["level"] == pytest.approx(r["expected"], rel=0.02)
    assert r["rate_ratio"] == pytest.approx(1.0, abs=0.1)


def test_a_dead_cable_is_reported(tmp_path):
    backend = FakeBackend(DEVICES)
    r = engine_main.check_cable(backend, DEVICES[0], DEVICES[1], seconds=0.3)
    assert r["ok"] is False and "nothing came back" in r["error"]


def test_selftest_reports_every_stage_and_the_result(tmp_path):
    save_config(good_config(), tmp_path)
    lines = []
    backend = FakeBackend(DEVICES)
    t0 = time.perf_counter()
    sent = {}

    def pump():
        for cable in ("CABLE-A", "CABLE-B"):
            try:
                out = backend.stream(f"{cable} Input", "output")
                inp = backend.stream(f"{cable} Output", "input")
            except KeyError:
                continue
            done = sent.get(cable, 0)
            want = int((time.perf_counter() - t0) * 48000) - done
            if want >= 480:
                want = (want // 480) * 480
                inp.deliver(out.collect(want))
                sent[cable] = done + want

    code = engine_main.selftest(tmp_path, backend, out=lines.append, seconds=0.6, pump=pump)
    text = "\n".join(lines)
    assert "== 1. Configuration" in text and "== 2. Devices" in text and "== 3. Cable loopback" in text
    assert "== 4. Processing speed" in text and "game" in text and "ok" in text
    assert code in (0, 1)                                                      # 1 only if this machine is too slow for the bench


def test_selftest_with_an_empty_configuration_explains_what_to_do(tmp_path):
    lines = []
    code = engine_main.selftest(tmp_path, FakeBackend(DEVICES), out=lines.append, seconds=0.2)
    text = "\n".join(lines)
    assert code == 1 and "PROBLEM: no headset output device is chosen" in text and "--suggest" in text


def test_selftest_names_missing_devices(tmp_path):
    cfg = good_config()
    cfg.inputs["game"] = "CABLE-Z Output"
    save_config(cfg, tmp_path)
    lines = []
    assert engine_main.selftest(tmp_path, FakeBackend(DEVICES), out=lines.append, seconds=0.2) == 1
    assert any("MISSING: game: no capture device matching 'CABLE-Z Output'" in l for l in lines)


def test_main_reports_a_missing_sounddevice_package(tmp_path, capsys, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    assert engine_main.main(["--config-dir", str(tmp_path), "--list-devices"]) == 2
    assert "pip install sounddevice" in capsys.readouterr().err


def test_main_suggest_prints_and_optionally_saves_a_guess(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(engine_main, "SoundDeviceBackend", lambda *a, **k: FakeBackend(DEVICES))
    assert engine_main.main(["--config-dir", str(tmp_path), "--suggest", "--write"]) == 0
    captured = capsys.readouterr()
    guess = json.loads(captured.out)
    assert guess["outputs"]["headset"].startswith("Headset Earphone") and "saved to" in captured.err
    assert (tmp_path / "win_audio_engine.json").exists()
    assert engine_main.main(["--config-dir", str(tmp_path), "--suggest", "--write"]) == 0
    assert "not writing" in capsys.readouterr().err                            # never overwrites a configuration


def test_main_bench_prints_json(tmp_path, capsys):
    assert engine_main.main(["--config-dir", str(tmp_path), "--bench", "--block", "480"]) == 0
    assert json.loads(capsys.readouterr().out)["block"] == 480


def test_run_engine_serves_until_told_to_shut_down(tmp_path, monkeypatch):
    import threading
    backend = FakeBackend(DEVICES)
    from arctis_sound_manager.win.audio import host as host_module
    monkeypatch.setattr(host_module, "default_backend", lambda cfg: backend)
    monkeypatch.setattr(engine_main, "EngineHost", lambda d: EngineHost(d, backend_factory=lambda cfg: backend, probe=None))
    save_config(good_config(), tmp_path)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", engine_main.run_engine(tmp_path)), daemon=True)
    t.start()
    client = EngineClient(tmp_path, timeout=2.0)
    deadline = time.time() + 5
    while not client.available() and time.time() < deadline:
        time.sleep(0.05)
    assert client.available()
    assert client.status()["status"]["running"]
    assert engine_main.run_engine(tmp_path) == 0                               # a second engine just says so and leaves
    client.shutdown()
    t.join(timeout=5)
    assert result["code"] == 0 and not client.available()


def test_ping_status_and_shutdown_do_not_wait_for_a_start_in_progress(host, tmp_path, monkeypatch):
    """The first start compiles kernels for seconds; the GUI polls ping/status meanwhile and must get answers."""
    import threading
    from arctis_sound_manager.win.audio import fast
    save_config(good_config(), tmp_path)
    monkeypatch.setattr(fast, "_ready", threading.Event())                     # a warm-up that has not finished
    starter = threading.Thread(target=host.reconfigure, daemon=True)
    starter.start()
    time.sleep(0.2)
    assert starter.is_alive()                                                  # the engine is waiting for the kernels
    t0 = time.time()
    assert host.handle({"cmd": "ping"})["pong"]
    status = host.handle({"cmd": "status"})["status"]
    assert not status["running"] and any("compiling" in n for n in status["notes"])
    host.handle({"cmd": "shutdown"})
    assert host.shutdown_requested.is_set()
    assert time.time() - t0 < 1.0
    fast._ready.set()
    starter.join(timeout=5)
    assert not starter.is_alive() and host.handle({"cmd": "status"})["status"]["running"]


# ── the headset's master level ───────────────────────────────────────────────

def test_the_master_level_is_a_control_like_the_channels(host, tmp_path):
    save_config(good_config(), tmp_path)
    host.reconfigure()
    host.handle({"cmd": "volume", "channel": "master", "pct": 40})
    host.handle({"cmd": "mute", "channel": "master", "muted": True})
    st = host.engine.status()
    assert st["volumes"]["master"] == 40 and st["muted"]["master"] is True
    assert host.engine.graph.master.volume_pct == 40 and host.engine.graph.master.muted is True


def test_the_master_level_survives_idling_and_a_rebuild(host, tmp_path):
    host.reconfigure()                                                          # no configuration yet
    host.handle({"cmd": "volume", "channel": "master", "pct": 25})
    save_config(good_config(), tmp_path)
    host.reconfigure()
    assert host.engine.volumes["master"] == 25.0 and host.engine.graph.master.volume_pct == 25.0
    host.handle({"cmd": "volume", "channel": "master", "pct": 80})
    host.reconfigure()                                                          # rebuilt from the file
    assert host.engine.graph.master.volume_pct == 80.0


def test_the_master_level_is_restored_from_channel_volumes_json(host, tmp_path):
    (tmp_path / "channel_volumes.json").write_text(json.dumps({"Arctis_Master": 55, "Arctis_Game": 65}))
    save_config(good_config(), tmp_path)
    host.reconfigure()
    host.engine.reload()
    assert host.engine.volumes["master"] == 55.0 and host.engine.volumes["game"] == 65.0
    assert host.engine.graph.master.volume_pct == 55.0


def test_the_master_level_is_clamped(host, tmp_path):
    host.handle({"cmd": "volume", "channel": "master", "pct": 900})
    save_config(good_config(), tmp_path)
    host.reconfigure()
    assert host.engine.graph.master.volume_pct == 100.0
