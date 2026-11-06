# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Windows "Audio devices" window: which headset, and how the microphone effect is doing.

ASM chooses the headset by itself the first time it runs (and again whenever its choice no longer exists),
so this window is only for correcting a wrong guess. The microphone has nothing to choose: the processed one reaches
programs through the headset's own microphone (:mod:`arctis_sound_manager.win.fx`), so the window only says how that
effect is doing and offers to set it up or switch it off. It lists the sound devices (taken by a separate process,
so devices plugged in since ASM started show up), shows what the engine is configured with and, on
*Save and apply*, writes ``win_audio_engine.json``, starts the engine if it is not running and tells it to
rebuild. *Choose automatically* hands the choice back to ASM. Every blocking step runs in a worker thread.

Programs need no setting here: the engine takes each program's sound from Windows itself and sorts it into
the Game / Chat / Media / Aux channels (:mod:`arctis_sound_manager.win.audio.tap`).
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

import arctis_sound_manager.gui.theme as _theme
from arctis_sound_manager.gui.qt_widgets.no_wheel_combo import NoWheelComboBox
from arctis_sound_manager.i18n import I18n
from arctis_sound_manager.win.audio.backend import DeviceInfo
from arctis_sound_manager.win.audio.bridge import shared_bridge
from arctis_sound_manager.win.audio.control import EngineClient
from arctis_sound_manager.win.audio.engine_config import (CHANNELS, PROFILES, EngineConfig, config_problems, is_virtual, load_config,
                                                          save_config, suggest_config)
from arctis_sound_manager.win.audio.host import subprocess_probe
from arctis_sound_manager.win.audio.launcher import start_engine
from arctis_sound_manager.win.audio.strings import t
from arctis_sound_manager.win.audio.summary import endpoint_lines, engine_state
from arctis_sound_manager.win.audio.tap import is_tap_device
from arctis_sound_manager.win.fx import state as fxstate

_log = logging.getLogger(__name__)

LATENCY_ORDER = ("low", "balanced", "safe")


def _lang() -> str:
    try:
        return I18n.current_lang()
    except Exception:                                            # noqa: BLE001
        return "en"


# ── what each drop-down offers ───────────────────────────────────────────────

def _pool(devices: list[DeviceInfo]) -> list[DeviceInfo]:
    real = [d for d in devices if not is_tap_device(d)]          # the tap's own capture devices are the engine's, not the user's
    return [d for d in real if d.wasapi] or list(real)


def candidates(devices: list[DeviceInfo]) -> dict[str, list[DeviceInfo]]:
    """The devices each drop-down lists (WASAPI ones only when there are any), the likeliest first. Virtual devices that
    other software brought (a voice mixer, somebody's virtual cable) are never listed: ASM does not use them."""
    pool = [d for d in _pool(devices) if d.max_outputs > 0 and not is_virtual(d.name)]
    ordered = sorted(pool, key=lambda d: (not ("arctis" in d.name.lower() or "steelseries" in d.name.lower()), d.name.lower()))
    return {"headset": ordered, "stream": ordered}


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

