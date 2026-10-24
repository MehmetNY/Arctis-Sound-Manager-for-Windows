# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The strip at the top of the Channels and Equalizer pages (Windows) that says how the audio engine is doing.

Hidden while the engine runs fine; otherwise it says what is wrong and offers the fix: *Set up...* opens the
assistant (:mod:`.win_audio_setup`), *Start* launches the engine. It polls the engine's cached status (never
blocking) every couple of seconds while it is on screen.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

import arctis_sound_manager.gui.theme as _theme
from arctis_sound_manager.i18n import I18n
from arctis_sound_manager.win.audio.bridge import shared_bridge
from arctis_sound_manager.win.audio.engine_config import load_config
from arctis_sound_manager.win.audio.launcher import start_engine
from arctis_sound_manager.win.audio.service import engine_enabled
from arctis_sound_manager.win.audio.strings import t
from arctis_sound_manager.win.audio.summary import EngineState, engine_state

_log = logging.getLogger(__name__)


class EngineBanner(QFrame):
    sig_state = Signal(str)                                      # the state code, when it changes

    def __init__(self, parent=None, bridge=None, config_dir: Path | None = None,
                 enabled: Callable[[], bool] = engine_enabled, starter: Callable[..., bool] = start_engine,
                 open_setup: Callable[[], None] | None = None, interval_ms: int = 2000,
                 lang: Callable[[], str] = I18n.current_lang):
        super().__init__(parent)
        self._bridge = bridge or shared_bridge()
        self._config_dir = config_dir
        self._enabled, self._starter, self._lang = enabled, starter, lang
        self._open_setup = open_setup
        self._on_page = False                                    # is a page that needs the engine showing?
        self._state: EngineState | None = None
        self._starting = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 16, 6)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        lay.addWidget(self._label, 1)
        self._start_btn = QPushButton("")
        self._setup_btn = QPushButton("")
        for b in (self._start_btn, self._setup_btn):
            lay.addWidget(b)
        self._start_btn.clicked.connect(self.start)
        self._setup_btn.clicked.connect(self._setup)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.refresh)
        self.apply_theme()
        self.setVisible(False)
        self.refresh()

    # ── what the rest of the window calls ───────────────────────────────────

    def text(self) -> str:
        return self._label.text()

    @property
    def state(self) -> EngineState | None:
        return self._state

    def set_page_visible(self, on_engine_page: bool) -> None:
        """The window tells us whether the current page is one that needs the engine."""
        self._on_page = on_engine_page
        if on_engine_page:
            self._timer.start()
            self.refresh()
        else:
            self._timer.stop()
        self._update_visibility()

    def apply_theme(self) -> None:
        self.setStyleSheet(
            f"EngineBanner {{ background-color: {_theme.c('BG_CARD')}; border-bottom: 2px solid {_theme.c('ACCENT')}; }}"
            f"QLabel {{ color: {_theme.c('TEXT_PRIMARY')}; font-size: 9pt; background: transparent; }}"
            f"QPushButton {{ background-color: {_theme.c('BG_BUTTON')}; color: {_theme.c('TEXT_PRIMARY')}; border: none;"
            f" border-radius: 6px; padding: 5px 14px; font-size: 9pt; }}"
            f"QPushButton:hover {{ background-color: {_theme.c('BG_BUTTON_HOVER')}; }}")

    # ── state ───────────────────────────────────────────────────────────────

    def refresh(self) -> EngineState:
        lang = self._lang()
        try:
            configured = not load_config(self._config_dir).problems()
        except Exception:                                        # noqa: BLE001 — never let a banner break the page
            configured = False
        state = engine_state(self._bridge.status(), enabled=self._enabled(), configured=configured, lang=lang)
        if self._starting and state.code in ("stopped", "starting"):
            state = EngineState("starting", t("banner_starting", lang))
        elif state.code not in ("stopped", "starting"):
            self._starting = False
        changed = self._state is None or state.code != self._state.code
        self._state = state
        self._label.setText(state.text)
        self._start_btn.setText(t("banner_start", lang))
        self._setup_btn.setText(t("banner_setup", lang))
        self._start_btn.setVisible(state.code == "stopped")
        self._setup_btn.setVisible(state.code in ("unconfigured", "problem", "stopped"))
        self._update_visibility()
        if changed:
            self.sig_state.emit(state.code)
        return state

    def _update_visibility(self) -> None:
        shown = self._on_page and self._state is not None and not self._state.healthy and self._state.code != "off"
        self.setVisible(shown)

    # ── buttons ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the engine (off the GUI thread; the banner follows through its polling)."""
        if self._starting:
            return
        self._starting = True
        self.refresh()
        threading.Thread(target=self._start_worker, name="asm-engine-start", daemon=True).start()

    def _start_worker(self) -> None:
        try:
            self._starter(self._config_dir, wait=15.0)
        except Exception:                                        # noqa: BLE001
            _log.exception("could not start the audio engine")

    def _setup(self) -> None:
        if self._open_setup is not None:
            self._open_setup()
            return
        from arctis_sound_manager.gui.win_audio_setup import AudioEngineSetupDialog
        dialog = AudioEngineSetupDialog(self.window(), config_dir=self._config_dir)
        dialog.exec()
        self.refresh()
