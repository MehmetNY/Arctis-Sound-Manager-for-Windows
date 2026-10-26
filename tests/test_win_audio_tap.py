"""The per-application tap: protocol, routing rules, the helper process, and its place in the engine."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from arctis_sound_manager.win.audio import tap as tapmod
from arctis_sound_manager.win.audio.backend import BackendError, DeviceInfo, FakeBackend
from arctis_sound_manager.win.audio.engine_config import EngineConfig, suggest_config
from arctis_sound_manager.win.audio.runtime import Engine
from arctis_sound_manager.win.audio.tap import (AppRules, FrameDecoder, TapBackend, TapHelper, exe_key,
                                                friendly_app_name, load_rules, save_rules, tap_device_name, tap_devices)

HERE = Path(__file__).parent
RATE = 48000
CHUNK = 480


def packet(frames=CHUNK, channels=8, fill=0.0, magic=b"ATP1"):
    data = np.full((frames, channels), fill, dtype="<f4")
    return tapmod.HEADER.pack(magic, frames, channels, 0) + data.tobytes()


# ── the byte stream ──────────────────────────────────────────────────────────

def test_the_decoder_returns_one_block_per_packet():
    out = FrameDecoder().feed(packet(fill=0.25) + packet(fill=0.5))
    assert [b.shape for b in out] == [(CHUNK, 8), (CHUNK, 8)]
    assert out[0][0, 0] == 0.25 and out[1][-1, 7] == 0.5


def test_the_decoder_waits_for_the_rest_of_a_packet():
    decoder, data = FrameDecoder(), packet(fill=0.1)
    assert decoder.feed(data[:7]) == [] and decoder.feed(data[7:1000]) == []
    (block,) = decoder.feed(data[1000:])
    assert block.shape == (CHUNK, 8) and np.allclose(block, 0.1)


def test_the_decoder_finds_the_next_packet_after_garbage():
    decoder = FrameDecoder()
    out = decoder.feed(b"\x00\x01garbage" + packet(fill=0.3))
    assert len(out) == 1 and np.allclose(out[0], 0.3) and decoder.skipped == len(b"\x00\x01garbage")


def test_the_decoder_refuses_absurd_headers():
    decoder = FrameDecoder()
    bad = tapmod.HEADER.pack(b"ATP1", 10 ** 9, 8, 0) + packet(fill=0.2)
    out = decoder.feed(bad)
    assert len(out) == 1 and np.allclose(out[0], 0.2)


def test_the_decoder_copes_with_data_arriving_a_byte_at_a_time():
    decoder, out = FrameDecoder(), []
    for b in packet(frames=4, channels=2, fill=0.5):
        out += decoder.feed(bytes([b]))
    assert len(out) == 1 and out[0].shape == (4, 2)


# ── which program goes where ─────────────────────────────────────────────────

def test_known_voice_chat_programs_go_to_chat_players_to_media_and_the_rest_to_game():
    rules = AppRules()
    assert rules.channel_for("Discord.exe") == "chat"
    assert rules.channel_for("C:\\Users\\me\\AppData\\Local\\Microsoft\\Teams\\ms-teams.exe") == "chat"
    assert rules.channel_for("spotify.exe") == "media"
    assert rules.channel_for("chrome.exe") == "media"
    assert rules.channel_for("RocketLeague.exe") == "game"


def test_a_users_choice_beats_the_defaults_and_can_be_forgotten():
    rules = AppRules()
    assert rules.set("Spotify.exe", "aux") and rules.channel_for("spotify.exe") == "aux"
    assert rules.set("game.exe", "bypass") and rules.channel_for("game.exe") == "bypass"
    assert rules.clear("spotify") and rules.channel_for("spotify.exe") == "media"
    assert not rules.clear("never-set.exe")


def test_nonsense_routes_are_refused():
    rules = AppRules()
    assert not rules.set("", "game") and not rules.set("x.exe", "ceiling")
    assert rules.user == {}


def test_names_are_normalised():
    assert exe_key("C:\\Games\\Foo\\Foo.EXE") == "foo.exe" and exe_key("Foo") == "foo.exe" and exe_key("") == ""
    assert friendly_app_name("msedge.exe") == "Msedge" and friendly_app_name("Rocket_League.exe") == "Rocket League"
    assert friendly_app_name("VLC.exe") == "VLC"


def test_the_helper_gets_the_defaults_before_the_users_rules_and_the_users_win():
    rules = AppRules(user={"discord.exe": "aux"})
    lines = rules.commands()
    assert lines[0] == "default\tgame"
    assert lines.count("rule\tdiscord.exe\taux") == 1 and "rule\tdiscord.exe\tchat" not in lines
    assert "rule\tspotify.exe\tmedia" in lines


def test_rules_survive_a_restart(tmp_path):
    rules = AppRules(user={"foo.exe": "chat"})
    save_rules(rules, tmp_path)
    assert load_rules(tmp_path).user == {"foo.exe": "chat"}


def test_a_broken_rules_file_means_the_defaults(tmp_path):
    (tmp_path / tapmod.ROUTING_FILE).write_text("{not json")
    assert load_rules(tmp_path).user == {}
    (tmp_path / tapmod.ROUTING_FILE).write_text(json.dumps({"default": "ceiling", "rules": {"a.exe": "chat", "b.exe": "no"}}))
    loaded = load_rules(tmp_path)
    assert loaded.default == "game" and loaded.user == {"a.exe": "chat"}


# ── the devices the engine sees ──────────────────────────────────────────────

def test_the_tap_offers_four_stereo_capture_devices_that_look_like_wasapi_ones():
    devices = tap_devices()
    assert [d.name for d in devices] == [tap_device_name(c) for c in ("game", "chat", "media", "aux")]
    assert all(d.max_inputs == 2 and d.max_outputs == 0 and d.wasapi for d in devices)
    assert len({d.index for d in devices}) == 4


class FakeProc:
    """What subprocess.Popen returns, with the three pipes made of real descriptors."""

    def __init__(self):
        self._out_r, self._out_w = os.pipe()
        self._err_r, self._err_w = os.pipe()
        self._in_r, self._in_w = os.pipe()
        self.stdout = os.fdopen(self._out_r, "rb", buffering=0)
        self.stderr = os.fdopen(self._err_r, "rb")
        self.stdin = os.fdopen(self._in_w, "wb")
        self._in = os.fdopen(self._in_r, "rb")
        self._rc = None
        self.pid = 4711
        self.killed = False

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self._rc = 0 if self._rc is None else self._rc
        return self._rc

    def terminate(self):
        self.finish(1)

    def kill(self):
        self.killed = True
        self.finish(-9)

    def finish(self, rc=0):
        self._rc = rc
        for fd in (self._out_w, self._err_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def commands(self) -> list[str]:
        self.stdin.flush()
        os.set_blocking(self._in.fileno(), False)
        try:
            raw = self._in.read() or b""
        except BlockingIOError:
            raw = b""
        return raw.decode().splitlines()

    def say(self, text: str):
        os.write(self._err_w, text.encode())


def make_helper(procs=None, **kw):
    procs = procs if procs is not None else []

    def popen(cmd, **kwargs):
        proc = FakeProc()
        proc.cmd = cmd
        procs.append(proc)
        return proc
    helper = TapHelper(exe="/fake/asm-audiotap.exe", popen=popen, exists=lambda p: True, state=kw.pop("state", None) or Path(os.devnull).parent / "x", **kw)
    return helper, procs


def test_the_backend_lists_the_tap_only_when_the_helper_is_installed():
    real = FakeBackend([DeviceInfo(1, "Headset", "Windows WASAPI", 0, 2)])
    present = TapBackend(real, TapHelper(exe="/x", exists=lambda p: True))
    absent = TapBackend(real, TapHelper(exe="", exists=lambda p: False))
    assert len(present.devices()) == 5 and len(absent.devices()) == 1


def test_opening_a_tap_device_starts_the_helper_and_other_devices_pass_through(tmp_path):
    real = FakeBackend([DeviceInfo(1, "Headset", "Windows WASAPI", 0, 2), DeviceInfo(2, "Mic", "Windows WASAPI", 1, 0)])
    helper, procs = make_helper(state=tmp_path / "s.txt")
    backend = TapBackend(real, helper)
    got = []
    stream = backend.open_input(tap_devices()[0], 8, got.append)
    assert isinstance(stream, tapmod.TapStream) and stream.channels == 2 and len(procs) == 1
    backend.open_input(real.devices()[1], 1, got.append)
    assert len(procs) == 1 and real.opened[-1].device.name == "Mic"
    backend.open_output(real.devices()[0], 2, lambda out: None)
    assert real.opened[-1].kind == "output"


def test_a_missing_helper_is_a_backend_error():
    backend = TapBackend(FakeBackend([]), TapHelper(exe="", exists=lambda p: False))
    with pytest.raises(BackendError, match="not found"):
        backend.open_input(tap_devices()[0], 2, lambda d: None)


def test_blocks_reach_the_stream_of_their_channel_and_only_that_one(tmp_path):
    helper, _ = make_helper(state=tmp_path / "s.txt")
    backend = TapBackend(FakeBackend([]), helper)
    game, chat = [], []
    s1 = backend.open_input(tap_devices()[0], 2, game.append)
    s2 = backend.open_input(tap_devices()[1], 2, chat.append)
    s1.start(), s2.start()
    block = np.zeros((CHUNK, 8), dtype=np.float32)
    block[:, 0], block[:, 1], block[:, 2], block[:, 3] = 0.1, 0.2, 0.3, 0.4
    helper._dispatch(block)
    assert game[0].shape == (CHUNK, 2) and np.allclose(game[0], [[0.1, 0.2]]) and game[0].flags["C_CONTIGUOUS"]
    assert np.allclose(chat[0], [[0.3, 0.4]])
    s1.stop()
    helper._dispatch(block)
    assert len(game) == 1 and len(chat) == 2


def test_a_failing_consumer_does_not_stop_the_others(tmp_path):
    helper, _ = make_helper(state=tmp_path / "s.txt")
    seen = []

    def boom(_):
        raise RuntimeError("x")
    helper.attach("game", boom)
    helper.attach("chat", seen.append)
    helper._dispatch(np.zeros((CHUNK, 8), dtype=np.float32))
    assert len(seen) == 1


def test_the_helper_is_stopped_after_a_grace_period_once_nobody_uses_it(tmp_path, monkeypatch):
    monkeypatch.setattr(tapmod, "STOP_GRACE", 0.05)
    helper, procs = make_helper(state=tmp_path / "s.txt")
    assert helper.acquire() and helper.acquire()
    helper.release()
    time.sleep(0.15)
    assert procs[0].poll() is None                                  # one user left
    helper.release()
    time.sleep(0.3)
    assert procs[0].poll() == 0                                     # asked to quit
    assert helper.acquire() and len(procs) == 2                     # and it starts again when needed


def test_reacquiring_within_the_grace_period_keeps_the_same_process(tmp_path, monkeypatch):
    monkeypatch.setattr(tapmod, "STOP_GRACE", 0.2)
    helper, procs = make_helper(state=tmp_path / "s.txt")
    helper.acquire()
    helper.release()
    helper.acquire()
    time.sleep(0.4)
    assert len(procs) == 1 and procs[0].poll() is None
    helper.stop()


def test_status_lines_become_the_app_list_and_stale_ones_disappear(tmp_path):
    clock = [0.0]
    helper, procs = make_helper(state=tmp_path / "s.txt", clock=lambda: clock[0])
    helper.ensure_running()
    procs[0].say(json.dumps({"t": "status", "engaged": True, "endpoint": "H", "apps": [{"exe": "a.exe", "pid": 1, "leaks": 2}]}) + "\n")
    procs[0].say("not json\n" + json.dumps({"t": "log", "msg": "tapping a.exe"}) + "\n")
    deadline = time.monotonic() + 10.0                              # the reader thread may be slow on a busy machine
    while not helper.apps() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert helper.apps()[0]["exe"] == "a.exe" and helper.info()["leaks"] == 2 and helper.info()["engaged"] is True
    assert helper.stderr_log == ["tapping a.exe"]
    clock[0] = 100.0
    assert helper.apps() == [] and helper.status() == {}


def test_configuration_is_sent_on_start_and_replayed_into_a_restarted_process(tmp_path):
    helper, procs = make_helper(state=tmp_path / "s.txt")
    helper.configure(endpoint="Headset Earphone", rules=AppRules(user={"foo.exe": "aux"}), engaged=True)
    helper.ensure_running()
    sent = procs[0].commands()
    assert sent[0] == "clear" and "endpoint\tHeadset Earphone" in sent and "rule\tfoo.exe\taux" in sent
    assert f"exclude\t{os.getpid()}" in sent and sent[-2:] == ["engage\t1", "apply"]
    procs[0].finish(1)                                              # the helper dies
    assert not helper.alive and helper.ensure_running() and helper.restarts == 1
    again = procs[1].commands()
    assert "endpoint\tHeadset Earphone" in again and "engage\t1" in again


def test_engaging_and_routing_send_just_what_changed(tmp_path):
    helper, procs = make_helper(state=tmp_path / "s.txt")
    helper.ensure_running()
    procs[0].commands()
    helper.engage(False)
    assert procs[0].commands() == ["engage\t0", "apply"]
    assert helper.set_rule("Foo.exe", "chat") and "rule\tfoo.exe\tchat" in procs[0].commands()
    assert helper.set_rule("Foo.exe", None) and "rule\tfoo.exe\tchat" not in procs[0].commands()
    assert not helper.set_rule("", "chat") and not helper.set_rule("x.exe", "ceiling")


def test_stop_tells_the_helper_to_put_everything_back(tmp_path):
    helper, procs = make_helper(state=tmp_path / "s.txt")
    helper.ensure_running()
    procs[0].commands()
    helper.stop()
    assert procs[0].commands() == ["engage\t0", "quit"] and not helper.alive


def test_a_helper_that_ignores_quit_is_terminated(tmp_path):
    helper, procs = make_helper(state=tmp_path / "s.txt")
    helper.ensure_running()
    proc = procs[0]
    proc.wait = lambda timeout=None: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", timeout))
    helper.stop(timeout=0.01)
    assert proc.poll() == -9 and proc.killed


def test_a_helper_that_cannot_start_reports_why(tmp_path):
    def popen(*a, **k):
        raise OSError("blocked by antivirus")
    helper = TapHelper(exe="/fake", popen=popen, exists=lambda p: True, state=tmp_path / "s")
    assert not helper.ensure_running() and "blocked by antivirus" in helper.last_error


# ── against the real protocol, with a stand-in program ───────────────────────

@pytest.fixture
def fake_exe(tmp_path):
    script = tmp_path / "fake-audiotap"
    script.write_text(f"#!{sys.executable}\n" + (HERE / "fake_audiotap.py").read_text())
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.mark.skipif(os.name == "nt", reason="the stand-in is a POSIX script")
def test_a_real_process_streams_blocks_status_and_commands(fake_exe, tmp_path):
    state = tmp_path / "state.txt"
    helper = TapHelper(exe=str(fake_exe), state=state)
    got = []
    helper.attach("game", got.append)
    try:
        helper.configure(endpoint="Fake Headset", engaged=True)
        assert helper.ensure_running()
        for _ in range(1000):
            if len(got) >= 20 and helper.apps() and helper.info()["engaged"]:
                break
            time.sleep(0.01)
        assert len(got) >= 20 and got[0].shape == (CHUNK, 2)
        assert 0.15 < float(np.abs(got[3][:, 0]).max()) <= 0.2001 and 0.07 < float(np.abs(got[3][:, 1]).max()) <= 0.1001
        assert helper.apps()[0]["exe"] == "game.exe" and helper.info()["engaged"] is True
        for _ in range(500):
            if (tmp_path / "state.txt.cmds").exists():
                break
            time.sleep(0.01)
        text = (tmp_path / "state.txt.cmds").read_text()
        assert "endpoint\tFake Headset" in text and "engage\t1" in text
    finally:
        helper.stop()
    for _ in range(500):
        if "exited" in (tmp_path / "state.txt.cmds").read_text():
            break
        time.sleep(0.01)
    assert "quit" in (tmp_path / "state.txt.cmds").read_text() and not helper.alive


@pytest.mark.skipif(os.name == "nt", reason="the stand-in is a POSIX script")
def test_a_killed_process_is_noticed_and_restarted(fake_exe, tmp_path):
    helper = TapHelper(exe=str(fake_exe), state=tmp_path / "state.txt")
    try:
        helper.ensure_running()
        helper._proc.kill()
        helper._proc.wait()
        assert not helper.alive
        assert helper.ensure_running() and helper.restarts == 1 and helper.alive
    finally:
        helper.stop()


def test_restoring_volumes_runs_the_helper_with_the_state_file(tmp_path):
    calls = []
    assert tapmod.restore_volumes("/fake/asm-audiotap.exe", run=lambda cmd, **kw: calls.append((cmd, kw)))
    cmd, kw = calls[0]
    assert cmd[:2] == ["/fake/asm-audiotap.exe", "--restore"] and cmd[-1].endswith("audiotap_state.txt") and kw["timeout"]
    assert not tapmod.restore_volumes("")

    def boom(*a, **k):
        raise OSError("gone")
    assert not tapmod.restore_volumes("/x", run=boom)


def test_the_helper_is_found_where_the_installer_puts_it(tmp_path, monkeypatch):
    exe = tmp_path / tapmod.HELPER_NAME
    exe.write_text("")
    monkeypatch.setenv("ASM_AUDIOTAP", str(exe))
    assert tapmod.find_helper() == str(exe)
    monkeypatch.delenv("ASM_AUDIOTAP")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "ASM.exe"))
    assert tapmod.find_helper() == str(exe)


# ── the engine with the tap ──────────────────────────────────────────────────

def dev(index, name, ins=0, outs=0):
    return DeviceInfo(index, name, "Windows WASAPI", ins, outs, 48000.0)


DEVICES = [
    dev(1, "CABLE Output (VB-Audio Virtual Cable)", ins=2), dev(2, "CABLE Input (VB-Audio Virtual Cable)", outs=2),
    dev(7, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2),
    dev(8, "Headset Microphone (Arctis Nova Pro Wireless)", ins=1),
    dev(9, "Speakers (Realtek(R) Audio)", outs=2),
]


def tap_config(**kw):
    cfg = EngineConfig(inputs={c: tap_device_name(c) for c in ("game", "chat", "media", "aux")},
                       outputs={"headset": "Arctis Nova Pro"}, mic_input="Microphone (Arctis",
                       mic_output="CABLE Input", latency="low")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class Rig:
    def __init__(self, tmp_path, cfg=None, devices=None):
        for suffix in ("", "_media", "_aux"):
            (tmp_path / f"sonar_spatial_audio{suffix}.json").write_text(json.dumps({"enabled": False}))
        self.t = 0
        self.real = FakeBackend(devices or DEVICES)
        self.helper, self.procs = make_helper(state=tmp_path / "s.txt")
        self.backend = TapBackend(self.real, self.helper)
        self.engine = Engine(cfg or tap_config(), self.backend, config_dir=tmp_path, port_clock=lambda: self.t / RATE)
        self.heard = []

    def start(self):
        self.engine.start(threaded=False)

    def commands(self):
        return self.procs[-1].commands() if self.procs else []

    def tick(self, n=1, game=None, chat=None):
        for _ in range(n):
            block = np.zeros((CHUNK, 8), dtype=np.float32)
            idx = np.arange(self.t, self.t + CHUNK)
            if game is not None:
                block[:, 0] = block[:, 1] = (np.sin(2 * np.pi * 1000 * idx / RATE) * game).astype(np.float32)
            if chat is not None:
                block[:, 2] = block[:, 3] = (np.sin(2 * np.pi * 440 * idx / RATE) * chat).astype(np.float32)
            self.helper._dispatch(block)
            try:
                out = self.real.stream("Arctis", "output")
                if out.running:
                    self.heard.append(out.collect(CHUNK))
            except KeyError:
                pass
            self.engine.playback.step()
            self.t += CHUNK

    def level(self, seconds=0.3):
        data = np.concatenate(self.heard)[-int(seconds * RATE):] if self.heard else np.zeros((1, 2))
        return float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))


def test_the_engine_reads_the_tap_like_four_capture_devices(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    status = rig.engine.status()
    for ch in ("game", "chat", "media", "aux"):
        assert status["endpoints"][f"in:{ch}"]["state"] == "running" and status["endpoints"][f"in:{ch}"]["channels"] == 2
    assert status["tap"]["available"] is True and len(rig.procs) == 1


def test_a_programs_sound_comes_out_of_the_headset_and_channels_stay_apart(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.tick(80, game=0.2)
    assert 0.1 < rig.level() < 0.2
    rig.heard.clear()
    rig.tick(80)                                                   # nothing playing: silence, not leftovers
    assert rig.level(0.2) < 1e-4
    rig.engine.set_mute("game", True)
    rig.heard.clear()
    rig.tick(60, game=0.2, chat=0.2)
    assert 0.1 < rig.level(0.2) < 0.2                              # game muted: only chat is left


def test_programs_are_turned_down_only_while_the_engine_plays_to_the_headset(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    sent = rig.commands()
    # the headset, and the virtual cable's Input end for people who pick the cable as Windows' output
    assert "engage\t1" in sent and "endpoint\tHeadset Earphone (Arctis Nova Pro Wireless)\tCABLE Input (VB-Audio Virtual Cable)" in sent
    assert f"exclude\t{os.getpid()}" in sent                       # the engine's own sound is never captured back
    rig.engine.stop()
    assert "engage\t0" in rig.commands()
    rig.helper.stop()


def test_the_cable_end_is_left_alone_when_tapping_cables_is_switched_off(tmp_path):
    rig = Rig(tmp_path, tap_config(tap_cables=False))
    rig.start()
    assert "endpoint\tHeadset Earphone (Arctis Nova Pro Wireless)" in rig.commands()
    rig.engine.stop()
    rig.helper.stop()


def test_a_headset_that_cannot_be_opened_never_silences_the_programs(tmp_path):
    rig = Rig(tmp_path)
    rig.real.fail_open.add("Headset Earphone (Arctis Nova Pro Wireless)")
    rig.start()
    assert "engage\t1" not in rig.commands()
    assert rig.helper.engaged is False


def test_the_tap_lets_go_when_the_headset_goes_away_and_takes_over_again_when_it_is_back(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.commands()
    headset = rig.engine.endpoints["out:headset"]
    rig.engine._close(headset)
    rig.real.fail_open.add("Headset Earphone (Arctis Nova Pro Wireless)")       # switched off: the device cannot be opened
    rig.engine.supervise(now=100.0)
    assert headset.state == "waiting" and rig.helper.engaged is False and "engage\t0" in rig.commands()
    rig.real.fail_open.clear()
    rig.engine.supervise(now=200.0)
    assert headset.state == "running" and rig.helper.engaged is True and "engage\t1" in rig.commands()
    rig.helper.stop()


def test_routing_a_program_is_remembered_and_sent_to_the_helper(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.commands()
    assert rig.engine.handle({"cmd": "route", "exe": "Foo.exe", "channel": "chat"}) == {}
    assert "rule\tfoo.exe\tchat" in rig.commands()
    assert load_rules(tmp_path).user == {"foo.exe": "chat"}
    assert rig.engine.handle({"cmd": "unroute", "exe": "foo.exe"}) == {}
    assert load_rules(tmp_path).user == {}
    assert rig.engine.handle({"cmd": "route", "exe": "foo.exe", "channel": "ceiling"})["ok"] is False
    assert rig.engine.handle({"cmd": "route", "exe": "", "channel": "chat"})["ok"] is False


def test_routes_saved_earlier_are_in_force_at_the_next_start(tmp_path):
    save_rules(AppRules(user={"foo.exe": "aux"}), tmp_path)
    rig = Rig(tmp_path)
    rig.start()
    assert "rule\tfoo.exe\taux" in rig.commands()


def test_without_the_tap_routing_is_politely_refused(tmp_path):
    engine = Engine(tap_config(inputs={"game": "CABLE Output"}), FakeBackend(DEVICES), config_dir=tmp_path)
    assert engine.tap is None and engine.handle({"cmd": "route", "exe": "a.exe", "channel": "chat"})["ok"] is False
    assert engine.status()["tap"] is None


def test_the_engine_stops_cleanly_and_releases_the_helper(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.engine.stop()
    assert rig.helper._refs == 0
    rig.helper.stop()


def test_a_first_guess_uses_the_tap_for_channels_and_the_cable_for_the_microphone():
    cfg = suggest_config(DEVICES + tap_devices())
    assert cfg.inputs == {c: tap_device_name(c) for c in ("game", "chat", "media", "aux")}
    assert cfg.mic_output == "CABLE Input (VB-Audio Virtual Cable)" and cfg.mic_input.startswith("Headset Microphone")
    assert cfg.outputs["headset"].startswith("Headset Earphone") and cfg.problems() == []


def test_a_first_guess_without_the_tap_is_what_it_was():
    cfg = suggest_config(DEVICES)
    assert cfg.inputs["game"].startswith("CABLE Output") and cfg.inputs["chat"] == ""
