# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""Now-playing line and away-detection for the DAC OLED.

The desktop side (MPRIS, logind, ScreenSaver) is replaced by a fake session:
what is under test is what the screen does with the answers, and that a
session that cannot answer leaves the screen exactly as it was before.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from arctis_sound_manager import oled_manager as oled_mod
from arctis_sound_manager.desktop_session import (DesktopSession,
                                                  format_now_playing)
from arctis_sound_manager.oled_manager import OledManager
from arctis_sound_manager.oled_renderer import OledRenderer
from arctis_sound_manager.settings import GeneralSettings


class _FakeSession:
    def __init__(self, away: bool = False, playing: str = "") -> None:
        self.away = away
        self.playing = playing
        self.away_queries = 0

    def user_away(self) -> bool:
        self.away_queries += 1
        return self.away

    def now_playing(self) -> str:
        return self.playing

    def close(self) -> None:
        pass


def _manager(monkeypatch, *, off_when_away: bool = True) -> tuple[OledManager, list[int]]:
    core = SimpleNamespace(
        usb_device=None,
        _usb_write_lock=threading.Lock(),
        device_config=None,
        general_settings=SimpleNamespace(
            oled_off_when_away=off_when_away, oled_brightness=7,
        ),
    )
    manager = OledManager(core)
    manager._session = _FakeSession()
    levels: list[int] = []
    monkeypatch.setattr(manager, "set_brightness", levels.append)
    sent: list = []
    monkeypatch.setattr(manager, "_send_oled_packet",
                        lambda packet, control=False: sent.append(packet) or True)
    manager._sent = sent
    return manager, levels


# ── format_now_playing ──────────────────────────────────────────────────────

def test_artist_list_and_title():
    assert format_now_playing(
        {"xesam:artist": ["A", "B"], "xesam:title": "Song"}) == "A, B - Song"


def test_artist_as_bare_string():
    assert format_now_playing({"xesam:artist": "A", "xesam:title": "Song"}) == "A - Song"


def test_title_alone_is_shown_artist_alone_is_not():
    assert format_now_playing({"xesam:title": "Song"}) == "Song"
    assert format_now_playing({"xesam:artist": ["A"]}) == ""


# ── DesktopSession failure mode ─────────────────────────────────────────────

def test_a_session_that_cannot_answer_reports_nothing_playing_and_present():
    session = DesktopSession()

    async def _boom():
        raise RuntimeError("no bus")

    assert session._run(_boom(), default="") == ""
    assert session._run(_boom(), default=False) is False
    session.close()


# ── away ────────────────────────────────────────────────────────────────────

def test_away_darkens_the_panel_and_coming_back_restores_it(monkeypatch):
    manager, levels = _manager(monkeypatch)
    manager._last_update_time = 0.0
    manager._session.away = True
    gs = manager._core.general_settings

    assert manager._apply_away(gs) is True
    assert manager._screen_off and manager._off_for_away
    assert len(manager._sent) == 1  # the brightness-0 packet

    # Still away: nothing more is sent.
    assert manager._apply_away(gs) is True
    assert len(manager._sent) == 1

    manager._session.away = False
    assert manager._apply_away(gs) is False
    assert not manager._screen_off and not manager._off_for_away
    assert levels == [7]


def test_a_recent_settings_change_keeps_the_panel_lit_for_a_while(monkeypatch):
    manager, _ = _manager(monkeypatch)
    manager._session.away = True
    manager._last_update_time = oled_mod.datetime.now().timestamp()

    assert manager._apply_away(manager._core.general_settings) is False
    assert not manager._screen_off


def test_disabled_never_asks_the_session(monkeypatch):
    manager, _ = _manager(monkeypatch, off_when_away=False)
    manager._session.away = True
    manager._last_update_time = 0.0

    assert manager._apply_away(manager._core.general_settings) is False
    assert manager._session.away_queries == 0


def test_timeout_screen_off_is_not_woken_by_coming_back(monkeypatch):
    """Only the away state lights the panel back up on its own."""
    manager, levels = _manager(monkeypatch)
    manager._screen_off = True  # the fixed timeout fired
    manager._session.away = False

    manager._apply_away(manager._core.general_settings)
    assert manager._screen_off
    assert levels == []


def test_no_frames_are_sent_while_dark_for_away(monkeypatch):
    manager, _ = _manager(monkeypatch)
    manager._off_for_away = True
    manager._current_image = object()  # would crash crop_frame if reached
    manager._send_current_frame()
    assert manager._sent == []


# ── renderer ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("now_playing", ["", "Artist - A title long enough to overflow the panel"])
def test_media_row_only_takes_space_when_something_plays(now_playing):
    r = OledRenderer()
    common = dict(
        battery_percent=50, charging=False, time_str="12:00", active_profile="P",
        show_profile=False, show_eq=False, show_media=True, now_playing=now_playing,
        display_order=["media"], font_sizes={"media": 10},
    )
    image, header_h = r.render_status_image(**common)
    body = image.crop((0, header_h, r.WIDTH, image.height))
    assert (body.getbbox() is not None) == bool(now_playing)
    assert r.measure_media_text("abc", 10) > 0


