# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the desktop session is doing, asked synchronously from the OLED threads.

Two questions the DAC screen needs answered and nothing else in the daemon asks:

* **What is playing** — MPRIS on the session bus. Every mainstream player and
  browser publishes ``org.mpris.MediaPlayer2.*``; the first one reporting
  ``Playing`` wins.
* **Is anyone there** — the base station OLED burns in (it is the first
  complaint about the device), and a fixed timeout cannot tell a user reading
  the screen from an empty room. Three signals, any of which is enough:

  - logind ``LockedHint`` on the user's graphical session: set by KDE's and
    GNOME's lockers alike, and by elogind on non-systemd distributions.
  - logind ``IdleHint`` on the same session: GNOME sets it after its idle
    delay. KDE does not, which is why the lock is checked too.
  - ``org.freedesktop.ScreenSaver.GetActive`` on the session bus: the one
    signal lighter desktops without a logind-aware locker still provide.

  ``GetSessionIdleTime`` is deliberately not used: KDE on Wayland refuses it
  ("not supported on this platform"), and the spec leaves its unit ambiguous.

The OLED code is threaded and synchronous; dbus_next is asyncio. This module
owns one private event loop on a daemon thread and exposes blocking methods
with a short timeout, so a hung bus can delay one refresh tick, never stall it.
Any failure — no session bus in a container, no logind, no player — answers
"nothing playing" / "not away" rather than raising: the screen then simply
behaves as it did before these features existed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from dbus_next import BusType, Message, MessageType
from dbus_next.aio import MessageBus

from arctis_sound_manager.constants import IS_WINDOWS

logger = logging.getLogger(__name__)

_CALL_TIMEOUT_S = 2.0
_DESKTOP_SWITCHDESKTOP = 0x0100   # access right OpenInputDesktop needs (Windows)
# After a bus refused to connect, how long before trying again. The daemon
# can start before the graphical session exists; retrying on every refresh
# tick would log the same failure every five seconds.
_RECONNECT_BACKOFF_S = 30.0

_MPRIS_PREFIX = "org.mpris.MediaPlayer2."
_MPRIS_PATH = "/org/mpris/MediaPlayer2"
_MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"
_PROPS = "org.freedesktop.DBus.Properties"
_LOGIN1 = "org.freedesktop.login1"
_LOGIN1_SESSION = "org.freedesktop.login1.Session"


def format_now_playing(metadata: dict[str, Any]) -> str:
    """Turn unpacked MPRIS metadata into "Artist - Title", or "" if untitled.

    ``xesam:artist`` is a list per the spec; some players send a bare string.
    A title alone is still worth showing, an artist alone is not.
    """
    title = str(metadata.get("xesam:title") or "").strip()
    if not title:
        return ""
    artist = metadata.get("xesam:artist") or ""
    if isinstance(artist, (list, tuple)):
        artist = ", ".join(str(a) for a in artist if a)
    artist = str(artist).strip()
    return f"{artist} - {title}" if artist else title


