# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Windows audio-engine set-up assistant: which cable carries which channel, which headset, which mic.

Opened from the status banner and from Settings. It lists the sound devices (taken by a separate process,
so devices installed since ASM started show up), pre-selects what the engine is configured with — or, the
first time, its best guess (:func:`~arctis_sound_manager.win.audio.engine_config.suggest_config`) — and on
*Save and apply* writes ``win_audio_engine.json``, starts the engine if it is not running and tells it to
rebuild. Every blocking step runs in a worker thread.
"""
from __future__ import annotations

import copy
import logging
import subprocess
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

import arctis_sound_manager.gui.theme as _theme
from arctis_sound_manager.gui.qt_widgets.no_wheel_combo import NoWheelComboBox
from arctis_sound_manager.i18n import I18n
from arctis_sound_manager.win.audio.backend import DeviceInfo
from arctis_sound_manager.win.audio.bridge import shared_bridge
from arctis_sound_manager.win.audio.control import EngineClient, EngineUnavailable
from arctis_sound_manager.win.audio.engine_config import (CHANNELS, PROFILES, EngineConfig, _CABLE, load_config,
                                                          save_config, suggest_config)
from arctis_sound_manager.win.audio.host import subprocess_probe
from arctis_sound_manager.win.audio.launcher import MODULE, start_engine, stop_engine
from arctis_sound_manager.win.audio.strings import t
from arctis_sound_manager.win.audio.summary import endpoint_lines, engine_state

_log = logging.getLogger(__name__)

LATENCY_ORDER = ("low", "balanced", "safe")


def _lang() -> str:
    try:
        return I18n.current_lang()
    except Exception:                                            # noqa: BLE001
        return "en"


# ── what each drop-down offers ───────────────────────────────────────────────

def _pool(devices: list[DeviceInfo]) -> list[DeviceInfo]:
    return [d for d in devices if d.wasapi] or list(devices)


def candidates(devices: list[DeviceInfo]) -> dict[str, list[DeviceInfo]]:
    """The devices each kind of drop-down lists (WASAPI ones only when there are any), cables first."""
    pool = _pool(devices)
    inputs = [d for d in pool if d.max_inputs > 0]
    outputs = [d for d in pool if d.max_outputs > 0]

    def cables_first(items, cable):
        return sorted(items, key=lambda d: (not (bool(_CABLE.search(d.name)) == cable), d.name.lower()))
    return {
        "cable_in": cables_first(inputs, True),                  # the Output end of a cable, which the engine records
        "mic": cables_first(inputs, False),
        "headset": cables_first(outputs, False),
        "cable_out": cables_first(outputs, True),                # the Input end of a cable, which the engine plays into
    }


def combo_items(options: list[DeviceInfo], current: str, optional: bool, lang: str | None = None) -> list[tuple[str, str]]:
    """``(label, config value)`` pairs; the current value is kept even if its device is gone."""
    items: list[tuple[str, str]] = [(t("none", lang), "")] if optional else []
    seen = set()
    for d in options:
        if d.name not in seen:
            seen.add(d.name)
            items.append((d.name, d.name))
    if current and not any(_matches(current, value) for _label, value in items):
        items.append((t("not_found", lang, name=current), current))
    return items


def _matches(current: str, value: str) -> bool:
    return bool(value) and current.lower() in value.lower()


def pick_index(items: list[tuple[str, str]], current: str) -> int:
    """Which item a config value points at (the shortest name containing it, as the engine would pick), else 0."""
    hits = [(len(value), i) for i, (_label, value) in enumerate(items) if _matches(current, value)]
    return min(hits)[1] if hits else 0


def selftest_text(config_dir: Path | None, timeout: float = 120.0) -> str:
    cmd = [sys.executable, "-m", MODULE, "--selftest"] + (["--config-dir", str(config_dir)] if config_dir else [])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return (result.stdout + result.stderr).strip() or f"exit code {result.returncode}"


# ── workers ──────────────────────────────────────────────────────────────────

class _Task(QThread):
    done = Signal(object, object)                                # (result, error)

    def __init__(self, fn: Callable[[], object]):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn(), None)
        except Exception as exc:                                 # noqa: BLE001 — reported in the dialog
            self.done.emit(None, exc)


# ── the dialog ───────────────────────────────────────────────────────────────

class AudioEngineSetupDialog(QDialog):
    sig_changed = Signal()                                       # the configuration was saved

    def __init__(self, parent=None, config_dir: Path | None = None,
                 probe: Callable[[], list[DeviceInfo]] = subprocess_probe,
                 ensure_engine: Callable[..., bool] = start_engine,
                 stop: Callable[..., bool] = stop_engine,
                 selftest: Callable[[Path | None], str] = selftest_text,
                 client_factory: Callable[..., EngineClient] = EngineClient, bridge=None,
                 synchronous: bool = False, poll_ms: int = 1500):
        super().__init__(parent)
        self._config_dir = config_dir
        self._probe, self._ensure, self._stop, self._selftest = probe, ensure_engine, stop, selftest
        self._client_factory = client_factory
        self._bridge = bridge or shared_bridge()
        self._synchronous = synchronous
        self._devices: list[DeviceInfo] = []
        self._tasks: list[_Task] = []
        self._lang = _lang()
        self._config = load_config(config_dir)

        self.setWindowTitle(t("dialog_title", self._lang))
        self.setMinimumWidth(640)
        self.setStyleSheet(f"background-color: {_theme.c('BG_MAIN')}; color: {_theme.c('TEXT_PRIMARY')};")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(10)

        intro = QLabel(t("dialog_intro", self._lang))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {_theme.c('TEXT_SECONDARY')}; font-size: 10pt; background: transparent;")
        root.addWidget(intro)

        self._state_lbl = QLabel("")
        self._state_lbl.setWordWrap(True)
        self._state_lbl.setStyleSheet(f"color: {_theme.c('ACCENT')}; font-size: 10pt; background: transparent;")
        root.addWidget(self._state_lbl)

        form = QFormLayout()
        form.setSpacing(8)
        self._combos: dict[str, NoWheelComboBox] = {}
        rows = [(f"in:{ch}", f"label_{ch}") for ch in CHANNELS] + [
            ("out:headset", "label_headset"), ("out:stream", "label_stream"),
            ("mic:in", "label_mic_in"), ("mic:out", "label_mic_out")]
        for key, label_key in rows:
            combo = NoWheelComboBox()
            combo.setMinimumWidth(360)
            self._combos[key] = combo
            form.addRow(t(label_key, self._lang), combo)
        self._latency = NoWheelComboBox()
        for name in LATENCY_ORDER:
            ms = int(round(PROFILES[name].approx_latency_ms()))
            self._latency.addItem(t(f"latency_{name}", self._lang, ms=ms), name)
        form.addRow(t("label_latency", self._lang), self._latency)
        self._limiter = QCheckBox(t("label_limiter", self._lang))
        form.addRow("", self._limiter)
        root.addLayout(form)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(110)
        self._log.setStyleSheet(f"background-color: {_theme.c('BG_CARD')}; color: {_theme.c('TEXT_PRIMARY')};"
                                f"border: 1px solid {_theme.c('BG_BUTTON_HOVER')}; font-size: 9pt;")
        root.addWidget(self._log, 1)

        buttons = QHBoxLayout()
        self._detect_btn = QPushButton(t("btn_detect", self._lang))
        self._test_btn = QPushButton(t("btn_test", self._lang))
        self._save_btn = QPushButton(t("btn_save", self._lang))
        self._close_btn = QPushButton(t("btn_close", self._lang))
        for b in (self._detect_btn, self._test_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(self._save_btn)
        buttons.addWidget(self._close_btn)
        root.addLayout(buttons)
        self._detect_btn.clicked.connect(self.detect)
        self._test_btn.clicked.connect(self.test)
        self._save_btn.clicked.connect(self.save)
        self._close_btn.clicked.connect(self.close)

        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self.refresh_state)
        self._set_busy(True)
        self._log.setPlainText(t("scanning", self._lang))
        self.scan()
        if not synchronous:
            self._timer.start()
        self.refresh_state()

    # ── threads ─────────────────────────────────────────────────────────────

    def _run(self, fn: Callable[[], object], then: Callable[[object, object], None]) -> None:
        if self._synchronous:
            try:
                then(fn(), None)
            except Exception as exc:                             # noqa: BLE001
                then(None, exc)
            return
        task = _Task(fn)
        self._tasks.append(task)
        task.done.connect(lambda result, error, task=task: (then(result, error), self._tasks.remove(task)))
        task.start()

    def _set_busy(self, busy: bool) -> None:
        for b in (self._detect_btn, self._test_btn, self._save_btn):
            b.setEnabled(not busy)

    def closeEvent(self, event) -> None:                         # noqa: N802 — Qt API
        self._timer.stop()
        for task in list(self._tasks):
            task.wait(3000)
        super().closeEvent(event)

    # ── scanning ────────────────────────────────────────────────────────────

    def scan(self) -> None:
        self._run(self._probe, self._on_scanned)

    def _on_scanned(self, devices, error) -> None:
        self._set_busy(False)
        if error is not None:
            self._log.setPlainText(t("scan_failed", self._lang, error=str(error)))
            return
        self._devices = list(devices)
        fresh = not (self._config.outputs.get("headset") or any(self._config.inputs.get(c) for c in CHANNELS))
        self.populate(suggest_config(self._devices) if fresh else self._config)
        cables = [d for d in self._devices if _CABLE.search(d.name)]
        self._log.setPlainText("" if cables else t("no_cables", self._lang))

    def populate(self, cfg: EngineConfig) -> None:
        cand = candidates(self._devices)
        wanted = {f"in:{ch}": (cfg.inputs.get(ch, ""), "cable_in", True) for ch in CHANNELS}      # any subset works
        wanted.update({"out:headset": (cfg.outputs.get("headset", ""), "headset", False),
                       "out:stream": (cfg.outputs.get("stream", ""), "cable_out", True),
                       "mic:in": (cfg.mic_input, "mic", True),
                       "mic:out": (cfg.mic_output, "cable_out", True)})
        for key, (current, pool, optional) in wanted.items():
            items = combo_items(cand[pool], current, optional, self._lang)
            combo = self._combos[key]
            combo.blockSignals(True)
            combo.clear()
            for label, value in items:
                combo.addItem(label, value)
            combo.setCurrentIndex(pick_index(items, current) if current else 0)
            combo.blockSignals(False)
        index = self._latency.findData(cfg.latency if cfg.latency in LATENCY_ORDER else "balanced")
        self._latency.setCurrentIndex(max(0, index))
        self._limiter.setChecked(bool(cfg.master_limiter))

    def detect(self) -> None:
        self.populate(suggest_config(self._devices))

    # ── reading the form ────────────────────────────────────────────────────

    def selected_config(self) -> EngineConfig:
        def value(key: str) -> str:
            return str(self._combos[key].currentData() or "")
        cfg = copy.deepcopy(self._config)                        # keeps what the form has no control for (blocks, routing, ...)
        cfg.inputs = {ch: value(f"in:{ch}") for ch in CHANNELS}
        cfg.outputs = {**{b: n for b, n in self._config.outputs.items() if b not in ("headset", "stream")},
                       "headset": value("out:headset")}
        if value("out:stream"):                                  # Streamer mode's cable; no entry when it is not used
            cfg.outputs["stream"] = value("out:stream")
        cfg.mic_input, cfg.mic_output = value("mic:in"), value("mic:out")
        cfg.latency = str(self._latency.currentData() or "balanced")
        cfg.master_limiter = self._limiter.isChecked()
        return cfg

    # ── save / test ─────────────────────────────────────────────────────────

    def save(self) -> None:
        cfg = self.selected_config()
        if not cfg.outputs.get("headset") or not any(cfg.inputs.values()):
            self._log.setPlainText(t("nothing_to_save", self._lang))
            return
        save_config(cfg, self._config_dir)
        self._config = cfg
        self.sig_changed.emit()
        self._set_busy(True)
        self._run(self._apply, self._on_applied)

    def _apply(self):
        """Worker: make an engine run and have it rebuild from the file just written."""
        up = self._ensure(self._config_dir, wait=10.0)
        if not up:
            return {"started": False}
        client = self._client_factory(self._config_dir, timeout=20.0)
        reply = client.reconfigure()
        return {"started": True, "problems": reply.get("problems") or []}

    def _on_applied(self, result, error) -> None:
        self._set_busy(False)
        if error is not None or not result:
            self._log.setPlainText(t("start_failed", self._lang))
        elif not result["started"]:
            self._log.setPlainText(t("start_failed", self._lang))
        elif result["problems"]:
            self._log.setPlainText(t("saved_problems", self._lang, problems="; ".join(result["problems"])))
        else:
            self._log.setPlainText(t("saved", self._lang) + "\n\n" + t("next_steps", self._lang))
        self.refresh_state()

    def test(self) -> None:
        self._set_busy(True)
        self._log.setPlainText(t("testing", self._lang))
        self._run(self._do_test, self._on_tested)

    def _do_test(self):
        was_running = self._client_factory(self._config_dir, timeout=0.5).available()
        if was_running:
            self._stop(self._config_dir)                         # the test plays a tone into the cables itself
        try:
            return self._selftest(self._config_dir)
        finally:
            if was_running:
                self._ensure(self._config_dir, wait=10.0)

    def _on_tested(self, result, error) -> None:
        self._set_busy(False)
        self._log.setPlainText(str(result) if error is None else f"{type(error).__name__}: {error}")
        self.refresh_state()

    # ── live state ──────────────────────────────────────────────────────────

    def refresh_state(self) -> None:
        status = self._bridge.status()
        cfg_ok = not load_config(self._config_dir).problems()
        state = engine_state(status, configured=cfg_ok, lang=self._lang)
        lines = endpoint_lines(status)
        self._state_lbl.setText(state.text + ("\n" + "  |  ".join(lines) if lines else ""))
