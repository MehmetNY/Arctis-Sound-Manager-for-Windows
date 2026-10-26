# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""The window's two polling loops: never two of a kind, never in the commands' way, quieter while the daemon is away."""
from __future__ import annotations

import logging
import threading
import time

import pytest

from arctis_sound_manager.gui import dbus_wrapper as dw
from arctis_sound_manager.gui.dbus_wrapper import DbusWrapper


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


class Loops:
    """Stands in for the real loops: records which ones are running, until they are told to end."""

    def __init__(self, monkeypatch):
        self.running: list[str] = []
        self.started: list[str] = []
        self.threads: list[str] = []
        self.lock = threading.Lock()
        monkeypatch.setattr(DbusWrapper, "request_status_thread", self._make("status"))
        monkeypatch.setattr(DbusWrapper, "request_settings_thread", self._make("settings"))

    def _make(self, kind):
        def loop(wrapper, frequency_seconds, keep_going=None):
            tag = f"{kind}#{len(self.started)}"
            with self.lock:
                self.started.append(tag)
                self.running.append(tag)
                self.threads.append(threading.current_thread().name)
            try:
                while keep_going():
                    time.sleep(0.005)
                    if frequency_seconds == 0:                # a one-shot: one round and out, like the real loop
                        break
            finally:
                with self.lock:
                    self.running.remove(tag)
        return loop

    def alive(self, kind):
        return [t for t in self.running if t.startswith(kind)]


@pytest.fixture
def loops(monkeypatch):
    return Loops(monkeypatch)


def test_a_window_hidden_and_shown_at_once_has_one_loop_of_each_kind(loops):
    wrapper = DbusWrapper()
    wrapper.request_settings()
    wrapper.request_status()
    assert _wait(lambda: len(loops.running) == 2)
    for _ in range(5):                                        # minimise, restore, minimise, restore ... within a second
        wrapper.stop()
        wrapper.request_settings()
        wrapper.request_status()
    assert _wait(lambda: len(loops.started) == 12)
    assert _wait(lambda: len(loops.alive("status")) == 1 and len(loops.alive("settings")) == 1)
    time.sleep(0.1)
    assert len(loops.running) == 2
    wrapper.stop()
    assert _wait(lambda: loops.running == [])


def test_starting_status_does_not_end_the_settings_loop(loops):
    wrapper = DbusWrapper()
    wrapper.request_settings()
    assert _wait(lambda: len(loops.alive("settings")) == 1)
    wrapper.request_status()
    assert _wait(lambda: len(loops.alive("status")) == 1)
    time.sleep(0.1)
    assert len(loops.alive("settings")) == 1
    wrapper.stop()
    assert _wait(lambda: loops.running == [])


def test_the_loops_have_their_own_threads_and_commands_are_not_held_up(loops):
    wrapper = DbusWrapper()
    wrapper.request_settings()
    wrapper.request_status()
    assert _wait(lambda: len(loops.running) == 2)
    assert all(name.startswith("asm-poll") for name in loops.threads)
    assert DbusWrapper._executor.submit(lambda: "done").result(timeout=2) == "done"
    wrapper.stop()
    assert _wait(lambda: loops.running == [])


def test_two_windows_do_not_end_each_others_loops(loops):
    first, second = DbusWrapper(), DbusWrapper()
    first.request_status()
    second.request_status()
    assert _wait(lambda: len(loops.alive("status")) == 2)
    first.stop()
    assert _wait(lambda: len(loops.alive("status")) == 1)
    second.stop()
    assert _wait(lambda: loops.running == [])


def test_one_answer_on_request_does_not_disturb_a_running_loop(loops):
    wrapper = DbusWrapper()
    wrapper.request_status()
    assert _wait(lambda: len(loops.alive("status")) == 1)
    wrapper._stopping = True                                  # a one-shot asks while the wrapper is stopped
    stopped = threading.Event()
    wrapper._executor.submit(lambda: stopped.set())
    assert stopped.wait(2)
    wrapper.request_status(one_time=True)
    assert _wait(lambda: len(loops.started) == 2)
    wrapper.stop()
    assert _wait(lambda: loops.running == [])


# ── a daemon that does not answer ────────────────────────────────────────────

class _NoDaemon:
    attempts = 0

    async def connect(self):
        type(self).attempts += 1
        raise ConnectionRefusedError("no daemon")


def test_a_missing_daemon_is_asked_less_and_less_often_and_reported_once(monkeypatch, caplog):
    import asyncio
    _NoDaemon.attempts = 0
    monkeypatch.setattr(dw, "MessageBus", _NoDaemon)
    monkeypatch.setattr(DbusWrapper, "_FAILING_POLL_MAX_SECONDS", 0.3)
    wrapper = DbusWrapper()
    stop_at = time.monotonic() + 1.5
    caplog.set_level(logging.DEBUG, logger="DbusWrapper")
    asyncio.run(wrapper.dbus_request_async(wrapper.sig_status, 0.01, "org.example.Daemon", "/org/example", "org.example.If", "GetStatus",
                                           keep_going=lambda: time.monotonic() < stop_at))
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "further failures are not logged" in errors[0].getMessage()
    assert 6 <= _NoDaemon.attempts <= 14, _NoDaemon.attempts     # steady 10 ms polling would have made ~150


def test_a_daemon_that_comes_back_is_polled_at_the_normal_rate_and_a_new_failure_is_reported(monkeypatch, caplog):
    import asyncio

    class Flaky:
        mode = "down"
        calls = 0

        async def connect(self):
            Flaky.calls += 1
            if Flaky.mode == "down":
                raise ConnectionRefusedError("no daemon")
            return self

        async def call(self, message):
            class Reply:
                message_type = dw.MessageType.METHOD_RETURN
                body = ['{"a": 1}']
            return Reply()

        def disconnect(self):
            pass

    monkeypatch.setattr(dw, "MessageBus", Flaky)
    monkeypatch.setattr(DbusWrapper, "_FAILING_POLL_MAX_SECONDS", 0.05)
    wrapper = DbusWrapper()
    got = []
    wrapper.sig_status.connect(got.append)
    caplog.set_level(logging.DEBUG, logger="DbusWrapper")
    state = {"phase": 0}
    start = time.monotonic()

    def keep_going():
        t = time.monotonic() - start
        if t > 0.3 and state["phase"] == 0:
            Flaky.mode = "up"
            state["phase"] = 1
        if t > 0.8 and state["phase"] == 1:
            Flaky.mode = "down"
            state["phase"] = 2
        return t < 1.3

    asyncio.run(wrapper.dbus_request_async(wrapper.sig_status, 0.01, "org.example.Daemon", "/org/example", "org.example.If", "GetStatus", keep_going=keep_going))
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2                                   # one per outage
    assert len(got) >= 20                                     # and ~100 Hz again in between (no back-off left over)


def test_the_nap_ends_as_soon_as_the_loop_is_told_to(monkeypatch):
    wrapper = DbusWrapper()
    flag = {"on": True}
    threading.Timer(0.2, lambda: flag.update(on=False)).start()
    start = time.monotonic()
    wrapper._poll_nap(30.0, lambda: flag["on"])
    assert time.monotonic() - start < 2.0
