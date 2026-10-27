# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Where does an audio thread spend its time? A profile of a few seconds, on demand, written to the health log.

Creating the file ``profile-<name>.txt`` (``profile-playback.txt`` or ``profile-microphone.txt``) in
``%LOCALAPPDATA%\\arctis_manager`` - its content may be a number of seconds, default 20 - makes that pipeline's thread
profile itself for that long (``cProfile``; the engine runs a little slower meanwhile; on Python 3.12 and later it
sees every thread of the process) and then log the functions that used the most time as ``profile:`` lines. The
file is removed as soon as it is picked up. Nothing happens, and nothing is paid for beyond one file check a second,
unless the file exists.
"""
from __future__ import annotations

import cProfile
import logging
import os
import pstats
import re
import threading
import time
from pathlib import Path
from typing import Callable

DEFAULT_SECONDS = 20.0
MAX_SECONDS = 120.0
CHECK_EVERY_S = 1.0
TOP_OWN = 25
TOP_CUMULATIVE = 12

_log = logging.getLogger("health")
_lock = threading.Lock()
_active: "ProfileTrigger | None" = None          # cProfile can run once at a time


def trigger_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "arctis_manager" if base else Path.home() / ".config" / "arctis_manager"


def _short(key: tuple) -> str:
    filename, line, name = key
    parts = re.split(r"[\\/]", filename) if filename and not filename.startswith("<") else [filename or "?"]
    return f"{'/'.join(parts[-2:])}:{line}({name})"


def summarise(stats: pstats.Stats, top_own: int = TOP_OWN, top_cumulative: int = TOP_CUMULATIVE) -> list[str]:
    """The lines of the report: own time by function, then cumulative time by function."""
    rows = [(key, values[1], values[2], values[3]) for key, values in stats.stats.items()]      # key, calls, own, cumulative
    total = sum(own for _, _, own, _ in rows) or 1e-9
    lines = [f"own time (of {total:.2f} s in {len(rows)} functions):"]
    for key, calls, own, _ in sorted(rows, key=lambda r: -r[2])[:top_own]:
        lines.append(f"{own / total * 100:5.1f}% {own:7.3f}s {calls:8d} calls  {_short(key)}")
    lines.append("cumulative time:")
    for key, calls, _, cumulative in sorted(rows, key=lambda r: -r[3])[:top_cumulative]:
        lines.append(f"{cumulative:7.3f}s {calls:8d} calls  {_short(key)}")
    return lines


class ProfileTrigger:
    """Call :meth:`poll` from the loop of the thread to profile; it is cheap until the trigger file appears."""

    def __init__(self, name: str, directory: Path | None = None, clock: Callable[[], float] = time.monotonic,
                 logger: logging.Logger | None = None):
        self.name = name
        self._dir = directory
        self._clock = clock
        self._log = logger or _log
        self._next_check = 0.0
        self._profile: cProfile.Profile | None = None
        self._end = 0.0
        self._seconds = 0.0

    @property
    def path(self) -> Path:
        return (self._dir or trigger_dir()) / f"profile-{self.name}.txt"

    @property
    def running(self) -> bool:
        return self._profile is not None

    def poll(self) -> None:
        now = self._clock()
        if self._profile is not None:
            if now >= self._end:
                self._finish()
            return
        if now < self._next_check:
            return
        self._next_check = now + CHECK_EVERY_S
        try:
            requested = self.path.is_file()
        except OSError:
            return
        if requested:
            self._start(now)

    def _start(self, now: float) -> None:
        global _active
        try:
            text = self.path.read_text(encoding="ascii", errors="ignore").strip()
            self.path.unlink()
        except OSError:
            return
        try:
            seconds = min(MAX_SECONDS, max(2.0, float(text))) if text else DEFAULT_SECONDS
        except ValueError:
            seconds = DEFAULT_SECONDS
        with _lock:
            if _active is not None:
                self._log.info("profile: %s: another profile is running; ignored", self.name)
                return
            _active = self
        profile = cProfile.Profile()
        try:
            profile.enable()
        except Exception as exc:                                  # noqa: BLE001 — e.g. another profiler or tracer is active
            with _lock:
                _active = None
            self._log.info("profile: %s: could not start (%r)", self.name, exc)
            return
        self._profile, self._seconds, self._end = profile, seconds, now + seconds
        self._log.info("profile: %s thread, %.0f s starting (the engine is a little slower meanwhile)", self.name, seconds)

    def abort(self) -> None:
        """Stop a profile that is running without reporting it (the thread is ending)."""
        global _active
        profile, self._profile = self._profile, None
        if profile is None:
            return
        try:
            profile.disable()
        except Exception:                                         # noqa: BLE001
            pass
        with _lock:
            if _active is self:
                _active = None

    def _finish(self) -> None:
        global _active
        profile, self._profile = self._profile, None
        try:
            profile.disable()
            for line in summarise(pstats.Stats(profile)):
                self._log.info("profile: %s: %s", self.name, line)
        except Exception:                                         # noqa: BLE001 — only a diagnostic
            self._log.exception("profile: %s: the report failed", self.name)
        finally:
            with _lock:
                _active = None
