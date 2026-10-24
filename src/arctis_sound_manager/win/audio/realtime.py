# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Making the audio threads as hard to interrupt as Windows allows.

Three things help a Python audio engine on Windows, all optional and all best effort (a failure is
logged at debug level and the engine runs anyway):

* **MMCSS** — registering the thread with the Multimedia Class Scheduler Service as ``Pro Audio``
  gives it a guaranteed slice of CPU time even when games hog the cores;
* **thread priority** ``TIME_CRITICAL``;
* **timer resolution** 1 ms — otherwise ``Event.wait(0.002)`` can sleep 15 ms.

On other systems everything here does nothing.
"""
from __future__ import annotations

import logging
import sys

_log = logging.getLogger(__name__)

THREAD_PRIORITY_TIME_CRITICAL = 15
AVRT_PRIORITY_HIGH = 2


class _WinApi:
    """The handful of Win32 calls, bound lazily so importing this module costs nothing."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.avrt = ctypes.WinDLL("avrt", use_last_error=True)
        self.winmm = ctypes.WinDLL("winmm", use_last_error=True)
        self.kernel32.GetCurrentThread.restype = wintypes.HANDLE
        self.kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        self.avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        self.avrt.AvSetMmThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        self.avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
        self._wintypes = wintypes

    def set_thread_priority(self, level: int) -> bool:
        return bool(self.kernel32.SetThreadPriority(self.kernel32.GetCurrentThread(), level))

    def mmcss_enter(self, task: str):
        index = self._wintypes.DWORD(0)
        handle = self.avrt.AvSetMmThreadCharacteristicsW(task, self._ct.byref(index))
        if handle:
            self.avrt.AvSetMmThreadPriority(handle, AVRT_PRIORITY_HIGH)
        return handle or None

    def mmcss_leave(self, handle) -> None:
        self.avrt.AvRevertMmThreadCharacteristics(handle)

    def time_begin(self, ms: int) -> None:
        self.winmm.timeBeginPeriod(ms)

    def time_end(self, ms: int) -> None:
        self.winmm.timeEndPeriod(ms)


def _default_api():
    if sys.platform != "win32":
        return None
    try:
        return _WinApi()
    except Exception as exc:                                      # noqa: BLE001
        _log.debug("Win32 scheduling API unavailable: %r", exc)
        return None


class ThreadBoost:
    """``with ThreadBoost():`` at the top of an audio thread; leaves MMCSS again on exit."""

    def __init__(self, task: str = "Pro Audio", api=None):
        self._api = api if api is not None else _default_api()
        self._task = task
        self._handle = None
        self.applied: list[str] = []

    def __enter__(self) -> "ThreadBoost":
        api = self._api
        if api is None:
            return self
        try:
            self._handle = api.mmcss_enter(self._task)
            if self._handle:
                self.applied.append(f"MMCSS {self._task}")
        except Exception as exc:                                  # noqa: BLE001
            _log.debug("MMCSS failed: %r", exc)
        try:
            if api.set_thread_priority(THREAD_PRIORITY_TIME_CRITICAL):
                self.applied.append("time-critical priority")
        except Exception as exc:                                  # noqa: BLE001
            _log.debug("SetThreadPriority failed: %r", exc)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._api is not None and self._handle:
            try:
                self._api.mmcss_leave(self._handle)
            except Exception as exc:                              # noqa: BLE001
                _log.debug("leaving MMCSS failed: %r", exc)
        self._handle = None


class TimerResolution:
    """``with TimerResolution(1):`` for the lifetime of the engine."""

    def __init__(self, ms: int = 1, api=None):
        self._api = api if api is not None else _default_api()
        self._ms = ms
        self._on = False

    def __enter__(self) -> "TimerResolution":
        if self._api is not None:
            try:
                self._api.time_begin(self._ms)
                self._on = True
            except Exception as exc:                              # noqa: BLE001
                _log.debug("timeBeginPeriod failed: %r", exc)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._on:
            try:
                self._api.time_end(self._ms)
            except Exception as exc:                              # noqa: BLE001
                _log.debug("timeEndPeriod failed: %r", exc)
            self._on = False
