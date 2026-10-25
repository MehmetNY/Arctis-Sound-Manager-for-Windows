# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The per-application audio tap: Windows' stand-in for ASM's PipeWire channel sinks.

On Linux every program is sent to one of four virtual sinks (Game / Chat / Media / Aux) and ASM's filter
chains process each sink. Windows has nothing like it between a program and the speaker. This module
talks to ``asm-audiotap.exe`` (``native/audiotap``), which

* takes a copy of each program's sound (WASAPI *process loopback*), after the program's own volume,
* turns the program down to ~-160 dB at the headset (its session volume, remembered and put back later)
  and multiplies the copy by what that attenuation removed — the copy is the sound the program meant
  to play, nothing else reaches the ear, and
* mixes the copies per channel by program name and streams four stereo pairs to us.

To the rest of the engine the four pairs look like four capture devices named ``ASM Tap game`` … ``ASM Tap
aux`` (:class:`TapBackend` adds them next to the real devices), so the ports, the drift control and the
whole processing graph work exactly as they do with virtual cables. No driver is involved.

Wire format of the helper's stdout, repeated: ``b"ATP1"``, ``u32 frames``, ``u32 channels``, ``u32 0``, then
``frames * channels`` little-endian ``float32`` (channels: game L R, chat L R, media L R, aux L R).
stdin takes one tab-separated command per line; stderr carries one JSON object per line.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from arctis_sound_manager.win.audio.backend import BackendError, DeviceInfo, OpenedStream

_log = logging.getLogger(__name__)

RATE = 48000
TAP_API = "Windows WASAPI (ASM Tap)"          # contains "wasapi": the device lists treat it like a native device
TAP_CHANNELS = ("game", "chat", "media", "aux")
TAP_PREFIX = "ASM Tap "
BYPASS = "bypass"
HELPER_NAME = "asm-audiotap.exe"
ROUTING_FILE = "win_app_routing.json"
FRAME_MAGIC = b"ATP1"
HEADER = struct.Struct("<4sIII")
MAX_FRAMES = 48000
STATUS_MAX_AGE = 3.0                          # a status older than this is not shown (the helper has stopped talking)
STOP_GRACE = 6.0                              # a helper nobody uses stays alive this long (a device rebuild takes seconds)
TAP_BASE_INDEX = 10_000                       # PortAudio indexes are small; ours never collide


def tap_device_name(channel: str) -> str:
    return TAP_PREFIX + channel


def is_tap_name(name: str) -> bool:
    return (name or "").startswith(TAP_PREFIX) and name[len(TAP_PREFIX):] in TAP_CHANNELS


def tap_devices() -> list[DeviceInfo]:
    """The four virtual capture devices the tap offers."""
    return [DeviceInfo(index=TAP_BASE_INDEX + i, name=tap_device_name(ch), hostapi=TAP_API, max_inputs=2,
                       max_outputs=0, default_rate=float(RATE)) for i, ch in enumerate(TAP_CHANNELS)]


def is_tap_device(device: DeviceInfo) -> bool:
    return device.hostapi == TAP_API


# ── which program goes to which channel ──────────────────────────────────────

