# SPDX-License-Identifier: GPL-3.0-or-later

"""What is playing right now, on Windows: the system media transport controls (SMTC).

Chrome, Edge, Firefox, Spotify, the Windows media player and most other players publish the track
they play to the same place Windows reads for its volume flyout and lock screen. This is the Windows
counterpart of the MPRIS lookup in :mod:`arctis_sound_manager.desktop_session`: it answers the text
the DAC screen shows on its media line ("Artist - Title", or just the title when there is no artist).

The WinRT bindings (``winrt-Windows.Media.Control``) are imported when first asked, so a machine
without them — or Linux, which never calls this — only loses the media line instead of failing.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
_PLAYING = 4

_import_failed = False
_manager = None            # the session manager is the same object every time: ask Windows for it once


def format_track(artist: str | None, title: str | None) -> str:
    """``"Artist - Title"``; the title alone when there is no artist; ``""`` when there is no title."""
    title = (title or "").strip()
    if not title:
        return ""
    artist = (artist or "").strip()
    return f"{artist} - {title}" if artist else title


def _load():
    global _import_failed
    if _import_failed:
        return None
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
    except Exception as e:  # noqa: BLE001 - missing package, or not Windows
        _import_failed = True
        _log.info("Media line unavailable: the WinRT media bindings are not installed (%s)", e)
        return None
    return GlobalSystemMediaTransportControlsSessionManager


def _is_playing(session) -> bool:
    try:
        return int(session.get_playback_info().playback_status) == _PLAYING
    except Exception as e:  # noqa: BLE001 - a session can vanish between listing and asking
        _log.debug("Media session playback state unavailable: %r", e)
        return False


async def _track_of(session) -> str:
    try:
        props = await session.try_get_media_properties_async()
    except Exception as e:  # noqa: BLE001
        _log.debug("Media session properties unavailable: %r", e)
        return ""
    if props is None:
        return ""
    return format_track(props.artist, props.title)


async def now_playing() -> str:
    """The track of the first player that is playing, or ``""``.

    Windows' own idea of the *current* session (the one its media flyout shows) is asked first, then
    every other session in the order Windows lists them; a paused or stopped player never counts, so
    the line empties when the music stops.
    """
    manager_cls = _load()
    if manager_cls is None:
        return ""
    global _manager
    try:
        if _manager is None:
            _manager = await manager_cls.request_async()
        manager = _manager
        sessions = []
        current = manager.get_current_session()
        if current is not None:
            sessions.append(current)
        sessions += [s for s in manager.get_sessions() if s is not current]
    except Exception as e:  # noqa: BLE001
        _manager = None    # ask again next time
        _log.debug("Media sessions unavailable: %r", e)
        return ""
    for session in sessions:
        if not _is_playing(session):
            continue
        text = await _track_of(session)
        if text:
            return text
    return ""
