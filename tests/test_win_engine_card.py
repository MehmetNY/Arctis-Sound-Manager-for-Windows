"""The "Audio engine" card of the Settings page."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                                           # noqa: E402
from PySide6.QtWidgets import QApplication                                              # noqa: E402

from arctis_sound_manager.gui import win_engine_card as mod                              # noqa: E402
from arctis_sound_manager.gui.win_engine_card import EngineCard, persist_engine_setting   # noqa: E402
from arctis_sound_manager.settings import GeneralSettings                                # noqa: E402
from arctis_sound_manager.win.audio.engine_config import EngineConfig, save_config       # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeBridge:
    def __init__(self, status=None):
        self.value = status

    def status(self, max_age=None):
        return self.value


OK = {"running": True, "profile": "balanced", "latency_ms": 70.0, "endpoints": {"in:game": {"state": "running"}}}


def configure(tmp_path):
    save_config(EngineConfig(inputs={"game": "CABLE-A Output", "chat": "", "media": "", "aux": ""},
                             outputs={"headset": "Arctis"}), tmp_path)


def make(tmp_path, enabled=True, bridge=None, **kw):
    written = []
    card = EngineCard(bridge=bridge or FakeBridge(), config_dir=tmp_path, read_enabled=lambda: enabled,
                      write_enabled=written.append, lang=lambda: "en", **kw)
    return card, written


def test_the_switch_shows_the_saved_setting(tmp_path, qapp):
    assert make(tmp_path, True)[0].enabled
    assert not make(tmp_path, False)[0].enabled


def test_flipping_the_switch_saves_it_and_the_status_follows(tmp_path, qapp):
    configure(tmp_path)
    card, written = make(tmp_path, True)
    assert card.state.code == "stopped"
    card._toggle.toggle.setChecked(False)
    assert written == [False] and card.state.code == "off" and "switched off" in card.status_text()
    card._toggle.toggle.setChecked(True)
    assert written == [False, True] and card.state.code == "stopped"


def test_the_status_line_says_how_the_engine_is_doing(tmp_path, qapp):
    card, _ = make(tmp_path, True)
    assert card.state.code == "unconfigured" and "not set up" in card.status_text()
    configure(tmp_path)
    bridge = FakeBridge(OK)
    card, _ = make(tmp_path, True, bridge=bridge)
    assert card.state.healthy and "running" in card.status_text() and "70 ms" in card.status_text()
    bridge.value = None
    card.refresh()
    assert card.state.code == "stopped"


def test_following_the_daemon_moves_the_switch_without_saving_again(tmp_path, qapp):
    card, written = make(tmp_path, True)
    card.update_settings({"general": {"windows_audio_engine": False}})
    assert not card.enabled and written == []                    # no write-back loop
    assert card.state.code == "off"
    card.update_settings({"general": {"windows_audio_engine": True}})
    assert card.enabled and written == []


@pytest.mark.parametrize("settings", [None, {}, {"general": None}, {"general": {}}, {"general": {"windows_audio_engine": 0}},
                                      {"general": {"windows_audio_engine": "false"}}, {"device": {}}])
def test_a_reply_without_a_proper_value_changes_nothing(tmp_path, qapp, settings):
    card, written = make(tmp_path, True)
    card.update_settings(settings)
    assert card.enabled and written == []


def test_the_setup_button_opens_the_assistant(tmp_path, qapp):
    opened = []
    card, _ = make(tmp_path, True, open_setup=lambda: opened.append(1))
    card._setup_btn.click()
    assert opened == [1]


def test_it_only_polls_while_the_card_is_on_screen(tmp_path, qapp):
    card, _ = make(tmp_path, True)
    assert not card._timer.isActive()
    card.show()
    assert card._timer.isActive()
    card.hide()
    assert not card._timer.isActive()


def test_showing_the_card_refreshes_it_at_once(tmp_path, qapp):
    configure(tmp_path)
    bridge = FakeBridge(None)
    card, _ = make(tmp_path, True, bridge=bridge)
    bridge.value = OK
    card.show()
    assert card.state.healthy
    card.hide()


def test_the_texts_follow_the_ui_language(tmp_path, qapp):
    card = EngineCard(bridge=FakeBridge(), config_dir=tmp_path, read_enabled=lambda: True,
                      write_enabled=lambda v: None, lang=lambda: "tr")
    assert card._setup_btn.text() == "Kurulum..." and "kurulmadı" in card.status_text()
    assert "ASM ses motoruyla" in card._switch_label.text()


def test_the_switch_explains_what_switching_off_does(tmp_path, qapp):
    card, _ = make(tmp_path, True)
    assert "silent" in card._toggle.toolTip()


# ── saving ──────────────────────────────────────────────────────────────────

def test_the_switch_is_written_to_the_settings_file_and_sent_to_the_daemon(tmp_path):
    with patch("arctis_sound_manager.settings.SETTINGS_FOLDER", tmp_path), \
         patch("arctis_sound_manager.gui.dbus_wrapper.DbusWrapper.change_setting") as send:
        persist_engine_setting(False)
        assert GeneralSettings.read_from_file().windows_audio_engine is False
        send.assert_called_once_with("windows_audio_engine", False)
        persist_engine_setting(True)
        assert GeneralSettings.read_from_file().windows_audio_engine is True
        send.assert_called_with("windows_audio_engine", True)


def test_saving_keeps_the_other_settings(tmp_path):
    with patch("arctis_sound_manager.settings.SETTINGS_FOLDER", tmp_path), \
         patch("arctis_sound_manager.gui.dbus_wrapper.DbusWrapper.change_setting"):
        gs = GeneralSettings()
        gs.aux_enabled = True
        gs.write_to_file()
        persist_engine_setting(False)
        after = GeneralSettings.read_from_file()
        assert after.aux_enabled is True and after.windows_audio_engine is False


def test_a_daemon_that_is_not_there_does_not_break_saving(tmp_path):
    with patch("arctis_sound_manager.settings.SETTINGS_FOLDER", tmp_path), \
         patch("arctis_sound_manager.gui.dbus_wrapper.DbusWrapper.change_setting", side_effect=OSError("no bus")):
        persist_engine_setting(False)
        assert GeneralSettings.read_from_file().windows_audio_engine is False


def test_an_unwritable_settings_file_still_tells_the_daemon(tmp_path):
    with patch("arctis_sound_manager.settings.GeneralSettings.read_from_file", side_effect=OSError("locked")), \
         patch("arctis_sound_manager.gui.dbus_wrapper.DbusWrapper.change_setting") as send:
        persist_engine_setting(False)
        send.assert_called_once_with("windows_audio_engine", False)


# ── on the Settings page ────────────────────────────────────────────────────

def test_the_settings_page_has_the_card_on_windows_only(qapp, monkeypatch):
    from arctis_sound_manager.gui import device_page
    monkeypatch.setattr(device_page, "IS_WINDOWS", True)
    page = device_page.DevicePage()
    assert isinstance(page._engine_card, EngineCard)
    assert page._engine_card.parent() is not None
    frame = page._engine_card.parentWidget()
    while frame is not None and frame.objectName() != "settingsCard":
        frame = frame.parentWidget()
    assert frame is not None and frame in page._cards and frame.title.text() == "Audio engine"

    monkeypatch.setattr(device_page, "IS_WINDOWS", False)
    assert device_page.DevicePage()._engine_card is None


def test_the_settings_page_passes_the_daemons_settings_to_the_card(qapp, monkeypatch):
    from arctis_sound_manager.gui import device_page
    monkeypatch.setattr(device_page, "IS_WINDOWS", True)
    monkeypatch.setattr(mod, "persist_engine_setting", lambda v: None)
    page = device_page.DevicePage()
    seen = []
    monkeypatch.setattr(page._engine_card, "update_settings", seen.append)
    page.update_settings({"general": {"windows_audio_engine": False}})
    assert seen == [{"general": {"windows_audio_engine": False}}]


def test_the_settings_page_on_linux_still_takes_settings_replies(qapp, monkeypatch):
    from arctis_sound_manager.gui import device_page
    monkeypatch.setattr(device_page, "IS_WINDOWS", False)
    device_page.DevicePage().update_settings({"general": {"windows_audio_engine": False}})