# ── settings migration ──────────────────────────────────────────────────────

def test_saved_display_order_gains_media():
    gs = GeneralSettings(oled_display_order=["weather", "eq", "profile"])
    assert "media" in gs.oled_display_order
    assert gs.oled_display_order[:3] == ["weather", "eq", "profile"]


# ── wheel wake ──────────────────────────────────────────────────────────────

def _wheel_manager(monkeypatch, *, custom: bool):
    manager, levels = _manager(monkeypatch)
    manager._core.general_settings.oled_custom_display = custom
    redraws: list[bool] = []
    monkeypatch.setattr(manager, "update_display",
                        lambda activity=True: redraws.append(activity))
    # Run the wake inline instead of on its own thread.
    monkeypatch.setattr(oled_mod.threading, "Thread",
                        lambda target, **k: SimpleNamespace(start=target))
    return manager, levels, redraws


def test_turning_the_wheel_lights_a_dark_panel(monkeypatch):
    manager, levels, redraws = _wheel_manager(monkeypatch, custom=True)
    manager.on_status_changed("station_volume", 20)  # initial read
    manager._screen_off = True
    manager._off_for_away = True

    manager.on_status_changed("station_volume", 21)

    assert not manager._screen_off and not manager._off_for_away
    assert levels == [7]
    assert redraws == [False]


def test_the_first_report_after_start_is_not_a_hand_on_the_wheel(monkeypatch):
    manager, levels, _ = _wheel_manager(monkeypatch, custom=True)
    manager._screen_off = True

    manager.on_status_changed("chat_mix", 50)

    assert manager._screen_off
    assert levels == []


def test_other_status_keys_do_not_wake(monkeypatch):
    manager, levels, _ = _wheel_manager(monkeypatch, custom=True)
    manager.on_status_changed("headset_battery_charge", 5)
    manager._screen_off = True

    manager.on_status_changed("headset_battery_charge", 4)

    assert manager._screen_off
    assert levels == []


def test_with_the_dac_ui_the_wheel_hands_the_panel_back_to_the_firmware(monkeypatch):
    manager, levels, redraws = _wheel_manager(monkeypatch, custom=False)
    manager.on_status_changed("media_mix", 100)
    manager._screen_off = True

    manager.on_status_changed("media_mix", 90)

    assert levels == [7]
    assert redraws == []
    assert len(manager._sent) == 1  # the return-to-UI packet


def test_a_wheel_turn_while_away_keeps_the_panel_lit_for_the_grace_period(monkeypatch):
    manager, _, _ = _wheel_manager(monkeypatch, custom=True)
    manager._session.away = True
    manager.on_status_changed("station_volume", 20)
    manager._screen_off = True
    manager._off_for_away = True

    manager.on_status_changed("station_volume", 22)

    assert manager._apply_away(manager._core.general_settings) is False
    assert not manager._screen_off


# ── screen timeout ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("custom", [True, False])
def test_the_screen_timeout_applies_with_and_without_custom_display(monkeypatch, custom):
    """The slider used to be skipped while Custom Display was on - the setting looked dead."""
    manager, _ = _manager(monkeypatch)
    gs = manager._core.general_settings
    gs.oled_screen_timeout = 10
    gs.oled_custom_display = custom
    manager._last_update_time = oled_mod.datetime.now().timestamp() - 60
    dark: list[int] = []
    monkeypatch.setattr(manager, "_go_dark", lambda: dark.append(1))
    monkeypatch.setattr(manager, "_hand_back_screen", lambda: None)
    monkeypatch.setattr(manager, "update_display", lambda activity=True: None)
    monkeypatch.setattr(manager, "_advance_burn_in", lambda: None)
    monkeypatch.setattr(oled_mod, "_REFRESH_INTERVAL_S", 0.0)
    manager._stop_event.clear()
    ticks = iter([False, False, True])
    monkeypatch.setattr(manager._stop_event, "is_set", lambda: next(ticks, True))
    manager._refresh_loop()
    assert dark == [1] and manager._screen_off


def test_a_zero_timeout_never_darkens_the_panel(monkeypatch):
    manager, _ = _manager(monkeypatch)
    gs = manager._core.general_settings
    gs.oled_screen_timeout = 0
    gs.oled_custom_display = True
    manager._last_update_time = 0.0
    dark: list[int] = []
    monkeypatch.setattr(manager, "_go_dark", lambda: dark.append(1))
    monkeypatch.setattr(manager, "update_display", lambda activity=True: None)
    monkeypatch.setattr(manager, "_advance_burn_in", lambda: None)
    monkeypatch.setattr(oled_mod, "_REFRESH_INTERVAL_S", 0.0)
    manager._stop_event.clear()
    ticks = iter([False, False, True])
    monkeypatch.setattr(manager._stop_event, "is_set", lambda: next(ticks, True))
    manager._refresh_loop()
    assert dark == [] and not manager._screen_off


def test_no_frames_are_sent_while_dark_for_the_timeout(monkeypatch):
    manager, _ = _manager(monkeypatch)
    manager._screen_off = True
    manager._current_image = object()  # would crash crop_frame if reached
    manager._send_current_frame()
    assert manager._sent == []
