# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading the equaliser bands: one read at a time, everybody who asked gets the answer, a missing daemon is logged rarely."""
from __future__ import annotations

import logging
import threading
import time

import pytest

from arctis_sound_manager.gui.dbus_wrapper import DbusWrapper


class _Sink:
    def __init__(self):
        self.got: list = []

    def emit(self, value):
        self.got.append(value)


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(DbusWrapper, "_eq_waiters", [])
    monkeypatch.setattr(DbusWrapper, "_eq_running", False)
    monkeypatch.setattr(DbusWrapper, "_eq_last_failure", 0.0)


def test_asks_that_arrive_during_a_read_share_one_read(monkeypatch):
    reads = []
    release = threading.Event()

    async def slow_read():
        reads.append(1)
        while not release.is_set():
            import asyncio
            await asyncio.sleep(0.005)
        return [1, 2, 3]

    monkeypatch.setattr(DbusWrapper, "_read_eq_bands", staticmethod(slow_read))
    a, b, c = _Sink(), _Sink(), _Sink()
    DbusWrapper.get_eq_bands(a)
    assert _wait(lambda: len(reads) == 1)
    DbusWrapper.get_eq_bands(b)                  # arrive while the first read is still going
    DbusWrapper.get_eq_bands(c)
    release.set()
    assert _wait(lambda: a.got and b.got and c.got)
    assert a.got[0] == b.got[0] == c.got[0] == [1, 2, 3]
    assert _wait(lambda: not DbusWrapper._eq_running)
    assert len(reads) <= 2                       # b and c cannot cost a read each


def test_a_burst_of_asks_never_queues_a_job_per_ask(monkeypatch):
    submitted = []
    real = DbusWrapper._executor

    class Counting:
        def submit(self, fn, *a, **k):
            submitted.append(fn)
            return real.submit(fn, *a, **k)

    release = threading.Event()

    async def slow_read():
        import asyncio
        while not release.is_set():
            await asyncio.sleep(0.005)
        return [0] * 10

    monkeypatch.setattr(DbusWrapper, "_executor", Counting())
    monkeypatch.setattr(DbusWrapper, "_read_eq_bands", staticmethod(slow_read))
    sink = _Sink()
    for _ in range(200):                         # 200 timer ticks while the daemon does not answer
        DbusWrapper.get_eq_bands(sink)
    assert len(submitted) == 1
    assert len(DbusWrapper._eq_waiters) <= DbusWrapper._EQ_MAX_WAITERS
    release.set()
    assert _wait(lambda: not DbusWrapper._eq_running)


def test_the_flag_is_cleared_when_a_read_fails(monkeypatch):
    async def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(DbusWrapper, "_read_eq_bands", staticmethod(broken))
    DbusWrapper.get_eq_bands(_Sink())
    assert _wait(lambda: not DbusWrapper._eq_running)
    calls = []

    async def fine():
        calls.append(1)
        return [5]

    monkeypatch.setattr(DbusWrapper, "_read_eq_bands", staticmethod(fine))
    sink = _Sink()
    DbusWrapper.get_eq_bands(sink)               # the next ask must not be swallowed by a stuck flag
    assert _wait(lambda: sink.got == [[5]])


def test_a_dead_page_does_not_stop_the_others_hearing_the_answer(monkeypatch):
    class Dead:
        def emit(self, value):
            raise RuntimeError("Internal C++ object already deleted")

    async def read():
        return [7]

    monkeypatch.setattr(DbusWrapper, "_read_eq_bands", staticmethod(read))
    alive = _Sink()
    DbusWrapper._eq_waiters.extend([Dead(), alive])
    DbusWrapper._eq_running = True               # as if get_eq_bands had just claimed the read
    DbusWrapper._executor.submit(DbusWrapper.get_eq_bands_thread)
    assert _wait(lambda: alive.got == [[7]])


def test_a_missing_daemon_is_logged_once_a_minute(caplog):
    caplog.set_level(logging.ERROR, logger="DbusWrapper")
    for _ in range(50):
        DbusWrapper._log_eq_failure(RuntimeError("no daemon"))
    assert len([r for r in caplog.records if "get_eq_bands" in r.getMessage()]) == 1
