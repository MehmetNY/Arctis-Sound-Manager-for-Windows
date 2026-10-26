# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""The DAC's own volume bar takes the screen from the custom display; ASM takes it back by itself.

A user reported that with Custom Display on the picture froze, and that after turning the DAC's volume the custom screen
no longer came back until the setting was switched off and on. The firmware shows its own bar on a wheel turn; frames
sent meanwhile are accepted and not shown. Switching Custom Display off hands the screen back, switching it on draws
again. These tests check that the manager does the same two steps once the wheel is still, once per burst of turns,
never while the display is off or dark on purpose, and that it reports a frame that cannot be delivered.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from arctis_sound_manager import oled_manager as oled_mod
from arctis_sound_manager.oled_manager import OledManager


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(oled_mod, "_RETAKE_QUIET_S", 0.15)
    monkeypatch.setattr(oled_mod, "_RETAKE_GAP_S", 0.02)


def _manager(monkeypatch, *, custom=True):
    core = SimpleNamespace(
        usb_device=None, _usb_write_lock=threading.Lock(), device_config=None,
        general_settings=SimpleNamespace(oled_custom_display=custom, oled_off_when_away=False, oled_brightness=7))
    manager = OledManager(core)
    events: list[str] = []
    monkeypatch.setattr(manager, "_hand_back_screen", lambda: events.append("hand_back"))
    monkeypatch.setattr(manager, "update_display", lambda activity=True: events.append(f"draw(activity={activity})"))
    manager._events = events
    return manager


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_the_first_report_of_a_wheel_key_is_only_remembered(monkeypatch):
    manager = _manager(monkeypatch)
    manager.on_status_changed("station_volume", 40)
    time.sleep(0.4)
    assert manager._events == []


def test_a_turn_gives_the_screen_back_and_draws_again_once_the_wheel_is_still(monkeypatch):
    manager = _manager(monkeypatch)
    manager.on_status_changed("station_volume", 40)          # remembered
    manager.on_status_changed("station_volume", 41)
    assert manager._events == []                              # not while the firmware's bar is up
    assert _wait(lambda: len(manager._events) == 2)
    assert manager._events == ["hand_back", "draw(activity=False)"]


def test_a_burst_of_turns_is_one_retake_after_the_last_turn(monkeypatch):
    manager = _manager(monkeypatch)
    manager.on_status_changed("station_volume", 10)
    started = time.monotonic()
    for value in range(11, 31):                               # twenty clicks, 30 ms apart
        manager.on_status_changed("station_volume", value)
        time.sleep(0.03)
    assert manager._events == []                              # the wheel kept moving: still waiting
    assert _wait(lambda: len(manager._events) == 2)
    assert manager._events == ["hand_back", "draw(activity=False)"]
    assert time.monotonic() - started >= 0.6 + 0.15 - 0.05
    time.sleep(0.4)
    assert len(manager._events) == 2                          # and not repeated


def test_a_turn_during_the_retake_is_not_lost(monkeypatch):
    manager = _manager(monkeypatch)
    manager.on_status_changed("chat_mix", 50)
    manager.on_status_changed("chat_mix", 51)
    assert _wait(lambda: manager._events[:1] == ["hand_back"])
    manager.on_status_changed("chat_mix", 52)                 # while the first retake is drawing
    assert _wait(lambda: manager._events.count("hand_back") == 2)
    assert _wait(lambda: not manager._retake_running)


def test_nothing_happens_while_custom_display_is_off(monkeypatch):
    manager = _manager(monkeypatch, custom=False)
    manager.on_status_changed("station_volume", 40)
    manager.on_status_changed("station_volume", 41)
    time.sleep(0.5)
    assert manager._events == []


def test_a_panel_that_is_dark_is_woken_not_retaken(monkeypatch):
    manager = _manager(monkeypatch)
    manager._screen_off = True
    woken = []
    monkeypatch.setattr(manager, "_wake", lambda: woken.append(1))
    manager.on_status_changed("station_volume", 40)
    manager.on_status_changed("station_volume", 41)
    assert _wait(lambda: woken)
    time.sleep(0.4)
    assert manager._events == []


def test_the_screen_turning_off_before_the_retake_cancels_it(monkeypatch):
    manager = _manager(monkeypatch)
    manager.on_status_changed("station_volume", 40)
    manager.on_status_changed("station_volume", 41)
    manager._screen_off = True                                # the timeout fired while waiting
    time.sleep(0.5)
    assert manager._events == []


def test_other_keys_do_not_retake(monkeypatch):
    manager = _manager(monkeypatch)
    manager.on_status_changed("headset_battery_charge", 40)
    manager.on_status_changed("headset_battery_charge", 41)
    time.sleep(0.4)
    assert manager._events == []


def test_stopping_ends_a_waiting_retake(monkeypatch):
    manager = _manager(monkeypatch)
    monkeypatch.setattr(oled_mod, "_RETAKE_QUIET_S", 30.0)
    manager.on_status_changed("station_volume", 40)
    manager.on_status_changed("station_volume", 41)
    assert _wait(lambda: manager._retake_running)
    manager._stop_event.set()
    assert _wait(lambda: not manager._retake_running)
    assert manager._events == []


# ── undelivered frames are reported ──────────────────────────────────────────

def test_a_frame_that_cannot_be_delivered_is_logged_once_and_then_rarely(monkeypatch, caplog):
    manager = _manager(monkeypatch)
    from PIL import Image
    manager._current_image = Image.new("1", (128, 64), 0)
    manager._header_h = 0
    monkeypatch.setattr(manager, "_take_screen", lambda: None)
    monkeypatch.setattr(manager, "_send_oled_packet", lambda packet, control=False: False)
    manager._last_send_errno = 5
    with caplog.at_level("INFO", logger=oled_mod.logger.name):
        for _ in range(5):
            manager._send_current_frame()
    said = [r for r in caplog.records if "could not be delivered" in r.getMessage()]
    assert len(said) == 1 and "errno 5" in said[0].getMessage()
    assert manager._failed_frames == 5
    # a delivered frame clears the count
    monkeypatch.setattr(manager, "_send_oled_packet", lambda packet, control=False: True)
    manager._send_current_frame()
    assert manager._failed_frames == 0
