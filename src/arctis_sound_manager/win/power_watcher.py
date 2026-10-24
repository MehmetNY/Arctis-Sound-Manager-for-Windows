# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Resume-from-sleep detection for Windows (the counterpart of ``DbusAwake``).

On Linux, logind announces ``PrepareForSleep`` over D-Bus. Windows offers power
notifications only through a window or a service handler, neither of which the
headless daemon has. A suspended machine is, however, trivially visible from
inside: the event loop is frozen, so a timer that should fire after one second
fires minutes later. After a resume the headset's USB handles are stale (the dongle
was re-enumerated), so the core must re-acquire the device, which is exactly
what ``CoreEngine.resume_from_sleep()`` does.

Two clocks are compared because Windows' monotonic clock may or may not count the
time spent asleep depending on the Python version:

* monotonic counts sleep  -> the timer itself overshoots;
* monotonic excludes it   -> the wall clock outruns the monotonic one.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

log = logging.getLogger("PowerWatcher")


class PowerWatcher:
    _instance: "PowerWatcher | None" = None

    @staticmethod
    def get_instance() -> "PowerWatcher":
        if PowerWatcher._instance is None:
            PowerWatcher._instance = PowerWatcher()
        return PowerWatcher._instance

    def __init__(self, interval: float = 1.0, gap: float = 10.0,
                 mono: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] = asyncio.sleep):
        self.interval = interval
        self.gap = gap
        self._mono, self._wall, self._sleep = mono, wall, sleep
        self.resumes = 0

    def slept(self, mono_elapsed: float, wall_elapsed: float) -> bool:
        """True when the elapsed times only make sense if the machine was suspended."""
        return (mono_elapsed > self.interval + self.gap
                or wall_elapsed - mono_elapsed > self.gap)

    async def start(self, core_engine) -> None:
        log.info("Resume-from-sleep watcher started.")
        while True:
            mono0, wall0 = self._mono(), self._wall()
            await self._sleep(self.interval)
            if self.slept(self._mono() - mono0, self._wall() - wall0):
                self.resumes += 1
                log.info("System resumed from sleep: re-acquiring the headset.")
                try:
                    await core_engine.resume_from_sleep()
                except Exception as exc:  # noqa: BLE001 - must never end the watcher
                    log.warning("resume_from_sleep failed: %r", exc)
