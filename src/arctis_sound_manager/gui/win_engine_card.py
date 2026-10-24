# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The "Audio engine" card of the Settings page (Windows): the on/off switch, how the engine is doing,
and the way into the set-up assistant.

The switch is saved the way the Channels page saves its Aux switch: into the settings file (so this
window shows it at once) *and* to the daemon through ``SetSetting`` — the daemon keeps its own copy of
the settings and would otherwise write the old value back the next time it saves something, and it is the
daemon that starts and stops the engine process.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

import arctis_sound_manager.gui.theme as _theme
from arctis_sound_manager.gui.qt_widgets.q_dual_state import QDualState
from arctis_sound_manager.i18n import I18n
from arctis_sound_manager.win.audio.bridge import shared_bridge
from arctis_sound_manager.win.audio.engine_config import load_config
from arctis_sound_manager.win.audio.service import engine_enabled
from arctis_sound_manager.win.audio.strings import t
from arctis_sound_manager.win.audio.summary import EngineState, engine_state

_log = logging.getLogger(__name__)

_SETTING = "windows_audio_engine"


def persist_engine_setting(enabled: bool) -> None:
    """Save the switch to the settings file and tell the daemon (which starts/stops the engine)."""
    try:
        from arctis_sound_manager.settings import GeneralSettings
        gs = GeneralSettings.read_from_file()
        gs.windows_audio_engine = bool(enabled)
        gs.write_to_file()
    except Exception:                                            # noqa: BLE001 — the daemon's copy is the one that counts
        _log.warning("could not persist %s", _SETTING, exc_info=True)
    try:
        from arctis_sound_manager.gui.dbus_wrapper import DbusWrapper
        DbusWrapper.change_setting(_SETTING, bool(enabled))
    except Exception:                                            # noqa: BLE001
        _log.debug("could not notify the daemon about %s", _SETTING, exc_info=True)


class EngineCard(QWidget):
    def __init__(self, parent=None, bridge=None, config_dir: Path | None = None,
                 read_enabled: Callable[[], bool] = engine_enabled,
                 write_enabled: Callable[[bool], None] = persist_engine_setting,
                 open_setup: Callable[[], None] | None = None, interval_ms: int = 2000,
                 lang: Callable[[], str] = I18n.current_lang):
        super().__init__(parent)
        self._bridge = bridge or shared_bridge()
        self._config_dir = config_dir
        self._read_enabled, self._write_enabled, self._lang = read_enabled, write_enabled, lang
        self._open_setup = open_setup
        self._state: EngineState | None = None
        lang_code = lang()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 4)
        self._switch_label = QLabel(t("engine_setting", lang_code))
        self._switch_label.setWordWrap(True)
        self._switch_label.setFixedWidth(200)
        row.addWidget(self._switch_label)
        self._toggle = QDualState(off_text=I18n.translate("settings_values", "off"),
                                  on_text=I18n.translate("settings_values", "on"),
                                  init_state="right" if self._read_enabled() else "left", accent=True)
        self._toggle.setToolTip(t("engine_setting_hint", lang_code))
        self._toggle.checkStateChanged.connect(self._on_toggled)
        row.addWidget(self._toggle)
        row.addStretch(1)
        outer.addLayout(row)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        status_row.addWidget(self._status, 1)
        self._setup_btn = QPushButton(t("banner_setup", lang_code))
        self._setup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._setup_btn.clicked.connect(self._setup)
        status_row.addWidget(self._setup_btn)
        outer.addLayout(status_row)

        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.refresh)
        self.apply_theme()
        self.refresh()

    # ── what the page calls ─────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._toggle.toggle.isChecked()

    @property
    def state(self) -> EngineState | None:
        return self._state

    def status_text(self) -> str:
        return self._status.text()

    def update_settings(self, settings: dict) -> None:
        """Follow the daemon's copy of the setting (the page gets it with every ``GetSettings`` reply)."""
        general = (settings or {}).get("general")
        value = general.get(_SETTING) if isinstance(general, dict) else None
        if isinstance(value, bool) and value != self.enabled:
            self._toggle.set_state("right" if value else "left")
            self.refresh()

    def apply_theme(self) -> None:
        self._switch_label.setStyleSheet(f"color: {_theme.c('TEXT_PRIMARY')}; font-size: 10pt; background: transparent;")
        self._status.setStyleSheet(f"color: {_theme.c('TEXT_SECONDARY')}; font-size: 10pt; background: transparent;")
        self._setup_btn.setStyleSheet(
            f"QPushButton {{ background-color: {_theme.c('BG_BUTTON')}; color: {_theme.c('TEXT_PRIMARY')};"
            f" border: 1px solid {_theme.c('BG_BUTTON_HOVER')}; border-radius: 6px; padding: 5px 14px; font-size: 10pt; }}"
            f"QPushButton:hover {{ background-color: {_theme.c('BG_BUTTON_HOVER')}; }}")

    # ── state ───────────────────────────────────────────────────────────────

    def refresh(self) -> EngineState:
        try:
            configured = not load_config(self._config_dir).problems()
        except Exception:                                        # noqa: BLE001
            configured = False
        self._state = engine_state(self._bridge.status(), enabled=self.enabled, configured=configured,
                                   lang=self._lang())
        self._status.setText(self._state.text)
        return self._state

    def showEvent(self, event) -> None:                          # noqa: N802 — Qt
        super().showEvent(event)
        self._timer.start()
        self.refresh()

    def hideEvent(self, event) -> None:                          # noqa: N802 — Qt
        super().hideEvent(event)
        self._timer.stop()

    # ── user actions ────────────────────────────────────────────────────────

    def _on_toggled(self, state) -> None:
        self._write_enabled(state == Qt.CheckState.Checked)
        self.refresh()

    def _setup(self) -> None:
        if self._open_setup is not None:
            self._open_setup()
            return
        from arctis_sound_manager.gui.win_audio_setup import AudioEngineSetupDialog
        AudioEngineSetupDialog(self.window(), config_dir=self._config_dir).exec()
        self.refresh()
