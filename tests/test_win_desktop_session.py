"""DesktopSession on Windows: no D-Bus round trips (they timed out every refresh), lock state via user32."""
from __future__ import annotations

import ctypes
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


def _fake_user32(monkeypatch, open_result=None, open_raises=None):
    calls = SimpleNamespace(open=[], closed=[])

    def open_input_desktop(flags, inherit, access):
        calls.open.append((flags, inherit, access))
        if open_raises:
            raise open_raises
        return open_result

    user32 = SimpleNamespace(OpenInputDesktop=open_input_desktop,
                             CloseDesktop=lambda handle: calls.closed.append(handle))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    return calls


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


def test_user_away_does_not_use_the_bus_on_windows(on_windows, desktop_session, monkeypatch):
    session = desktop_session.DesktopSession()
    monkeypatch.setattr(session, "_run", lambda *a, **k: pytest.fail("a D-Bus query was attempted"))
    _fake_user32(monkeypatch, open_result=0x1234)
    assert session.user_away() is False
    assert session._loop is None


def test_locked_workstation_counts_as_away(on_windows, desktop_session, monkeypatch):
    calls = _fake_user32(monkeypatch, open_result=0)           # NULL: the secure desktop owns input
    assert desktop_session.DesktopSession().user_away() is True
    assert calls.open == [(0, False, 0x0100)] and calls.closed == []


def test_unlocked_workstation_is_not_away_and_the_handle_is_closed(on_windows, desktop_session, monkeypatch):
    calls = _fake_user32(monkeypatch, open_result=0xBEEF)
    assert desktop_session.DesktopSession().user_away() is False
    assert calls.closed == [0xBEEF]


def test_an_unexpected_user32_failure_means_not_away(on_windows, desktop_session, monkeypatch):
    _fake_user32(monkeypatch, open_raises=OSError("boom"))
    assert desktop_session.DesktopSession().user_away() is False


def test_without_windll_the_answer_is_not_away(on_windows, desktop_session, monkeypatch):
    monkeypatch.delattr(ctypes, "windll", raising=False)
    assert desktop_session.DesktopSession().user_away() is False


def test_linux_path_is_unchanged(desktop_session, monkeypatch):
    monkeypatch.setattr(desktop_session, "IS_WINDOWS", False)
    session = desktop_session.DesktopSession()
    seen = []
    monkeypatch.setattr(session, "_run", lambda coro, default: (coro.close(), seen.append(default), default)[2])
    assert session.now_playing() == "" and session.user_away() is False
    assert seen == ["", False]                                   # both still go through the D-Bus helper
