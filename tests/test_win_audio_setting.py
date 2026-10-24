"""SetSetting windows_audio_engine: saved by the daemon, applied to the engine process on Windows."""
from __future__ import annotations

import json
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from arctis_sound_manager import dbus_service
from arctis_sound_manager.dbus_service import ArctisManagerDbusSettingsService
from arctis_sound_manager.settings import GeneralSettings

_set_setting = ArctisManagerDbusSettingsService.set_setting.__dict__["__DBUS_METHOD"].fn


def make_service(tmp_path):
    svc = ArctisManagerDbusSettingsService.__new__(ArctisManagerDbusSettingsService)
    svc.core_engine = MagicMock()
    svc.core_engine.oled_manager = None
    svc.logger = logging.getLogger("test_win_audio_setting")
    with patch("arctis_sound_manager.settings.SETTINGS_FOLDER", tmp_path):
        svc.core_engine.general_settings = GeneralSettings()
    return svc


@pytest.fixture
def applied(monkeypatch):
    """Records what the engine service is asked to do."""
    class Calls(list):
        done = threading.Event()
    calls = Calls()

    def fake(logger, enabled, *a, **kw):
        calls.append(enabled)
        calls.done.set()
    monkeypatch.setattr("arctis_sound_manager.win.audio.service.apply_engine_setting", fake)
    return calls


def test_the_default_is_on():
    assert GeneralSettings().windows_audio_engine is True


@pytest.mark.parametrize("value", [False, True])
def test_on_windows_the_value_is_saved_and_applied(tmp_path, monkeypatch, applied, value):
    monkeypatch.setattr(dbus_service, "IS_WINDOWS", True)
    svc = make_service(tmp_path)
    svc.core_engine.general_settings.windows_audio_engine = not value
    with patch("arctis_sound_manager.settings.SETTINGS_FOLDER", tmp_path):
        assert _set_setting(svc, "windows_audio_engine", json.dumps(value)) is True
        assert GeneralSettings.read_from_file().windows_audio_engine is value        # on disk
    assert svc.core_engine.general_settings.windows_audio_engine is value            # and in the daemon
    assert applied.done.wait(5) and applied == [value]


def test_on_linux_it_is_only_stored(tmp_path, monkeypatch, applied):
    monkeypatch.setattr(dbus_service, "IS_WINDOWS", False)
    svc = make_service(tmp_path)
    with patch("arctis_sound_manager.settings.SETTINGS_FOLDER", tmp_path):
        assert _set_setting(svc, "windows_audio_engine", json.dumps(False)) is True
    assert svc.core_engine.general_settings.windows_audio_engine is False
    assert not applied.done.wait(0.2) and applied == []


@pytest.mark.parametrize("bad", [1, 0, "true", None, [True]])
def test_anything_but_a_bool_is_refused(tmp_path, monkeypatch, applied, bad):
    monkeypatch.setattr(dbus_service, "IS_WINDOWS", True)
    svc = make_service(tmp_path)
    with patch("arctis_sound_manager.settings.SETTINGS_FOLDER", tmp_path):
        assert _set_setting(svc, "windows_audio_engine", json.dumps(bad)) is False
    assert svc.core_engine.general_settings.windows_audio_engine is True
    assert not applied.done.wait(0.2) and applied == []


def test_the_daemon_does_not_wait_for_the_engine_to_start(tmp_path, monkeypatch):
    """Starting/stopping blocks (it waits for the process): SetSetting must answer before it is done."""
    monkeypatch.setattr(dbus_service, "IS_WINDOWS", True)
    release, entered = threading.Event(), threading.Event()

    def slow(logger, enabled, *a, **kw):
        entered.set()
        release.wait(5)
    monkeypatch.setattr("arctis_sound_manager.win.audio.service.apply_engine_setting", slow)
    svc = make_service(tmp_path)
    try:
        with patch("arctis_sound_manager.settings.SETTINGS_FOLDER", tmp_path):
            assert _set_setting(svc, "windows_audio_engine", json.dumps(False)) is True     # returned while 'slow' runs
        assert entered.wait(5) and not release.is_set()
    finally:
        release.set()