class AudioDevicesDialog(QDialog):
    sig_changed = Signal()                                       # the configuration was saved

    def __init__(self, parent=None, config_dir: Path | None = None,
                 probe: Callable[[], list[DeviceInfo]] = subprocess_probe,
                 ensure_engine: Callable[..., bool] = start_engine,
                 client_factory: Callable[..., EngineClient] = EngineClient, bridge=None,
                 synchronous: bool = False, poll_ms: int = 1500,
                 fx_describe: Callable[..., fxstate.FxState] = fxstate.describe,
                 fx_request: Callable[[str], bool] = fxstate.request):
        super().__init__(parent)
        self._config_dir = config_dir
        self._probe, self._ensure = probe, ensure_engine
        self._client_factory = client_factory
        self._bridge = bridge or shared_bridge()
        self._synchronous = synchronous
        self._fx_describe, self._fx_request = fx_describe, fx_request
        self._devices: list[DeviceInfo] = []
        self._tasks: list[_Task] = []
        self._lang = _lang()
        self._config = load_config(config_dir)

        self.setWindowTitle(t("dialog_title", self._lang))
        self.setMinimumWidth(620)
        self.setStyleSheet(f"background-color: {_theme.c('BG_MAIN')}; color: {_theme.c('TEXT_PRIMARY')};")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(10)

        def note(text: str, color: str = "TEXT_SECONDARY", links: bool = False) -> QLabel:
            label = QLabel(text)
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {_theme.c(color)}; font-size: 10pt; background: transparent;")
            if links:
                label.setTextFormat(Qt.TextFormat.RichText)
                label.setOpenExternalLinks(True)
            return label

        root.addWidget(note(t("dialog_intro", self._lang)))
        self._state_lbl = note("", "ACCENT")
        root.addWidget(self._state_lbl)

        form = QFormLayout()
        form.setSpacing(8)
        self._combos: dict[str, NoWheelComboBox] = {}
        self._rows: dict[str, tuple[QLabel, NoWheelComboBox]] = {}
        for key, label_key in (("out:headset", "label_headset"), ("out:stream", "label_stream")):
            combo = NoWheelComboBox()
            combo.setMinimumWidth(360)
            self._combos[key] = combo
            label = QLabel(t(label_key, self._lang))
            label.setStyleSheet("background: transparent;")
            self._rows[key] = (label, combo)
            form.addRow(label, combo)
        self._latency = NoWheelComboBox()
        for name in LATENCY_ORDER:
            ms = int(round(PROFILES[name].approx_latency_ms()))
            self._latency.addItem(t(f"latency_{name}", self._lang, ms=ms), name)
        form.addRow(t("label_latency", self._lang), self._latency)
        self._limiter = QCheckBox(t("label_limiter", self._lang))
        form.addRow("", self._limiter)
        root.addLayout(form)

        self._message = note("", "TEXT_PRIMARY")
        root.addWidget(self._message)
        self._fx_lbl = note("")
        root.addWidget(self._fx_lbl)
        self._fx_btn = QPushButton("")
        self._fx_btn.clicked.connect(self._fx_clicked)
        fx_row = QHBoxLayout()
        fx_row.addWidget(self._fx_btn)
        fx_row.addStretch(1)
        root.addLayout(fx_row)

        buttons = QHBoxLayout()
        self._auto_btn = QPushButton(t("btn_automatic", self._lang))
        self._save_btn = QPushButton(t("btn_save", self._lang))
        self._close_btn = QPushButton(t("btn_close", self._lang))
        buttons.addWidget(self._auto_btn)
        buttons.addStretch(1)
        buttons.addWidget(self._save_btn)
        buttons.addWidget(self._close_btn)
        root.addLayout(buttons)
        self._auto_btn.clicked.connect(self.automatic)
        self._save_btn.clicked.connect(self.save)
        self._close_btn.clicked.connect(self.close)

        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self.refresh_state)
        self._set_busy(True)
        self._message.setText(t("scanning", self._lang))
        self.scan()
        if not synchronous:
            self._timer.start()
        self.refresh_state()

    # ── texts the tests and the page read ───────────────────────────────────

    def message(self) -> str:
        return self._message.text()

    def state_text(self) -> str:
        return self._state_lbl.text()

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
        for b in (self._auto_btn, self._save_btn):
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
            self._message.setText(t("scan_failed", self._lang, error=str(error)))
            return
        self._devices = list(devices)
        fresh = not self._config.outputs.get("headset")
        self.populate(suggest_config(self._devices) if fresh else self._config)
        self._message.setText("")

    def populate(self, cfg: EngineConfig) -> None:
        cand = candidates(self._devices)
        wanted = {"out:headset": (cfg.outputs.get("headset", ""), "headset", False),
                  "out:stream": (cfg.outputs.get("stream", ""), "stream", True)}
        for key, (current, pool, optional) in wanted.items():
            items = combo_items(cand[pool], current, optional, self._lang)
            combo = self._combos[key]
            combo.blockSignals(True)
            combo.clear()
            for label, value in items:
                combo.addItem(label, value)
            combo.setCurrentIndex(pick_index(items, current) if current else 0)
            combo.blockSignals(False)
        # Streamer mode's separate output only means something with a second real output device (or one already chosen)
        others = [d for d in cand["stream"] if d.name != cfg.outputs.get("headset")]
        show_stream = bool(cfg.outputs.get("stream")) or len(others) > 1
        for widget in self._rows["out:stream"]:
            widget.setVisible(show_stream)
        index = self._latency.findData(cfg.latency if cfg.latency in LATENCY_ORDER else "balanced")
        self._latency.setCurrentIndex(max(0, index))
        self._limiter.setChecked(bool(cfg.master_limiter))

    # ── reading the form ────────────────────────────────────────────────────

    def selected_config(self) -> EngineConfig:
        def value(key: str) -> str:
            return str(self._combos[key].currentData() or "")
        if any(self._config.inputs.get(ch) for ch in CHANNELS):
            cfg = copy.deepcopy(self._config)                    # keeps what the form has no control for (blocks, routing, ...)
        else:
            cfg = suggest_config(self._devices)                  # a first run: the channels' own capture devices
        cfg.outputs = {**{b: n for b, n in cfg.outputs.items() if b not in ("headset", "stream")},
                       "headset": value("out:headset")}
        if value("out:stream"):                                  # Streamer mode's second output; no entry when it is not used
            cfg.outputs["stream"] = value("out:stream")
        cfg.latency = str(self._latency.currentData() or "balanced")
        cfg.master_limiter = self._limiter.isChecked()
        cfg.auto = False                                         # the user has chosen: ASM stops choosing
        return cfg

    # ── save / automatic ────────────────────────────────────────────────────

    def save(self) -> None:
        cfg = self.selected_config()
        if not cfg.outputs.get("headset"):
            self._message.setText(t("nothing_to_save", self._lang))
            return
        self._write(cfg, "saved")

    def automatic(self) -> None:
        """Hand the choice back to ASM: its own pick of devices, remade whenever they come and go."""
        if not self._devices:                                    # the scan has not delivered anything: nothing to choose from
            return
        cfg = suggest_config(self._devices)
        for name in ("latency", "master_limiter", "block", "mic_block", "device_latency"):
            setattr(cfg, name, getattr(self._config, name))
        cfg.auto = True
        self.populate(cfg)
        self._write(cfg, "saved_automatic")

    def _write(self, cfg: EngineConfig, done_key: str) -> None:
        save_config(cfg, self._config_dir)
        self._config = cfg
        self.sig_changed.emit()
        self._set_busy(True)
        self._run(self._apply, lambda result, error: self._on_applied(result, error, done_key))

    def _apply(self):
        """Worker: make an engine run and have it rebuild from the file just written."""
        up = self._ensure(self._config_dir, wait=10.0)
        if not up:
            return {"started": False}
        client = self._client_factory(self._config_dir, timeout=20.0)
        reply = client.reconfigure(force=True)
        return {"started": True, "problems": reply.get("problems") or []}

    def _on_applied(self, result, error, done_key: str = "saved") -> None:
        self._set_busy(False)
        if error is not None or not result or not result["started"]:
            self._message.setText(t("start_failed", self._lang))
        elif result["problems"]:
            self._message.setText(t("saved_problems", self._lang, problems="; ".join(result["problems"])))
        else:
            self._message.setText(t(done_key, self._lang))
        self.refresh_state()

    # ── live state ──────────────────────────────────────────────────────────

    def refresh_state(self) -> None:
        status = self._bridge.status()
        cfg_ok = not config_problems(self._config_dir)
        state = engine_state(status, configured=cfg_ok, lang=self._lang)
        lines = endpoint_lines(status)
        self._state_lbl.setText(state.text + ("\n" + "  |  ".join(lines) if lines else ""))
        self._refresh_fx(status)

    # ── the microphone effect ───────────────────────────────────────────────

    def _refresh_fx(self, status: dict | None) -> None:
        try:
            state = self._fx_describe(status)
        except Exception as exc:                                 # noqa: BLE001 — a status line must never raise
            _log.debug("microphone effect state failed: %r", exc)
            state = fxstate.FxState(fxstate.OFF)
        self._fx_state = state
        self._fx_lbl.setText(t(f"fx_{state.code}", self._lang, reason=state.detail))
        if state.code in (fxstate.READY, fxstate.RUNNING):
            self._fx_btn.setText(t("btn_fx_remove", self._lang))
            self._fx_btn.setVisible(True)
        elif state.code in (fxstate.NEEDS_SETUP, fxstate.DISABLED):
            self._fx_btn.setText(t("btn_fx_setup", self._lang))
            self._fx_btn.setVisible(True)
        else:
            self._fx_btn.setVisible(False)

    def fx_text(self) -> str:
        return self._fx_lbl.text()

    def _fx_clicked(self) -> None:
        state = getattr(self, "_fx_state", None)
        remove = state is not None and state.active
        action = "detach" if remove else "attach"
        if self._fx_request(action):
            self._message.setText(t("fx_asked_off" if remove else "fx_asked", self._lang))
        else:
            self._message.setText(t("fx_cannot_ask", self._lang))