def _unpack(value: Any) -> Any:
    """Strip dbus_next Variants, recursively through dicts and lists."""
    if hasattr(value, "signature") and hasattr(value, "value"):
        return _unpack(value.value)
    if isinstance(value, dict):
        return {k: _unpack(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unpack(v) for v in value]
    return value


def _workstation_locked() -> bool:
    """Windows: True while the lock screen (or a UAC prompt) owns the input desktop.

    ``OpenInputDesktop`` is refused for a normal process as soon as the secure desktop is
    showing, and succeeds on the user's own desktop. Any other failure counts as "not locked":
    the screen then behaves as it did before this feature existed.
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        desktop = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        if not desktop:
            return True
        user32.CloseDesktop(desktop)
        return False
    except Exception as e:  # noqa: BLE001
        logger.debug("Desktop session: lock state unavailable: %r", e)
        return False


class DesktopSession:
    """On Windows the D-Bus questions below have no counterpart (no MPRIS, no logind), so
    ``now_playing`` is always empty and ``user_away`` means "the workstation is locked"."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._buses: dict[BusType, MessageBus | None] = {}
        self._next_connect: dict[BusType, float] = {}
        self._session_path: str | None = None

    # ── public, blocking ────────────────────────────────────────────────────

    def now_playing(self) -> str:
        """"Artist - Title" of the first playing MPRIS player, or ""."""
        if IS_WINDOWS:
            return ""
        return self._run(self._now_playing(), default="")

    def user_away(self) -> bool:
        """True when the session is locked or reported idle."""
        if IS_WINDOWS:
            return _workstation_locked()
        return self._run(self._user_away(), default=False)

    def close(self) -> None:
        loop = self._loop
        if loop is None:
            return
        for bus in self._buses.values():
            if bus is not None:
                loop.call_soon_threadsafe(bus.disconnect)
        loop.call_soon_threadsafe(loop.stop)
        self._loop = None

    # ── plumbing ────────────────────────────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=loop.run_forever, name="DesktopSession", daemon=True,
                )
                self._thread.start()
                self._loop = loop
            return self._loop

    def _run(self, coro, default):
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
            return future.result(timeout=_CALL_TIMEOUT_S)
        except Exception as e:
            logger.debug("Desktop session query failed: %r", e)
            return default

    async def _bus(self, bus_type: BusType) -> MessageBus | None:
        bus = self._buses.get(bus_type)
        if bus is not None and bus.connected:
            return bus
        if time.monotonic() < self._next_connect.get(bus_type, 0.0):
            return None
        try:
            bus = await MessageBus(bus_type=bus_type).connect()
        except Exception as e:
            self._next_connect[bus_type] = time.monotonic() + _RECONNECT_BACKOFF_S
            logger.info("Desktop session: %s bus unavailable (%s)", bus_type.name.lower(), e)
            self._buses[bus_type] = None
            return None
        self._buses[bus_type] = bus
        return bus

    async def _call(self, bus_type: BusType, destination: str, path: str,
                    interface: str, member: str, signature: str = "",
                    body: list | None = None) -> list | None:
        """One method call, raw (no introspection round trip); None on error."""
        bus = await self._bus(bus_type)
        if bus is None:
            return None
        reply = await bus.call(Message(
            destination=destination, path=path, interface=interface,
            member=member, signature=signature, body=body or [],
        ))
        if reply is None or reply.message_type != MessageType.METHOD_RETURN:
            return None
        return reply.body

    async def _get_prop(self, bus_type: BusType, destination: str, path: str,
                        interface: str, prop: str) -> Any:
        body = await self._call(bus_type, destination, path, _PROPS, "Get",
                                "ss", [interface, prop])
        return _unpack(body[0]) if body else None

    # ── MPRIS ───────────────────────────────────────────────────────────────

    async def _now_playing(self) -> str:
        body = await self._call(BusType.SESSION, "org.freedesktop.DBus",
                                "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "ListNames")
        if not body:
            return ""
        players = sorted(n for n in body[0] if n.startswith(_MPRIS_PREFIX))
        for name in players:
            status = await self._get_prop(BusType.SESSION, name, _MPRIS_PATH,
                                          _MPRIS_PLAYER, "PlaybackStatus")
            if status != "Playing":
                continue
            metadata = await self._get_prop(BusType.SESSION, name, _MPRIS_PATH,
                                            _MPRIS_PLAYER, "Metadata")
            text = format_now_playing(metadata or {})
            if text:
                return text
        return ""

    # ── presence ────────────────────────────────────────────────────────────

    async def _graphical_session_path(self) -> str | None:
        """logind object path of the user's graphical session, cached.

        The daemon runs as a user service, outside any session, so
        ``session/self`` would not resolve; ``user/self``'s ``Display`` names
        the session the desktop runs in.
        """
        if self._session_path is None:
            display = await self._get_prop(BusType.SYSTEM, _LOGIN1,
                                           "/org/freedesktop/login1/user/self",
                                           "org.freedesktop.login1.User", "Display")
            if isinstance(display, list) and len(display) == 2 and display[1] != "/":
                self._session_path = display[1]
        return self._session_path

    async def _user_away(self) -> bool:
        path = await self._graphical_session_path()
        if path is not None:
            for hint in ("LockedHint", "IdleHint"):
                value = await self._get_prop(BusType.SYSTEM, _LOGIN1, path,
                                             _LOGIN1_SESSION, hint)
                if value is None:
                    # The session is gone (logout/login): look it up again.
                    self._session_path = None
                    break
                if value:
                    return True
        body = await self._call(BusType.SESSION, "org.freedesktop.ScreenSaver",
                                "/org/freedesktop/ScreenSaver",
                                "org.freedesktop.ScreenSaver", "GetActive")
        return bool(body and body[0])