# Voice-chat programs go to Chat, players and browsers to Media, everything else (games) to Game.
# Aux has no default occupant. Names are lower-case executable names.
CHAT_APPS = frozenset({
    "discord.exe", "discordptb.exe", "discordcanary.exe", "vesktop.exe", "legcord.exe", "teams.exe", "ms-teams.exe",
    "msteams.exe", "zoom.exe", "skype.exe", "lync.exe", "teamspeak3.exe", "ts3client_win64.exe", "teamspeak.exe",
    "mumble.exe", "slack.exe", "telegram.exe", "whatsapp.exe", "signal.exe", "element.exe", "webex.exe",
    "ventrilo.exe", "guilded.exe", "revolt.exe", "ringcentral.exe", "googlemeet.exe", "line.exe",
})
MEDIA_APPS = frozenset({
    "spotify.exe", "vlc.exe", "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "opera_gx.exe",
    "vivaldi.exe", "arc.exe", "zen.exe", "librewolf.exe", "waterfox.exe", "iexplore.exe", "wmplayer.exe",
    "musicbee.exe", "foobar2000.exe", "itunes.exe", "applemusic.exe", "tidal.exe", "deezer.exe", "amazonmusic.exe",
    "potplayermini64.exe", "potplayermini.exe", "mpc-hc64.exe", "mpc-hc.exe", "mpc-be64.exe", "mpv.exe", "winamp.exe",
    "groove.exe", "music.ui.exe", "video.ui.exe", "plex.exe", "plexamp.exe", "jellyfin.exe", "kodi.exe",
    "netflix.exe", "disneyplus.exe", "primevideo.exe", "twitch.exe", "youtubemusic.exe", "ytmdesktop.exe",
    "pear-desktop.exe", "audacious.exe", "aimp.exe", "clementine.exe", "strawberry.exe", "reaper.exe",
})
DEFAULT_CHANNEL = "game"
CHOICES = TAP_CHANNELS + (BYPASS,)


def exe_key(name: str) -> str:
    """``C:\\Apps\\Spotify.EXE`` / ``Spotify`` -> ``spotify.exe``."""
    base = os.path.basename(str(name or "").strip().replace("\\", "/")).lower()
    if not base:
        return ""
    return base if "." in base else base + ".exe"


def friendly_app_name(exe: str) -> str:
    """``msedge.exe`` -> ``Msedge``; ``Rocket_League.exe`` -> ``Rocket League``."""
    stem = os.path.splitext(os.path.basename(exe or ""))[0]
    if not stem:
        return "?"
    pretty = stem.replace("_", " ").replace("-", " ").strip()
    return pretty[:1].upper() + pretty[1:] if pretty == pretty.lower() else pretty


class AppRules:
    """Program name -> channel: the built-in defaults plus what the user dragged somewhere."""

    def __init__(self, default: str = DEFAULT_CHANNEL, user: dict[str, str] | None = None):
        self.default = default if default in TAP_CHANNELS else DEFAULT_CHANNEL
        self.user: dict[str, str] = {}
        for exe, channel in (user or {}).items():
            self.set(exe, channel)

    def channel_for(self, exe: str) -> str:
        key = exe_key(exe)
        if key in self.user:
            return self.user[key]
        if key in CHAT_APPS:
            return "chat"
        if key in MEDIA_APPS:
            return "media"
        return self.default

    def set(self, exe: str, channel: str) -> bool:
        key = exe_key(exe)
        if not key or channel not in CHOICES:
            return False
        self.user[key] = channel
        return True

    def clear(self, exe: str) -> bool:
        return self.user.pop(exe_key(exe), None) is not None

    def commands(self) -> list[str]:
        """The helper's ``default`` / ``rule`` lines (defaults first, so a user rule of the same name wins)."""
        lines = [f"default\t{self.default}"]
        merged = {**{e: "chat" for e in CHAT_APPS}, **{e: "media" for e in MEDIA_APPS}, **self.user}
        lines += [f"rule\t{exe}\t{channel}" for exe, channel in sorted(merged.items())]
        return lines

    def to_dict(self) -> dict:
        return {"default": self.default, "rules": dict(sorted(self.user.items()))}

    @classmethod
    def from_dict(cls, raw: object) -> "AppRules":
        if not isinstance(raw, dict):
            return cls()
        rules = raw.get("rules")
        return cls(str(raw.get("default") or DEFAULT_CHANNEL),
                   {str(k): str(v) for k, v in rules.items()} if isinstance(rules, dict) else None)


def routing_path(config_dir: Path | None = None) -> Path:
    from arctis_sound_manager.win.audio.params import default_config_dir
    return (config_dir or default_config_dir()) / ROUTING_FILE


def load_rules(config_dir: Path | None = None) -> AppRules:
    try:
        return AppRules.from_dict(json.loads(routing_path(config_dir).read_text(encoding="utf-8")))
    except FileNotFoundError:
        return AppRules()
    except (OSError, ValueError) as exc:
        _log.warning("cannot read %s: %s; using the defaults", routing_path(config_dir), exc)
        return AppRules()


