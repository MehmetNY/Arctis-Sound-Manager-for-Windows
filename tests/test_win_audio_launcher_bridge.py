"""Starting the engine process, and the non-blocking bridge to its control channel."""
from __future__ import annotations

import threading
import time

import pytest

from arctis_sound_manager.win.audio import launcher
from arctis_sound_manager.win.audio.bridge import EngineBridge
from arctis_sound_manager.win.audio.control import ControlServer, EngineClient, EngineUnavailable


class FakeClient:
    """Stands in for EngineClient: answers from a script, records what it was asked."""

    def __init__(self, up=True, delay=0.0):
        self.up, self.delay = up, delay
        self.calls: list[tuple[str, dict]] = []
        self.status_reply = {"ok": True, "status": {"running": True, "volumes": {"game": 100}}}
        self.gate = None

    def request(self, cmd, **fields):
        if self.gate is not None:
            self.gate.wait(5)
        if self.delay:
            time.sleep(self.delay)
        if not self.up:
            raise EngineUnavailable("down")
        self.calls.append((cmd, fields))
        return {"ok": True}

    def status(self):
        if not self.up:
            raise EngineUnavailable("down")
        return self.status_reply

    def available(self):
        return self.up


# ── launcher ─────────────────────────────────────────────────────────────────

def test_the_engine_command_prefers_the_windowless_interpreter(tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    assert launcher.engine_command(str(tmp_path / "python.exe")) == [str(tmp_path / "pythonw.exe"), "-m", launcher.MODULE]
    (tmp_path / "pythonw.exe").unlink()
    assert launcher.engine_command(str(tmp_path / "python.exe"))[0] == str(tmp_path / "python.exe")
    assert launcher.engine_command("/usr/bin/python3")[0] == "/usr/bin/python3"


def test_a_running_engine_is_not_started_twice():
    started = []
    assert launcher.start_engine(client=FakeClient(up=True), popen=lambda *a, **k: started.append(a))
    assert started == []


def test_start_launches_a_detached_process_and_waits_until_it_answers():
    client = FakeClient(up=False)
    spawned = []

    def popen(cmd, **kwargs):
        spawned.append((cmd, kwargs))
    ticks = {"n": 0}

    def sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 3:
            client.up = True                                     # the engine comes up after a moment
    now = {"t": 0.0}

    def clock():
        now["t"] += 0.05
        return now["t"]
    assert launcher.start_engine(config_dir=None, client=client, popen=popen, sleep=sleep, clock=clock, wait=5.0)
    (cmd, kwargs), = spawned
    assert cmd[1:3] == ["-m", launcher.MODULE]
    assert kwargs["close_fds"] is True and kwargs["stdout"] is not None
    assert kwargs.get("start_new_session") is True or "creationflags" in kwargs      # detached either way


def test_start_gives_up_after_the_wait_and_reports_it():
    now = {"t": 0.0}

    def clock():
        now["t"] += 0.5
        return now["t"]
    assert not launcher.start_engine(client=FakeClient(up=False), popen=lambda *a, **k: None,
                                     sleep=lambda s: None, clock=clock, wait=3.0)


def test_a_missing_interpreter_is_reported_not_raised():
    def popen(*a, **k):
        raise FileNotFoundError("no python")
    assert not launcher.start_engine(client=FakeClient(up=False), popen=popen, sleep=lambda s: None)


def test_extra_arguments_and_the_config_folder_reach_the_command_line(tmp_path):
    seen = []
    launcher.start_engine(config_dir=tmp_path, client=FakeClient(up=False), popen=lambda cmd, **k: seen.append(cmd),
                          sleep=lambda s: None, wait=0.0, extra_args=["-v"])
    assert seen[0][-3:] == ["--config-dir", str(tmp_path), "-v"]


def test_stop_engine_with_and_without_an_engine():
    assert launcher.stop_engine(client=_ShutdownClient(True))
    assert not launcher.stop_engine(client=_ShutdownClient(False))


class _ShutdownClient:
    def __init__(self, up):
        self.up = up

    def shutdown(self):
        if not self.up:
            raise EngineUnavailable("down")
        return {"ok": True}


# ── bridge ───────────────────────────────────────────────────────────────────

def test_commands_are_delivered_in_the_background():
    client = FakeClient()
    bridge = EngineBridge(client=client)
    try:
        bridge.volume("game", 40)
        bridge.mute("chat", True)
        bridge.chatmix(30, 100, ["game", "media"])
        bridge.reload()
        assert bridge.flush()
    finally:
        bridge.close()
    assert [c for c, _ in client.calls] == ["volume", "mute", "chatmix", "reload"]
    assert client.calls[2][1] == {"channels_pct": 30.0, "chat_pct": 100.0, "channels": ["game", "media"]}


def test_only_the_latest_command_of_a_kind_is_sent_while_the_engine_is_busy():
    client = FakeClient()
    client.gate = threading.Event()                              # the first delivery blocks (a slow engine)
    bridge = EngineBridge(client=client)
    try:
        bridge.volume("game", 1)
        time.sleep(0.1)                                          # the sender is now stuck inside that call
        for pct in range(2, 50):
            bridge.volume("game", pct)                           # a slider being dragged
        bridge.volume("chat", 7)
        client.gate.set()
        assert bridge.flush()
    finally:
        bridge.close()
    game = [f["pct"] for c, f in client.calls if f.get("channel") == "game"]
    assert game == [1.0, 49.0]                                   # the one in flight, then only the last
    assert ("volume", {"channel": "chat", "pct": 7.0}) in client.calls


def test_commands_keep_their_order_of_arrival_across_kinds():
    client = FakeClient()
    client.gate = threading.Event()
    bridge = EngineBridge(client=client)
    try:
        bridge.reload()
        time.sleep(0.1)
        bridge.volume("game", 10)
        bridge.chatmix(10, 100)
        bridge.volume("game", 20)                                # replaces game's, and moves it after chatmix
        client.gate.set()
        assert bridge.flush()
    finally:
        bridge.close()
    assert [c for c, _ in client.calls] == ["reload", "chatmix", "volume"]


def test_without_an_engine_commands_are_dropped_quietly():
    client = FakeClient(up=False)
    bridge = EngineBridge(client=client)
    try:
        bridge.volume("game", 10)
        assert bridge.flush()
        assert bridge.dropped == 1 and bridge.sent == 0
        assert bridge.status() is None or isinstance(bridge.status(), dict)      # no exception
    finally:
        bridge.close()


def test_status_is_cached_and_the_poller_follows_the_engine():
    client = FakeClient()
    bridge = EngineBridge(client=client, poll_interval=0.02)
    try:
        assert bridge.status() is None or isinstance(bridge.status(), dict)   # first call never blocks
        deadline = time.time() + 3
        while bridge.status() is None and time.time() < deadline:
            time.sleep(0.01)
        assert bridge.status()["volumes"] == {"game": 100} and bridge.running()
        client.up = False
        deadline = time.time() + 3
        while bridge.status() is not None and time.time() < deadline:
            time.sleep(0.01)
        assert bridge.status() is None and not bridge.running()
    finally:
        bridge.close()


def test_the_poller_goes_idle_when_nobody_looks():
    client = FakeClient()
    polls = []
    orig = client.status
    client.status = lambda: (polls.append(1), orig())[1]
    bridge = EngineBridge(client=client, poll_interval=0.01, idle_after=0.1)
    try:
        bridge.status()
        time.sleep(0.4)                                          # looked once; the poller stops after idle_after
        n = len(polls)
        assert 1 <= n < 40
        time.sleep(0.3)
        assert len(polls) == n
        bridge.status()                                          # looking again wakes it up
        time.sleep(0.2)
        assert len(polls) > n
    finally:
        bridge.close()


def test_a_command_sent_while_the_poller_is_idle_is_not_lost():
    client = FakeClient()
    bridge = EngineBridge(client=client, poll_interval=0.01, idle_after=0.05)
    try:
        bridge.status()
        time.sleep(0.3)                                          # the thread is asleep with no timeout
        bridge.volume("aux", 5)
        assert bridge.flush(timeout=2.0)
        assert ("volume", {"channel": "aux", "pct": 5.0}) in client.calls
    finally:
        bridge.close()


def test_the_bridge_talks_to_a_real_control_server(tmp_path):
    received = []

    def handler(request):
        received.append({k: v for k, v in request.items() if k != "token"})
        if request["cmd"] == "status":
            return {"status": {"running": True, "volumes": {"game": 55}}}
        return {}
    server = ControlServer(handler, tmp_path)
    server.start()
    bridge = EngineBridge(client=EngineClient(tmp_path, timeout=2.0), poll_interval=0.02)
    try:
        bridge.volume("game", 55)
        bridge.reload()
        assert bridge.flush()
        deadline = time.time() + 3
        while not bridge.running() and time.time() < deadline:
            time.sleep(0.01)
        assert bridge.status()["volumes"]["game"] == 55
        assert {"cmd": "volume", "channel": "game", "pct": 55.0} in received and {"cmd": "reload"} in received
    finally:
        bridge.close()
        server.stop()


def test_close_stops_the_thread():
    bridge = EngineBridge(client=FakeClient())
    bridge.volume("game", 1)
    assert bridge.flush()
    thread = bridge._thread
    bridge.close()
    assert not thread.is_alive()


def _count_loop_iterations(bridge):
    n = {"v": 0}
    orig = bridge._take

    def counting():
        n["v"] += 1
        return orig()
    bridge._take = counting
    return n


def test_an_idle_sender_sleeps_instead_of_spinning():
    bridge = EngineBridge(client=FakeClient())
    try:
        bridge.volume("game", 1)
        assert bridge.flush()
        n = _count_loop_iterations(bridge)
        time.sleep(0.5)
        assert n["v"] <= 2, n["v"]
    finally:
        bridge.close()


def test_a_watching_poller_wakes_only_when_a_poll_is_due():
    bridge = EngineBridge(client=FakeClient(), poll_interval=0.25, idle_after=5.0)
    try:
        bridge.status()
        time.sleep(0.15)
        n = _count_loop_iterations(bridge)
        time.sleep(0.6)
        assert n["v"] <= 6, n["v"]                               # ~2-3 polls, not a hundred 10 ms wake-ups
    finally:
        bridge.close()


def test_a_command_that_finds_no_engine_forgets_the_cached_status():
    client = FakeClient()
    bridge = EngineBridge(client=client, poll_interval=0.02, idle_after=0.05)
    try:
        deadline = time.time() + 3
        while bridge.status() is None and time.time() < deadline:
            time.sleep(0.01)
        assert bridge.running()
        time.sleep(0.3)                                          # nobody looks: the poller sleeps
        client.up = False                                        # ... and the engine dies meanwhile
        bridge.volume("game", 3)
        assert bridge.flush()
        assert bridge._status is None
    finally:
        bridge.close()
