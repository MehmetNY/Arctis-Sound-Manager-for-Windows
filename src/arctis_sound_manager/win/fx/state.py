# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later
"""How the microphone effect is doing, in a few words the window can show, and the asking for it to be set up.

Reading needs no administrator rights. Changing anything is done by a scheduled task that runs as SYSTEM
(:mod:`.tasks`, created by the installer); when that task is missing the caller gets ``False`` and says so.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Callable

from arctis_sound_manager.win.fx import attach as fx
from arctis_sound_manager.win.fx import tasks

_log = logging.getLogger(__name__)

# codes
OFF = "off"                  # not Windows
MISSING = "missing"          # the effect's file is not installed
DISABLED = "disabled"        # switched off by hand, or after the audio engine crashed in it
NO_HEADSET = "no_headset"    # Windows does not know the headset's microphone (yet)
NEEDS_SETUP = "needs_setup"  # installed, not attached to the microphone
READY = "ready"              # attached; nothing is recording right now
RUNNING = "running"          # a program is recording and the sound goes through ASM


@dataclass(frozen=True)
class FxState:
    code: str
    detail: str = ""

    @property
    def active(self) -> bool:
        return self.code in (READY, RUNNING)


def describe(status: dict | None = None, registry=None, dll: str | None = None) -> FxState:
    """``status``: the audio engine's own status reply, if the caller has it (its microphone block knows whether sound
    is flowing through the effect right now)."""
    mic = (status or {}).get("microphone") or {}
    if mic.get("state") == "processing":
        return FxState(RUNNING)
    if sys.platform != "win32" and registry is None:
        return FxState(OFF)
    try:
        if registry is None:
            from arctis_sound_manager.win.fx.registry import system_registry
            registry = system_registry()
        if dll is None:
            from arctis_sound_manager.win.fx.cli import dll_path
            dll = dll_path()
        s = fx.status(registry, dll)
    except Exception as exc:                                         # noqa: BLE001 — a status line must never raise
        _log.debug("could not read the state of the microphone effect: %r", exc)
        return FxState(OFF, str(exc))
    if s.disabled:
        return FxState(DISABLED, s.disabled)
    if not s.dll_present:
        return FxState(MISSING)
    if s.ready and not s.missing:
        return FxState(READY)
    if not s.wanted:
        return FxState(NO_HEADSET)
    return FxState(NEEDS_SETUP)


def request(action: str, run_task: Callable[[str], bool] = tasks.start) -> bool:
    """Ask for ``attach`` (set it up, also after it was switched off), ``detach`` (switch it off) or ``guard``.
    ``False`` when the scheduled tasks are not there to do it."""
    return bool(run_task(action))


class Maintainer:
    """Keeps the effect attached for the person who never opens a window: called now and then by the daemon.

    * the headset's microphone appears in Windows, or Windows forgets the effect (a driver update rewrites the
      device's keys): ask for ``attach`` again, at most once every ``retry_s`` seconds;
    * the audio engine crashed in the effect twice in half an hour: ask for ``guard``, which removes it for good.
    """

    def __init__(self, describe_fn: Callable[[], FxState] = describe, run_task: Callable[[str], bool] = tasks.start,
                 crashes: Callable[[], int] = fx.audiodg_crashes, clock: Callable[[], float] = time.monotonic,
                 retry_s: float = 600.0, crash_check_s: float = 300.0):
        self._describe, self._run, self._crashes, self._clock = describe_fn, run_task, crashes, clock
        self.retry_s, self.crash_check_s = retry_s, crash_check_s
        self._next_attach = 0.0
        self._next_crash_check = 0.0
        self.asked: list[str] = []

    def tick(self) -> str | None:
        """Returns what it asked for, if anything."""
        now = self._clock()
        state = self._describe()
        if state.active and now >= self._next_crash_check:
            self._next_crash_check = now + self.crash_check_s
            if self._crashes() >= 2:
                self.asked.append("guard")
                self._run("guard")
                return "guard"
        if state.code == NEEDS_SETUP and now >= self._next_attach:
            self._next_attach = now + self.retry_s
            self.asked.append("attach")
            self._run("attach")
            return "attach"
        return None