def save_rules(rules: AppRules, config_dir: Path | None = None) -> Path:
    path = routing_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rules.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ── the helper program ───────────────────────────────────────────────────────

def find_helper() -> str | None:
    """Where ``asm-audiotap.exe`` is: ``ASM_AUDIOTAP``, next to the program (installed build), in the package's
    ``win/bin`` (a wheel), or in ASM's data folder."""
    candidates: list[Path] = []
    env = os.environ.get("ASM_AUDIOTAP")
    if env:
        candidates.append(Path(env))
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        candidates += [base / HELPER_NAME, base / "bin" / HELPER_NAME]
    candidates.append(Path(__file__).resolve().parent.parent / "bin" / HELPER_NAME)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "arctis_manager" / "bin" / HELPER_NAME)
    for path in candidates:
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return None


def state_file() -> Path:
    from arctis_sound_manager.file_lock import runtime_dir
    return runtime_dir() / "audiotap_state.txt"


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def restore_volumes(exe: str | None = None, run=subprocess.run, timeout: float = 10.0, state: str | os.PathLike | None = None) -> bool:
    """Put back the volume of every program a previous (crashed) helper had turned down. Harmless when there is
    nothing to put back. ``True`` when the helper ran."""
    exe = exe or find_helper()
    if not exe:
        return False
    kwargs: dict = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = _no_window()
    try:
        run([exe, "--restore", "--state", str(state or state_file())], **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("restoring program volumes failed: %s", exc)
        return False
    return True


class FrameDecoder:
    """Cuts the helper's byte stream into ``(frames, channels)`` float32 blocks; resynchronises after garbage."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.skipped = 0                       # bytes thrown away while looking for the next header

    def feed(self, data: bytes) -> list[np.ndarray]:
        self._buf += data
        out: list[np.ndarray] = []
        buf = self._buf
        while len(buf) >= HEADER.size:
            if bytes(buf[:4]) != FRAME_MAGIC:
                at = buf.find(FRAME_MAGIC, 1)
                drop = at if at > 0 else max(1, len(buf) - 3)
                self.skipped += drop
                del buf[:drop]
                continue
            _, frames, channels, _ = HEADER.unpack_from(buf)
            if not 0 < frames <= MAX_FRAMES or not 0 < channels <= 64:
                self.skipped += 4
                del buf[:4]
                continue
            need = HEADER.size + frames * channels * 4
            if len(buf) < need:
                break
            out.append(np.frombuffer(bytes(buf[HEADER.size:need]), dtype="<f4").reshape(frames, channels).copy())
            del buf[:need]
        return out


class TapHelper:
    """One ``asm-audiotap.exe`` process, shared by everything in this process that wants the tap."""

    def __init__(self, exe: str | None = None, popen=subprocess.Popen, clock: Callable[[], float] = time.monotonic,
                 state: Path | None = None, exists: Callable[[str], bool] = os.path.isfile):
        self.exe = exe if exe is not None else find_helper()
        self._popen = popen
        self._exists = exists
        self._clock = clock
        self._state = state
        self._lock = threading.RLock()
        self._proc = None
        self._sinks: dict[str, Callable[[np.ndarray], None]] = {}
        self._refs = 0
        self._stop_timer: threading.Timer | None = None
        self._endpoint = ""
        self._rules = AppRules()
        self._exclude: set[int] = {os.getpid()}
        self._engaged = False
        self._status: dict = {}
        self._status_at = -1e9
        self.packets = 0
        self.restarts = 0
        self.skipped = 0
        self.last_error = ""
        self.stderr_log: list[str] = []

    # ── availability ────────────────────────────────────────────────────────

    def available(self) -> bool:
        return bool(self.exe) and self._exists(self.exe)

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    # ── configuration (remembered, replayed into every new process) ─────────

    def configure(self, endpoint: str | None = None, rules: AppRules | None = None, exclude: set[int] | None = None,
                  engaged: bool | None = None) -> None:
        with self._lock:
            if engaged is not None:
                self._engaged = bool(engaged)
            if endpoint is not None:
                self._endpoint = endpoint
            if rules is not None:
                self._rules = rules
            if exclude is not None:
                self._exclude = set(exclude) | {os.getpid()}
            self._send_config()

    def engage(self, on: bool) -> None:
        with self._lock:
            self._engaged = bool(on)
            self._send(f"engage\t{'1' if on else '0'}", "apply")

    @property
    def engaged(self) -> bool:
        return self._engaged

    def set_rule(self, exe: str, channel: str | None) -> bool:
        """Route one program (``channel`` ``None`` = back to the default). Returns ``False`` for nonsense."""
        with self._lock:
            if not exe_key(exe):
                return False
            if channel is None:
                self._rules.clear(exe)
            elif not self._rules.set(exe, channel):
                return False
            self._send_config()
            return True

    @property
    def rules(self) -> AppRules:
        return self._rules

    def _config_lines(self) -> list[str]:
        lines = ["clear", f"endpoint\t{self._endpoint or 'default'}"]
        lines += self._rules.commands()
        lines += [f"exclude\t{pid}" for pid in sorted(self._exclude)]
        lines += [f"engage\t{'1' if self._engaged else '0'}", "apply"]
        return lines

    def _send_config(self) -> None:
        self._send(*self._config_lines())

    def _send(self, *lines: str) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return
        try:
            proc.stdin.write(("\n".join(lines) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except (OSError, ValueError):
            pass                                                      # the process is going away; ensure_running() notices

    # ── the process ─────────────────────────────────────────────────────────

    def ensure_running(self) -> bool:
        """Start the helper if it is not running. ``False`` (and ``last_error``) when it cannot be started."""
        with self._lock:
            if self.alive:
                return True
            if not self.exe:
                self.last_error = f"{HELPER_NAME} was not found"
                return False
            if self._proc is not None:
                self.restarts += 1
                self._proc = None
            state = self._state or state_file()
            try:
                state.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            kwargs: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "bufsize": 0}
            if os.name == "nt":
                kwargs["creationflags"] = _no_window()
            try:
                proc = self._popen([self.exe, "--state", str(state)], **kwargs)
            except OSError as exc:
                self.last_error = f"{HELPER_NAME} could not be started: {exc}"
                _log.warning("%s", self.last_error)
                return False
            self._proc = proc
            self.last_error = ""
            threading.Thread(target=self._pump_frames, args=(proc,), name="asm-tap-frames", daemon=True).start()
            threading.Thread(target=self._pump_status, args=(proc,), name="asm-tap-status", daemon=True).start()
            self._send_config()
            _log.info("%s started (pid %s)", HELPER_NAME, getattr(proc, "pid", "?"))
            return True

    def _pump_frames(self, proc) -> None:
        decoder = FrameDecoder()
        stream = proc.stdout
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                for block in decoder.feed(chunk):
                    self.packets += 1
                    self._dispatch(block)
                self.skipped = decoder.skipped
        except (OSError, ValueError):
            return

    def _dispatch(self, block: np.ndarray) -> None:
        for i, channel in enumerate(TAP_CHANNELS):
            sink = self._sinks.get(channel)
            if sink is None or block.shape[1] < 2 * i + 2:
                continue
            try:
                sink(np.ascontiguousarray(block[:, 2 * i:2 * i + 2]))
            except Exception:                                          # noqa: BLE001 — one bad consumer must not stop the others
                _log.exception("tap sink %s failed", channel)

    def _pump_status(self, proc) -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("t") == "status":
                    self._status, self._status_at = msg, self._clock()
                elif msg.get("t") == "log":
                    text = str(msg.get("msg", ""))
                    self.stderr_log = (self.stderr_log + [text])[-50:]
                    _log.info("audiotap: %s", text)
        except (OSError, ValueError):
            return

    def stop(self, timeout: float = 3.0) -> None:
        """Ask the helper to quit (it puts every program's volume back first) and wait for it."""
        with self._lock:
            timer, self._stop_timer = self._stop_timer, None
            if timer is not None:
                timer.cancel()
            proc, self._proc = self._proc, None
            self._status, self._engaged = {}, False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write(b"engage\t0\nquit\n")
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        except Exception:                                              # noqa: BLE001 — shutting down: nothing to do about it
            _log.debug("stopping the helper failed", exc_info=True)

    # ── users of the tap ────────────────────────────────────────────────────

    def acquire(self) -> bool:
        with self._lock:
            if self._stop_timer is not None:
                self._stop_timer.cancel()
                self._stop_timer = None
            if not self.ensure_running():
                return False
            self._refs += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._refs = max(0, self._refs - 1)
            if self._refs == 0 and self.alive and self._stop_timer is None:
                timer = threading.Timer(STOP_GRACE, self._stop_if_unused)
                timer.daemon = True
                self._stop_timer = timer
                timer.start()

    def _stop_if_unused(self) -> None:
        with self._lock:
            self._stop_timer = None
            unused = self._refs == 0
        if unused:
            self.stop()

    def attach(self, channel: str, sink: Callable[[np.ndarray], None]) -> None:
        self._sinks[channel] = sink

    def detach(self, channel: str) -> None:
        self._sinks.pop(channel, None)

    # ── what is playing ─────────────────────────────────────────────────────

    def status(self) -> dict:
        return dict(self._status) if self._clock() - self._status_at <= STATUS_MAX_AGE else {}

    def apps(self) -> list[dict]:
        """Programs the helper sees on the headset: ``exe, pid, channel, managed, active, level, leaks``."""
        apps = self.status().get("apps")
        return [a for a in apps if isinstance(a, dict)] if isinstance(apps, list) else []

    def info(self) -> dict:
        status = self.status()
        return {"available": self.available(), "running": self.alive, "engaged": bool(status.get("engaged", False)),
                "endpoint": status.get("endpoint", ""), "packets": self.packets, "restarts": self.restarts,
                "leaks": sum(int(a.get("leaks", 0) or 0) for a in self.apps()), "error": self.last_error,
                "apps": self.apps()}


_shared: TapHelper | None = None
_shared_lock = threading.Lock()


def shared_helper() -> TapHelper:
    """The one helper of this process (so a rebuilt engine reuses it and no program's volume flaps)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = TapHelper()
            atexit.register(_shared.stop)
        return _shared


# ── the engine's view: four more capture devices ─────────────────────────────

class TapStream(OpenedStream):
    """One channel of the tap, opened like a capture device."""

    def __init__(self, device: DeviceInfo, helper: TapHelper, channel: str, callback: Callable[[np.ndarray], None]):
        super().__init__(device, "input", 2, None, note="per-application tap")
        self._helper = helper
        self._channel = channel
        self._callback = callback
        self._open = True

    def start(self) -> None:
        if self._open:
            self._helper.ensure_running()
            self._helper.attach(self._channel, self._callback)

    def stop(self) -> None:
        self._helper.detach(self._channel)

    def close(self) -> None:
        if self._open:
            self._open = False
            self._helper.detach(self._channel)
            self._helper.release()

    @property
    def active(self) -> bool:
        return self._open and self._helper.alive


class TapBackend:
    """The real backend plus the tap's four devices. Everything else is passed through."""

    name = "sounddevice+tap"

    def __init__(self, real, helper: TapHelper):
        self.real = real
        self.tap = helper

    def __getattr__(self, name: str):                                    # latency, block, ... of the real backend
        return getattr(self.real, name)

    def devices(self) -> list[DeviceInfo]:
        listing = list(self.real.devices())
        return listing + (tap_devices() if self.tap.available() else [])

    def refresh(self) -> None:
        self.real.refresh()

    def open_input(self, device: DeviceInfo, channels: int, callback, status=None):
        if not is_tap_device(device):
            return self.real.open_input(device, channels, callback, status)
        channel = device.name[len(TAP_PREFIX):]
        if not self.tap.acquire():
            raise BackendError(self.tap.last_error or "the per-application audio tap is not available")
        return TapStream(device, self.tap, channel, callback)

    def open_output(self, device: DeviceInfo, channels: int, callback, status=None):
        return self.real.open_output(device, channels, callback, status)
