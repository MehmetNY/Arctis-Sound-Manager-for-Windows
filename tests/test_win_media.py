"""The DAC screen's media line on Windows: what the system media controls (SMTC) say is playing.

The WinRT bindings do not exist on the machine the tests run on, so a stand-in for
``winrt.windows.media.control`` is put into ``sys.modules``; what is tested is which session is chosen
and how its title is written, plus that ``DesktopSession.now_playing`` goes through it on Windows."""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from arctis_sound_manager.win import media

PLAYING, PAUSED, STOPPED = 4, 5, 3


class _Props:
    def __init__(self, artist, title):
        self.artist, self.title = artist, title


class _Info:
    def __init__(self, status):
        self.playback_status = status


class _Session:
    def __init__(self, status, artist="", title="", props_none=False, boom=False):
        self._status, self._props_none, self._boom = status, props_none, boom
        self._props = _Props(artist, title)

    def get_playback_info(self):
        if self._boom:
            raise OSError("the session went away")
        return _Info(self._status)

    async def try_get_media_properties_async(self):
        return None if self._props_none else self._props


class _Manager:
    def __init__(self, current, sessions):
        self._current, self._sessions = current, sessions

    def get_current_session(self):
        return self._current

    def get_sessions(self):
        return list(self._sessions)


@pytest.fixture()
def smtc(monkeypatch):
    """Install a fake ``winrt`` and return a function that sets what Windows reports."""
    state = {"manager": _Manager(None, []), "requests": 0, "fail": False}

    class FakeManagerClass:
        @staticmethod
        async def request_async():
            state["requests"] += 1
            if state["fail"]:
                raise OSError("no media session service")
            return state["manager"]

    control = types.ModuleType("winrt.windows.media.control")
    control.GlobalSystemMediaTransportControlsSessionManager = FakeManagerClass
    for name in ("winrt", "winrt.windows", "winrt.windows.media"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "winrt.windows.media.control", control)
    monkeypatch.setattr(media, "_import_failed", False)
    monkeypatch.setattr(media, "_manager", None)

    def report(current=None, others=()):
        sessions = ([current] if current is not None else []) + list(others)
        state["manager"] = _Manager(current, sessions)

    report.state = state
    return report


def _now():
    return asyncio.run(media.now_playing())


def test_artist_and_title_of_the_playing_current_session(smtc):
    smtc(_Session(PLAYING, "Daft Punk", "Around the World"))
    assert _now() == "Daft Punk - Around the World"


def test_a_youtube_tab_has_a_title_and_no_artist(smtc):
    smtc(_Session(PLAYING, "", "How to bake bread - YouTube"))
    assert _now() == "How to bake bread - YouTube"


def test_nothing_playing_gives_an_empty_line(smtc):
    smtc(_Session(PAUSED, "A", "B"), others=[_Session(STOPPED, "C", "D")])
    assert _now() == ""


def test_a_paused_current_session_does_not_hide_another_one_that_plays(smtc):
    smtc(_Session(PAUSED, "Paused", "Song"), others=[_Session(PLAYING, "Playing", "Other")])
    assert _now() == "Playing - Other"


def test_the_current_session_wins_over_another_playing_one(smtc):
    smtc(_Session(PLAYING, "Current", "One"), others=[_Session(PLAYING, "Other", "Two")])
    assert _now() == "Current - One"


def test_no_sessions_at_all(smtc):
    smtc()
    assert _now() == ""


def test_a_playing_session_without_a_title_is_skipped(smtc):
    smtc(_Session(PLAYING, "Artist", ""), others=[_Session(PLAYING, "Real", "Title")])
    assert _now() == "Real - Title"


def test_a_session_without_properties_is_skipped(smtc):
    smtc(_Session(PLAYING, props_none=True), others=[_Session(PLAYING, "Real", "Title")])
    assert _now() == "Real - Title"


def test_a_session_that_vanishes_does_not_break_the_line(smtc):
    smtc(_Session(PLAYING, boom=True), others=[_Session(PLAYING, "Real", "Title")])
    assert _now() == "Real - Title"


def test_windows_refusing_the_manager_gives_an_empty_line_and_is_asked_again(smtc):
    smtc.state["fail"] = True
    assert _now() == ""
    smtc.state["fail"] = False
    smtc(_Session(PLAYING, "A", "B"))
    assert _now() == "A - B"


def test_the_manager_is_requested_once(smtc):
    smtc(_Session(PLAYING, "A", "B"))
    assert _now() == "A - B" and _now() == "A - B"
    assert smtc.state["requests"] == 1


def test_missing_bindings_only_lose_the_line(monkeypatch):
    monkeypatch.setitem(sys.modules, "winrt.windows.media.control", None)   # import raises ImportError
    monkeypatch.setattr(media, "_import_failed", False)
    monkeypatch.setattr(media, "_manager", None)
    assert _now() == ""
    assert media._import_failed is True


@pytest.mark.parametrize("artist, title, expected", [
    ("A", "T", "A - T"), ("  A ", " T  ", "A - T"), ("", "T", "T"), (None, "T", "T"),
    ("A", "", ""), ("A", None, ""), (None, None, ""), ("", "  ", ""),
])
def test_format_track(artist, title, expected):
    assert media.format_track(artist, title) == expected


def test_desktop_session_asks_the_media_controls_on_windows(monkeypatch, smtc):
    from arctis_sound_manager import desktop_session
    smtc(_Session(PLAYING, "Artist", "Title"))
    monkeypatch.setattr(desktop_session, "IS_WINDOWS", True)
    session = desktop_session.DesktopSession()
    try:
        assert session.now_playing() == "Artist - Title"
    finally:
        session.close()


def test_desktop_session_still_asks_mpris_on_linux(monkeypatch):
    from arctis_sound_manager import desktop_session
    monkeypatch.setattr(desktop_session, "IS_WINDOWS", False)
    session = desktop_session.DesktopSession()
    called = []

    async def fake_now_playing():
        called.append(True)
        return "From MPRIS"
    monkeypatch.setattr(session, "_now_playing", fake_now_playing)
    try:
        assert session.now_playing() == "From MPRIS" and called
    finally:
        session.close()
