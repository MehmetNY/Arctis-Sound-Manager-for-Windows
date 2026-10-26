# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The engine's process-level shell: configuration, restarts, the control channel.

:class:`EngineHost` owns *at most one* :class:`~.runtime.Engine` at a time and what surrounds it:

* with no usable configuration yet it **idles** (and says why in ``status``) instead of exiting, so the
  GUI's setup assistant can talk to it, write ``win_audio_engine.json`` and send ``reconfigure``;
* ``reconfigure`` rebuilds the engine from the file; volume / mute / ChatMix commands are remembered and
  replayed into every new engine, so a restart never loses what the user set;
* ``devices`` / ``suggest`` give the assistant the device list and a first guess, without the GUI having
  to load PortAudio itself.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from arctis_sound_manager.win.audio import fast
from arctis_sound_manager.win.audio.backend import (BackendError, DeviceInfo, SoundDeviceBackend, device_from_dict,
                                                    device_to_dict)
from arctis_sound_manager.win.audio.engine_config import (EngineConfig, config_path, load_config, resolve, save_config,
                                                          suggest_config)
from arctis_sound_manager.win.audio.graph import CHANNELS, CONTROLS
from arctis_sound_manager.win.audio.params import default_config_dir
from arctis_sound_manager.win.audio.runtime import Engine
from arctis_sound_manager.win.audio.tap import (TapBackend, list_endpoints, load_rules, restore_volumes, save_rules,
                                                 shared_helper)

_log = logging.getLogger(__name__)

VERSION = "win-engine 1"
AUTOSETUP_RETRY = 6.0                                               # seconds between looks for devices while there is no engine
SETTLE_SECONDS = 0.4                                                # after an engine ended: the devices are still being released
START_DELAYS = (0.6, 1.5)                                           # waits between the tries of one (re)configuration
RESTART_BACKOFF = (3.0, 6.0, 12.0, 30.0)                            # then the host itself keeps trying, ever less often


def default_backend(config: EngineConfig):
    """PortAudio, plus the per-application tap when ``asm-audiotap.exe`` is installed."""
    real = SoundDeviceBackend(latency=config.device_latency)
    helper = shared_helper()
    return TapBackend(real, helper) if helper.available() else real


