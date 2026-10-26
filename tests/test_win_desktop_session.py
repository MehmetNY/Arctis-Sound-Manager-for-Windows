"""DesktopSession on Windows: no D-Bus round trips (they timed out every refresh), presence via win/presence.py."""
from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest

from arctis_sound_manager.win import install_dbus_shim

_TOUCHED = ("dbus_next", "dbus_next.aio", "dbus_next.aio.message_bus", "dbus_next.constants",
            "dbus_next.errors", "dbus_next.message", "dbus_next.service",
            "arctis_sound_manager.desktop_session")


@pytest.fixture
def desktop_session():
    """desktop_session imported the way Windows imports it: with no real dbus_next, behind the shim.
    sys.modules is put back afterwards so nothing leaks into other test files."""
    before = {name: sys.modules.get(name) for name in _TOUCHED}
    package = sys.modules.get("arctis_sound_manager")
    package_attr = getattr(package, "desktop_session", None)
    install_dbus_shim()
    sys.modules.pop("arctis_sound_manager.desktop_session", None)
    module = importlib.import_module("arctis_sound_manager.desktop_session")
    yield module
    for name, old in before.items():
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old
    if package is not None:
        if package_attr is not None:
            package.desktop_session = package_attr
        elif hasattr(package, "desktop_session"):
            del package.desktop_session


@pytest.fixture
def on_windows(monkeypatch, desktop_session):
    monkeypatch.setattr(desktop_session, "IS_WINDOWS", True)


def test_now_playing_comes_from_the_media_controls_and_never_touches_the_bus_on_windows(
        on_windows, desktop_session, monkeypatch):
    from arctis_sound_manager.win import media

    async def playing():
        return "Artist - Title"
    monkeypatch.setattr(media, "now_playing", playing)
    session = desktop_session.DesktopSession()
    monkeypatch.setattr(session, "_now_playing",
                        lambda *a, **k: pytest.fail("an MPRIS query was attempted"))

    async def no_bus(*a, **k):
        pytest.fail("a D-Bus connection was attempted")
    monkeypatch.setattr(session, "_bus", no_bus)
    try:
        assert session.now_playing() == "Artist - Title"
    finally:
        session.close()


def test_now_playing_is_empty_when_the_media_controls_fail_on_windows(on_windows, desktop_session, monkeypatch):
    from arctis_sound_manager.win import media

    async def broken():
        raise RuntimeError("media session service not running")
    monkeypatch.setattr(media, "now_playing", broken)
    session = desktop_session.DesktopSession()
    try:
        assert session.now_playing() == ""
    finally:
        session.close()


def _presence(monkeypatch, locked=False, idle=0.0):
    from arctis_sound_manager.win import presence
    monkeypatch.setattr(presence, "session_locked", lambda: locked)
    monkeypatch.setattr(presence, "idle_seconds", lambda: idle)


def test_user_away_does_not_use_the_bus_on_windows(on_windows, desktop_session, monkeypatch):
    session = desktop_session.DesktopSession()
    monkeypatch.setattr(session, "_run", lambda *a, **k: pytest.fail("a D-Bus query was attempted"))
    _presence(monkeypatch)
    assert session.user_away() is False
    assert session._loop is None


def test_locked_workstation_counts_as_away(on_windows, desktop_session, monkeypatch):
    _presence(monkeypatch, locked=True)
    assert desktop_session.DesktopSession().user_away() is True


def test_idle_keyboard_and_mouse_count_as_away(on_windows, desktop_session, monkeypatch):
    from arctis_sound_manager.win import presence
    _presence(monkeypatch, idle=presence.IDLE_AWAY_S + 1)
    assert desktop_session.DesktopSession().user_away() is True


def test_recent_input_on_an_unlocked_desktop_is_present(on_windows, desktop_session, monkeypatch):
    from arctis_sound_manager.win import presence
    _presence(monkeypatch, idle=presence.IDLE_AWAY_S - 1)
    assert desktop_session.DesktopSession().user_away() is False


def test_linux_path_is_unchanged(desktop_session, monkeypatch):
    monkeypatch.setattr(desktop_session, "IS_WINDOWS", False)
    session = desktop_session.DesktopSession()
    seen = []
    monkeypatch.setattr(session, "_run", lambda coro, default: (coro.close(), seen.append(default), default)[2])
    assert session.now_playing() == "" and session.user_away() is False
    assert seen == ["", False]                                   # both still go through the D-Bus helper
