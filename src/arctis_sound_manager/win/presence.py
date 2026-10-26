# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Is anybody at the Windows desktop? (the counterpart of logind's ``LockedHint`` / ``IdleHint``)

Two questions, both answered by ``user32`` without any window, service or elevated right:

* :func:`session_locked` - the lock screen, the screen saver and UAC prompts run on their own desktop
  ("Winlogon", "Screen-saver"); only the user's own desktop is called "Default". The input desktop is opened
  read-only and *named* - the same test Chromium uses. Testing only whether the open succeeds is not enough:
  an administrator is allowed to open the secure desktop, so the lock went unnoticed on exactly the
  accounts most people use.
* :func:`idle_seconds` - seconds since the last key press or mouse move (``GetLastInputInfo``).

Anything unexpected (no ``windll``, a call failing) answers "not locked" / "not idle": the DAC screen then
behaves as it did before these checks existed instead of going dark for no reason.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# No input for this long counts as "away" for the DAC screen. Games played with a controller do not move
# the keyboard/mouse idle counter, so it is deliberately not short.
IDLE_AWAY_S = 300.0

_GENERIC_READ = 0x80000000
_UOI_NAME = 2
_USER_DESKTOP = "default"


def _user32():
    import ctypes
    return ctypes, ctypes.windll.user32


def input_desktop_name() -> str | None:
    """Name of the desktop that currently receives input: ``"Default"``, ``"Winlogon"``, ``"Screen-saver"``...

    ``""`` when the desktop cannot be opened at all (access denied - the secure desktop is up);
    ``None`` when the question cannot be asked (not Windows, call failed)."""
    try:
        ctypes, user32 = _user32()
        from ctypes import wintypes
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        user32.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                     ctypes.POINTER(wintypes.DWORD)]
        desktop = user32.OpenInputDesktop(0, False, _GENERIC_READ)
        if not desktop:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD(0)
            if not user32.GetUserObjectInformationW(desktop, _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
                return None
            return buf.value
        finally:
            user32.CloseDesktop(desktop)
    except Exception as exc:  # noqa: BLE001
        log.debug("input desktop unavailable: %r", exc)
        return None


def session_locked() -> bool:
    """True while the lock screen / screen saver / a UAC prompt owns the input desktop."""
    name = input_desktop_name()
    if name is None:
        return False
    return name.lower() != _USER_DESKTOP


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input of this session (0.0 when it cannot be told)."""
    try:
        ctypes, user32 = _user32()
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount.restype = wintypes.DWORD
        # both are 32-bit millisecond counters that wrap every 49.7 days: subtract modulo 2**32
        return float((int(kernel32.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0
    except Exception as exc:  # noqa: BLE001
        log.debug("idle time unavailable: %r", exc)
        return 0.0


def user_away(idle_limit: float = IDLE_AWAY_S) -> bool:
    """Locked, or no keyboard/mouse input for ``idle_limit`` seconds."""
    return session_locked() or idle_seconds() >= idle_limit