def subprocess_probe(timeout: float = 15.0) -> list[DeviceInfo]:
    """A fresh listing of the devices, taken in a new process (this one's PortAudio only knows the devices
    that existed when it started)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--engine", "--list-devices", "--json"]
    else:
        cmd = [sys.executable, "-m", "arctis_sound_manager.win.audio.engine_main", "--list-devices", "--json"]
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"exit code {result.returncode}")
    return [device_from_dict(d) for d in json.loads(result.stdout)]


def helper_probe() -> list[DeviceInfo] | None:
    """The same question asked of the native helper: a few milliseconds instead of a new Python process.
    ``None`` when the helper cannot answer (the caller then uses :func:`subprocess_probe`)."""
    return list_endpoints()


_DEFAULT = object()


class EngineHost:
    def __init__(self, config_dir: Path | None = None,
                 backend_factory: Callable[[EngineConfig], object] = default_backend,
                 probe: Callable[[], list[DeviceInfo]] | None = subprocess_probe,
                 native=None, hrir_id: str | None = None, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic, quick_probe=_DEFAULT):
        self.config_dir = config_dir or default_config_dir()
        self._sleep = sleep
        self._monotonic = monotonic
        self._failures = 0                                           # starts in a row that failed (drives the back-off)
        self._retry_at = 0.0
        self._backend_factory = backend_factory
        self._probe = probe
        # with no probe at all (tests, or a caller that wants none) there is no quick one either
        self._quick_probe = (helper_probe if probe is not None else None) if quick_probe is _DEFAULT else quick_probe
        self._native = native
        self._hrir_id = hrir_id
        self.engine: Engine | None = None
        self.config = EngineConfig()
        self.error = ""
        self.started = time.time()
        self.shutdown_requested = threading.Event()
        self._lock = threading.RLock()
        # what the daemon / GUI set, replayed into every new engine
        self._volumes: dict[str, float] = {}
        self._muted: dict[str, bool] = {}
        self._mix: tuple[float, float] | None = None
        self._mix_channels: list[str] | None = None

    # ── life cycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if shared_helper().available():
            restore_volumes()                                        # a crashed run may have left programs turned down
        self.autoconfigure()
        self.reconfigure()

    def autoconfigure(self) -> bool:
        """Pick the devices by ourselves: on a first run (no configuration file) and, for a configuration we made
        ourselves (``auto``), whenever it no longer works - the headset was switched on later, a cable was
        installed. A configuration the user saved is never touched. Returns whether a file was (re)written."""
        with self._lock:
            path = config_path(self.config_dir)
            current = load_config(self.config_dir)
            # a file written by a build from before the per-application tap (cables per channel, set up by hand) is
            # worth nothing to this one: redo it, keeping the old one beside it
            legacy = path.exists() and current.schema < EngineConfig().schema
            if path.exists() and not current.auto and not legacy:
                return False
            if path.exists() and not legacy and not current.problems() and not self._missing(current):
                return False                                          # still fine as it is
            try:
                backend = self._backend_factory(current)
                devices = backend.devices()
            except BackendError as exc:
                _log.info("auto-setup: no device list yet (%s)", exc)
                return False
            guess = suggest_config(devices)
            if guess.problems():
                _log.info("auto-setup: not enough devices yet (%s)", "; ".join(guess.problems()))
                return False
            guess.auto = True
            if path.exists() and guess.to_dict() == current.to_dict():
                return False
            if legacy:
                try:
                    path.replace(path.with_name(path.name + ".old-build"))
                    _log.info("auto-setup: the configuration of an older build is kept as %s.old-build", path.name)
                except OSError:
                    pass
            save_config(guess, self.config_dir)
            _log.info("auto-setup: configuration written (headset %r, microphone %r -> %r)", guess.outputs.get("headset"),
                      guess.mic_input, guess.mic_output)
            return True

    def _missing(self, cfg: EngineConfig) -> list[str]:
        try:
            return resolve(cfg, self._backend_factory(cfg).devices()).missing
        except BackendError:
            return []

    def stop(self) -> None:
        with self._lock:
            self._stop_engine()

    def _stop_engine(self) -> None:
        engine, self.engine = self.engine, None
        if engine is not None:
            try:
                engine.stop()
            except Exception:                                        # noqa: BLE001
                _log.exception("stopping the engine failed")

    def reconfigure(self, force: bool = False) -> list[str]:
        """Re-read ``win_audio_engine.json`` and rebuild the engine. Returns what is wrong with the configuration.

        Asking for what is already running changes nothing (the daemon and the GUI send this on every setting they
        touch, and tearing the sound system down and up again for nothing is how a device ends up "in use");
        ``force`` rebuilds anyway. A start that fails is tried again a little later, with everything the failed
        try opened closed first; if it keeps failing, :meth:`wait` goes on trying (see ``RESTART_BACKOFF``)."""
        with self._lock:
            wanted = load_config(self.config_dir)
            engine = self.engine
            if (not force and engine is not None and engine.running and not self.error
                    and wanted.to_dict() == self.config.to_dict()):
                _log.debug("the engine already runs with this configuration")
                return []
            had_engine = engine is not None
            self._stop_engine()
            self.config = wanted
            problems = self.config.problems()
            self.error = ""
            if problems:
                _log.info("not starting the engine yet: %s", "; ".join(problems))
                return problems
            if had_engine:
                self._sleep(SETTLE_SECONDS)                          # Windows needs a moment to let go of the devices just closed
            for attempt in range(len(START_DELAYS) + 1):
                try:
                    backend = self._backend_factory(self.config)
                    engine = Engine(self.config, backend, config_dir=self.config_dir, native=self._native,
                                    hrir_id=self._hrir_id, probe=self._probe, quick_probe=self._quick_probe)
                    engine.preset = {"volumes": dict(self._volumes), "muted": dict(self._muted), "mix": self._mix,
                                     "mix_channels": self._mix_channels}
                    engine.start(threaded=True)
                except BackendError as exc:
                    self.error = str(exc)
                    _log.error("%s", exc)
                except Exception as exc:                              # noqa: BLE001 — never take the host down
                    self.error = f"{type(exc).__name__}: {exc}"
                    if attempt < len(START_DELAYS):
                        _log.warning("the engine could not start (%s); trying again", self.error)
                    else:
                        _log.exception("the engine could not start")
                else:
                    self.engine = engine
                    self.error = ""
                    self._failures = 0
                    return []
                if attempt < len(START_DELAYS):
                    self._sleep(START_DELAYS[attempt])
            self._failures += 1
            self._retry_at = self._monotonic() + RESTART_BACKOFF[min(self._failures, len(RESTART_BACKOFF)) - 1]
            return [self.error]

    # ── the control channel ─────────────────────────────────────────────────

    def devices(self) -> list[DeviceInfo]:
        engine = self.engine
        if engine is not None:
            return list(engine._devices)
        backend = self._backend_factory(self.config)
        return backend.devices()

    def status(self) -> dict:
        engine = self.engine
        base = {"version": VERSION, "uptime": round(time.time() - self.started, 1), "config": self.config.to_dict()}
        if engine is None:
            problems = self.config.problems()
            notes = ["waiting for a configuration"] if problems else []
            if fast.is_warming():
                notes.append("compiling the audio kernels (first start on this computer only)")
            return {**base, "running": False, "problems": problems, "error": self.error, "notes": notes}
        return {**base, **engine.status(), "error": self.error}

    def handle(self, request: dict) -> dict:
        cmd = request.get("cmd")
        # these never wait for a restart in progress (which can take seconds the first time): the GUI polls them
        if cmd == "ping":
            return {"pong": True, "version": VERSION}
        if cmd == "status":
            return {"status": self.status()}
        if cmd == "shutdown":
            self.shutdown_requested.set()
            return {}
        with self._lock:
            if cmd == "reconfigure":
                problems = self.reconfigure(force=bool(request.get("force")))
                return {"problems": problems, "ok": not problems}
            if cmd == "devices":
                try:
                    listing = self.devices()
                except BackendError as exc:
                    return {"ok": False, "error": str(exc)}
                resolved = resolve(self.config, listing)
                return {"devices": [device_to_dict(d) for d in listing], "missing": resolved.missing}
            if cmd == "suggest":
                try:
                    listing = self.devices()
                except BackendError as exc:
                    return {"ok": False, "error": str(exc)}
                return {"config": suggest_config(listing).to_dict()}
            if cmd == "volume":
                channel = str(request["channel"])
                if channel not in CONTROLS:
                    raise ValueError(f"unknown channel {channel!r}")
                self._volumes[channel] = float(max(0.0, min(100.0, float(request["pct"]))))
            elif cmd == "mute":
                channel = str(request["channel"])
                if channel not in CONTROLS:
                    raise ValueError(f"unknown channel {channel!r}")
                self._muted[channel] = bool(request["muted"])
            elif cmd == "chatmix":
                self._mix = (float(max(0.0, min(100.0, float(request["channels_pct"])))),
                             float(max(0.0, min(100.0, float(request["chat_pct"])))))
                channels = request.get("channels")
                if isinstance(channels, list):
                    self._mix_channels = [c for c in channels if c in CHANNELS and c != "chat"]
            elif cmd in ("route", "unroute"):
                if self.engine is None:                              # remembered for when the engine starts
                    return self._route_offline(request)
            elif cmd != "reload":
                return {"ok": False, "error": f"unknown command {cmd!r}"}
            if self.engine is not None:
                return self.engine.handle(request)
            return {"engine": "not running"}

    def _route_offline(self, request: dict) -> dict:
        rules = load_rules(self.config_dir)
        exe, channel = str(request.get("exe", "")), request.get("channel") if request.get("cmd") == "route" else None
        ok = rules.set(exe, str(channel)) if channel is not None else rules.clear(exe) or True
        if not ok:
            return {"ok": False, "error": f"cannot route {exe!r} to {channel!r}"}
        save_rules(rules, self.config_dir)
        return {"engine": "not running"}

    def wait(self, poll: float = 0.5) -> None:
        """Block until a ``shutdown`` command (or Ctrl+C in the caller); also starts the engine once a
        configuration file appears while idle."""
        last = self._config_stamp()
        retry_at = time.monotonic() + AUTOSETUP_RETRY
        while not self.shutdown_requested.wait(poll):
            if time.monotonic() >= retry_at:                          # a headset switched on later, a cable installed meanwhile
                retry_at = time.monotonic() + AUTOSETUP_RETRY
                if self.engine is None and load_config(self.config_dir).auto or not config_path(self.config_dir).exists():
                    self.autoconfigure()
            stamp = self._config_stamp()
            if self.engine is None and stamp != last:
                _log.info("configuration changed; trying to start")
                self.reconfigure()
            elif self.engine is None and self.error and self._monotonic() >= self._retry_at:
                _log.info("the engine is not running (%s); trying to start it again", self.error)
                self.reconfigure(force=True)                          # a failure is never left standing: start it again
            last = stamp

    def _config_stamp(self):
        from arctis_sound_manager.win.audio.engine_config import config_path
        try:
            st = config_path(self.config_dir).stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None
