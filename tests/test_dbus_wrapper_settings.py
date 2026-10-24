# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""A setting the user changed must stay changed.

Reported on the Windows build: DAC settings (screen timeout, "turn off while the session is locked
or idle") "change themselves", and a toggle switched off comes back on. Two causes, both in how the
GUI talks to the daemon:

* every change went out as its own call on one of four pool threads, so a slider drag reached the
  daemon in no particular order - the last value to arrive was not the last one chosen;
* the once-a-second GetSettings poll redrew every page, and a reply read just before the change
  landed carried the old value, so the control flipped back until the next poll.
"""
from __future__ import annotations

import asyncio
import json
import random
import threading
import time

import pytest
from dbus_next.constants import MessageType
from PySide6.QtCore import Qt

from arctis_sound_manager.gui import dbus_wrapper as dw
from arctis_sound_manager.gui.dbus_wrapper import DbusWrapper


@pytest.fixture(autouse=True)
def clean_wrapper_state():
    def wipe():
        with DbusWrapper._setting_lock:
            DbusWrapper._setting_held.clear()
            DbusWrapper._setting_queued.clear()
            DbusWrapper._setting_senders.clear()
    wipe()
    yield
    wipe()


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _idle():
    with DbusWrapper._setting_lock:
        return not DbusWrapper._setting_senders and not DbusWrapper._setting_queued


# ── ordering ─────────────────────────────────────────────────────────────────

def test_values_of_one_setting_reach_the_daemon_in_the_order_they_were_chosen(monkeypatch):
    sent, in_flight, overlap = [], [0], [0]
    lock = threading.Lock()

    async def fake_send(name, value):
        with lock:
            in_flight[0] += 1
            overlap[0] = max(overlap[0], in_flight[0])
        await asyncio.sleep(random.uniform(0.0, 0.02))   # the daemon is slow, and unevenly so
        with lock:
            sent.append(value)
            in_flight[0] -= 1
        return True

    monkeypatch.setattr(DbusWrapper, "change_setting_async", staticmethod(fake_send))

    chosen = list(range(10, 310, 10))                      # a drag across the whole screen-timeout slider
    for value in chosen:
        DbusWrapper.change_setting("oled_screen_timeout", value)
    assert _wait(_idle)

    assert overlap[0] == 1, "two calls for the same setting were in flight at once, so they can overtake each other"
    assert sent == sorted(sent), f"values arrived out of order: {sent}"
    assert sent[-1] == chosen[-1], "the last value the user chose must be the last one the daemon gets"
    assert len(sent) < len(chosen), "a fast drag should collapse to the values the daemon can keep up with"


def test_different_settings_do_not_wait_for_each_other(monkeypatch):
    both_in_flight = threading.Barrier(2, timeout=3)
    released = []

    async def fake_send(name, value):
        try:
            await asyncio.get_running_loop().run_in_executor(None, both_in_flight.wait)
        except threading.BrokenBarrierError:
            return False
        released.append(name)
        return True

    monkeypatch.setattr(DbusWrapper, "change_setting_async", staticmethod(fake_send))
    DbusWrapper.change_setting("oled_brightness", 3)
    DbusWrapper.change_setting("oled_screen_timeout", 60)
    assert _wait(lambda: len(released) == 2), "one setting blocked the other"
    assert _wait(_idle)


def test_a_failed_send_does_not_wedge_the_setting(monkeypatch):
    calls = []

    async def fake_send(name, value):
        calls.append(value)
        if len(calls) == 1:
            raise RuntimeError("daemon went away")
        return True

    monkeypatch.setattr(DbusWrapper, "change_setting_async", staticmethod(fake_send))
    DbusWrapper.change_setting("oled_brightness", 1)
    assert _wait(_idle)
    DbusWrapper.change_setting("oled_brightness", 2)
    assert _wait(_idle)
    assert calls == [1, 2]


# ── a stale poll must not undo the change ────────────────────────────────────

def _poll_reply(value):
    """What GetSettings returns while the daemon still holds *value* for the toggle."""
    return {"general": {"oled_off_when_away": value, "theme": "x"},
            "dac": {"oled_off_when_away": value},
            "device": {}}


def test_a_poll_read_before_the_change_landed_does_not_flip_the_control_back(monkeypatch):
    release = threading.Event()

    async def slow_send(name, value):
        await asyncio.get_running_loop().run_in_executor(None, release.wait)
        return True

    monkeypatch.setattr(DbusWrapper, "change_setting_async", staticmethod(slow_send))
    DbusWrapper.change_setting("oled_off_when_away", False)

    try:
        stale = DbusWrapper._overlay_held_settings(_poll_reply(True))
        assert stale["general"]["oled_off_when_away"] is False
        assert stale["dac"]["oled_off_when_away"] is False
        assert stale["general"]["theme"] == "x", "unrelated settings must pass through untouched"
    finally:
        release.set()      # a failed assertion must not leave a sender thread blocked for ever
    assert _wait(_idle)


def test_the_hold_ends_once_the_daemon_reports_the_new_value(monkeypatch):
    async def fast_send(name, value):
        return True

    monkeypatch.setattr(DbusWrapper, "change_setting_async", staticmethod(fast_send))
    DbusWrapper.change_setting("oled_off_when_away", False)
    assert _wait(_idle)

    DbusWrapper._overlay_held_settings(_poll_reply(False))
    assert "oled_off_when_away" not in DbusWrapper._setting_held

    # ... and from then on the daemon is the authority again (e.g. a profile or the OLED itself changes it)
    assert DbusWrapper._overlay_held_settings(_poll_reply(True))["general"]["oled_off_when_away"] is True


def test_a_refused_value_is_not_held(monkeypatch, caplog):
    async def refusing_send(name, value):
        return False

    monkeypatch.setattr(DbusWrapper, "change_setting_async", staticmethod(refusing_send))
    DbusWrapper.change_setting("oled_off_when_away", False)
    assert _wait(_idle)

    shown = DbusWrapper._overlay_held_settings(_poll_reply(True))
    assert shown["general"]["oled_off_when_away"] is True, "the UI must go back to what the daemon really has"


def test_the_hold_runs_out(monkeypatch):
    async def never_confirmed(name, value):
        return True

    monkeypatch.setattr(DbusWrapper, "change_setting_async", staticmethod(never_confirmed))
    monkeypatch.setattr(DbusWrapper, "_SETTING_HOLD_SECONDS", 0.05)
    DbusWrapper.change_setting("oled_off_when_away", False)
    assert _wait(_idle)
    time.sleep(0.1)
    assert DbusWrapper._overlay_held_settings(_poll_reply(True))["general"]["oled_off_when_away"] is True


def test_a_list_setting_is_held_as_a_copy(monkeypatch):
    async def slow(name, value):
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(DbusWrapper, "change_setting_async", staticmethod(slow))
    order = ["profile", "eq"]
    DbusWrapper.change_setting("oled_display_order", order)
    order.append("media")          # the page keeps editing its own list
    shown = DbusWrapper._overlay_held_settings({"dac": {"oled_display_order": ["eq", "profile"]}, "general": {}})
    assert shown["dac"]["oled_display_order"] == ["profile", "eq"]
    assert _wait(_idle)


# ── what the real call reports ───────────────────────────────────────────────

class _Reply:
    def __init__(self, message_type, body):
        self.message_type, self.body = message_type, body


def _fake_bus(reply=None, error=None):
    class Bus:
        async def connect(self):
            return self

        async def call(self, message):
            if error:
                raise error
            return reply

        def disconnect(self):
            pass
    return Bus


@pytest.mark.parametrize("reply, error, expected", [
    (_Reply(MessageType.METHOD_RETURN, [True]), None, True),
    (_Reply(MessageType.METHOD_RETURN, [False]), None, False),      # the daemon validated and refused
    (_Reply(MessageType.ERROR, ["boom"]), None, False),
    (None, None, False),
    (None, OSError("daemon not running"), False),
])
def test_change_setting_reports_whether_the_daemon_accepted(monkeypatch, reply, error, expected):
    monkeypatch.setattr(dw, "MessageBus", _fake_bus(reply, error))
    assert asyncio.run(DbusWrapper.change_setting_async("oled_brightness", 5)) is expected


# ── end to end: poller + a daemon that takes its time ────────────────────────

class _SlowDaemon:
    """GetSettings / SetSetting with a latency, like the Windows IPC (new connection per call)."""

    def __init__(self, latency):
        self.state = {"oled_off_when_away": True, "oled_screen_timeout": 30}
        self.latency = latency
        self.applied = []

    def bus_class(self):
        daemon = self

        class Bus:
            async def connect(self):
                return self

            def disconnect(self):
                pass

            async def call(self, message):
                if message.member == "GetSettings":
                    snapshot = dict(daemon.state)          # read NOW; delivered after the latency below
                    await asyncio.sleep(daemon.latency / 4)
                    return _Reply(MessageType.METHOD_RETURN,
                                  [json.dumps({"general": dict(snapshot), "dac": dict(snapshot), "device": {}})])
                if message.member == "SetSetting":
                    await asyncio.sleep(daemon.latency)
                    name, value = message.body[0], json.loads(message.body[1])
                    daemon.state[name] = value
                    daemon.applied.append((name, value))
                    return _Reply(MessageType.METHOD_RETURN, [True])
                raise AssertionError(message.member)
        return Bus


def test_no_poll_shows_the_old_value_after_the_user_changed_it(monkeypatch):
    daemon = _SlowDaemon(latency=0.4)
    monkeypatch.setattr(dw, "MessageBus", daemon.bus_class())

    wrapper = DbusWrapper()
    seen = []                                   # (time, toggle value, timeout value) of every poll result
    start = time.monotonic()
    wrapper.sig_settings.connect(
        lambda payload: seen.append((time.monotonic() - start,
                                     payload["dac"]["oled_off_when_away"],
                                     payload["general"]["oled_screen_timeout"])),
        Qt.ConnectionType.DirectConnection)
    poller = threading.Thread(target=wrapper.request_settings_thread, kwargs={"frequency_seconds": 0.03}, daemon=True)
    poller.start()
    try:
        assert _wait(lambda: len(seen) >= 3)
        changed_at = time.monotonic() - start
        DbusWrapper.change_setting("oled_off_when_away", False)
        DbusWrapper.change_setting("oled_screen_timeout", 90)
        assert _wait(lambda: len(daemon.applied) == 2, 5)
        time.sleep(0.4)                         # a few more polls after the daemon has caught up
    finally:
        wrapper._stopping = True
        poller.join(3)

    after = [(t, toggle, timeout) for t, toggle, timeout in seen if t > changed_at]
    assert len(after) > 5, "the poller did not keep running"
    assert [toggle for _, toggle, _ in after] == [False] * len(after), "the toggle flipped back to ON"
    assert [timeout for _, _, timeout in after] == [90] * len(after), "the timeout jumped back"
    assert daemon.state == {"oled_off_when_away": False, "oled_screen_timeout": 90}
