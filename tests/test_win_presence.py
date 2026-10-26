"""win/presence.py: lock state from the input desktop's name, idle time from GetLastInputInfo (user32 faked)."""
from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from arctis_sound_manager.win import presence


def _windll(monkeypatch, *, desktop=0x1234, name="Default", name_ok=True, open_raises=None, last_input=None,
            ticks=0, input_ok=True):
    calls = SimpleNamespace(open=[], closed=[])

    def open_input_desktop(flags, inherit, access):
        calls.open.append((flags, bool(inherit), access))
        if open_raises:
            raise open_raises
        return desktop

    def get_info(handle, index, buf, size, needed):
        assert index == 2                                         # UOI_NAME
        if not name_ok:
            return 0
        buf.value = name
        return 1

    def get_last_input(ref):
        if not input_ok:
            return 0
        ref._obj.dwTime = last_input
        return 1

    user32 = SimpleNamespace(OpenInputDesktop=open_input_desktop,
                             CloseDesktop=lambda handle: calls.closed.append(handle),
                             GetUserObjectInformationW=get_info, GetLastInputInfo=get_last_input)
    kernel32 = SimpleNamespace(GetTickCount=lambda: ticks)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32, kernel32=kernel32), raising=False)
    return calls


def test_the_users_own_desktop_is_not_locked_and_the_handle_is_closed(monkeypatch):
    calls = _windll(monkeypatch, name="Default")
    assert presence.session_locked() is False
    assert calls.open == [(0, False, 0x80000000)] and calls.closed == [0x1234]


@pytest.mark.parametrize("name", ["Winlogon", "Screen-saver", "Disconnect"])
def test_another_input_desktop_is_a_locked_session_even_when_it_can_be_opened(monkeypatch, name):
    """An administrator may open the secure desktop: only its name tells."""
    calls = _windll(monkeypatch, name=name)
    assert presence.session_locked() is True
    assert calls.closed == [0x1234]


def test_the_name_comparison_ignores_case(monkeypatch):
    _windll(monkeypatch, name="DEFAULT")
    assert presence.session_locked() is False


def test_a_desktop_that_cannot_be_opened_is_the_secure_desktop(monkeypatch):
    calls = _windll(monkeypatch, desktop=0)
    assert presence.session_locked() is True
    assert presence.input_desktop_name() == "" and calls.closed == []


def test_an_unreadable_name_or_a_failing_call_does_not_count_as_locked(monkeypatch):
    calls = _windll(monkeypatch, name_ok=False)
    assert presence.input_desktop_name() is None and presence.session_locked() is False
    assert calls.closed == [0x1234, 0x1234]                       # closed both times
    _windll(monkeypatch, open_raises=OSError("boom"))
    assert presence.session_locked() is False


def test_without_windll_nothing_is_locked_and_nobody_is_idle(monkeypatch):
    monkeypatch.delattr(ctypes, "windll", raising=False)
    assert presence.session_locked() is False
    assert presence.idle_seconds() == 0.0
    assert presence.user_away() is False


def test_idle_time_is_the_distance_between_the_two_millisecond_clocks(monkeypatch):
    _windll(monkeypatch, last_input=1_000, ticks=91_500)
    assert presence.idle_seconds() == pytest.approx(90.5)


def test_idle_time_survives_the_49_day_wrap_of_the_tick_counter(monkeypatch):
    _windll(monkeypatch, last_input=0xFFFFFF00, ticks=0x00000100)
    assert presence.idle_seconds() == pytest.approx(0x200 / 1000)


def test_a_failing_last_input_call_means_not_idle(monkeypatch):
    _windll(monkeypatch, input_ok=False)
    assert presence.idle_seconds() == 0.0


def test_away_means_locked_or_idle_for_long_enough(monkeypatch):
    _windll(monkeypatch, name="Default", last_input=0, ticks=int(presence.IDLE_AWAY_S * 1000) - 1)
    assert presence.user_away() is False
    _windll(monkeypatch, name="Default", last_input=0, ticks=int(presence.IDLE_AWAY_S * 1000))
    assert presence.user_away() is True
    _windll(monkeypatch, name="Winlogon", last_input=0, ticks=5)
    assert presence.user_away() is True
    assert presence.user_away(idle_limit=0.001) is True
