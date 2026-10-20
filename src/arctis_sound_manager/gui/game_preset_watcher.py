# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""
game_preset_watcher.py — automatic Sonar Game preset per detected game.

Lives in the tray process. Every few seconds a short-lived worker thread runs
the (blocking) game detection; the result is handed back to the Qt thread, where
the ``AutoPresetController`` decides what to do and the preset is applied through
the tray's ``SonarPresetApplier`` (same flow as picking it in the tray menu).

Nothing long-lived is opened per tick: detection uses context-managed pulsectl
connections inside ``game_presets`` and the worker thread ends after each run.
"""
from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from arctis_sound_manager.gui.tray_eq_presets import SonarPresetApplier
from arctis_sound_manager.i18n import I18n

logger = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 5000


class GamePresetWatcher(QObject):
    # Emitted from the worker thread; delivered on the Qt thread (queued).
    _detected = Signal(object, bool)  # (DetectedGame | None, ok)
    # Emitted on the Qt thread when a preset was applied: (message)
    notify = Signal(str)

    def __init__(self, applier: SonarPresetApplier, parent: QObject | None = None):
        super().__init__(parent)
        from arctis_sound_manager import game_presets as gp

        self._gp = gp
        self._applier = applier
        self._controller = gp.AutoPresetController(
            self._resolve_target, self._can_remember)
        self._last_game = None
        self._was_enabled = False
        self._in_flight = False
        self._pending: tuple[str, str] | None = None  # (kind, preset) being applied

        self._detected.connect(self._on_detected)
        self._applier.done.connect(self._on_applier_done)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

    # ── public ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    @property
    def current_game(self):
        """Game seen by the last tick (None when disabled or no game)."""
        return self._last_game

    @property
    def controller(self):
        return self._controller

    def association_changed(self) -> None:
        """The user changed a per-game choice: re-evaluate right away."""
        self._controller.reassess()
        self.poll_now()

    def poll_now(self) -> None:
        QTimer.singleShot(0, self._poll)

    # ── internals ───────────────────────────────────────────────────────────

    def _resolve_target(self, game) -> str | None:
        from arctis_sound_manager.gui.sonar_page import _list_presets
        return self._gp.target_preset(game, list(_list_presets("game").keys()))

    def _can_remember(self, game) -> bool:
        # An explicit "Do not change" is the user's choice, not ours to overwrite.
        has_entry, preset = self._gp.get_association(game.key)
        return not (has_entry and preset is None)

    @Slot()
    def _poll(self) -> None:
        try:
            enabled = self._gp.is_enabled()
        except Exception:
            logger.exception("game preset: is_enabled failed")
            return
        if not enabled:
            if self._was_enabled:
                self._was_enabled = False
                self._last_game = None
                self._controller.reset()
            return
        self._was_enabled = True
        if self._in_flight or self._applier.is_running():
            return
        self._in_flight = True
        threading.Thread(target=self._detect_worker, name="asm-game-detect", daemon=True).start()

    def _detect_worker(self) -> None:
        game, ok = None, False
        try:
            game = self._gp.detect_current_game()
            ok = True
        except Exception:
            logger.exception("game preset: detection failed")
        try:
            self._detected.emit(game, ok)
        except RuntimeError:
            pass  # watcher destroyed during shutdown

    @Slot(object, bool)
    def _on_detected(self, game, ok: bool) -> None:
        self._in_flight = False
        if not ok:
            return
        try:
            if not self._gp.is_enabled():
                return
            self._last_game = game
            if self._applier.is_running():
                return
            from arctis_sound_manager.gui.sonar_page import _active_preset_name
            current = _active_preset_name("game") or None
            action = self._controller.tick(game, current, time.monotonic())
            if action is None:
                return
            if action.kind == "remember":
                self._gp.set_association(action.game.key, action.game.title, action.preset)
                self.notify.emit(I18n.translate("ui", "auto_preset_remembered").format(
                    preset=action.preset, game=action.game.title))
                return
            self._pending = (action.kind, action.preset)
            self._applier.apply("game", action.preset)
        except Exception:
            self._pending = None
            logger.exception("game preset: tick failed")

    @Slot(bool, str, str)
    def _on_applier_done(self, ok: bool, channel: str, name: str) -> None:
        pending, self._pending = self._pending, None
        if pending is None or pending[1] != name:
            return
        if not ok:
            self._controller.apply_failed(name)
            return
        self.notify.emit(I18n.translate("ui", "auto_preset_applied").format(preset=name))
