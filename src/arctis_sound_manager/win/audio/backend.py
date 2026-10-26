# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sound devices for the engine: PortAudio through ``sounddevice``, on WASAPI.

The engine only needs four things from a sound system: the list of devices, a way to open a capture
stream, a way to open a render stream, and a callback per stream. This module keeps that small
surface (so the timing logic in :mod:`.ports` and :mod:`.runtime` can be driven by a fake in tests)
and puts everything PortAudio-specific in one place:

* devices are looked up by a piece of their name, and the **WASAPI** copy of a device is preferred over
  the MME / DirectSound / WDM-KS ones PortAudio also lists (full names, shared mode, low latency);
* a stream is opened *shared mode* with Windows' own sample-rate/format conversion switched on, so a
  device that is set to 44.1 kHz still works at the engine's 48 kHz; when a device refuses the channel
  count (an 8-channel request on a stereo cable) the next best count is tried and the caller is told
  how many channels it really got;
* callbacks never raise into PortAudio (an exception there would silently stop the stream).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import numpy as np

_log = logging.getLogger(__name__)

RATE = 48000


class BackendError(RuntimeError):
    """The sound system is unavailable or a device could not be opened."""


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    hostapi: str
    max_inputs: int
    max_outputs: int
    default_rate: float = 48000.0
    default_input: bool = False
    default_output: bool = False

    @property
    def wasapi(self) -> bool:
        return "wasapi" in self.hostapi.lower()

    def channels(self, kind: str) -> int:
        return self.max_inputs if kind == "input" else self.max_outputs

    def label(self) -> str:
        return f"[{self.index}] {self.name} ({self.hostapi}, in {self.max_inputs} / out {self.max_outputs})"


def device_to_dict(d: DeviceInfo) -> dict:
    return {"index": d.index, "name": d.name, "hostapi": d.hostapi, "inputs": d.max_inputs, "outputs": d.max_outputs,
            "rate": d.default_rate, "default_input": d.default_input, "default_output": d.default_output}


def device_from_dict(raw: dict) -> DeviceInfo:
    return DeviceInfo(int(raw["index"]), str(raw["name"]), str(raw.get("hostapi", "?")), int(raw.get("inputs", 0)),
                      int(raw.get("outputs", 0)), float(raw.get("rate", RATE)), bool(raw.get("default_input", False)),
                      bool(raw.get("default_output", False)))


# ── finding devices ──────────────────────────────────────────────────────────

def find_device(devices: list[DeviceInfo], query: str | None, kind: str) -> DeviceInfo | None:
    """The device a config entry points at.

    ``query`` is a piece of the device name (case-insensitive), ``"index:N"`` for an exact PortAudio
    index, or ``"default"`` for the system default. Among several matches the WASAPI one wins, then
    an exact name, then the shortest name (``"CABLE Output"`` before ``"CABLE Output 2"``). ``None``
    when nothing matches, or the query is empty.
    """
    query = (query or "").strip()
    if not query:
        return None
    usable = [d for d in devices if d.channels(kind) > 0]
    if query.lower() == "default":
        defaults = [d for d in usable if (d.default_input if kind == "input" else d.default_output)]
        pool = [d for d in defaults if d.wasapi] or defaults
        return pool[0] if pool else None
    if query.lower().startswith("index:"):
        try:
            wanted = int(query.split(":", 1)[1])
        except ValueError:
            return None
        return next((d for d in usable if d.index == wanted), None)
    needle = query.lower()
    matches = [d for d in usable if needle in d.name.lower()]
    if not matches:
        return None
    matches.sort(key=lambda d: (not d.wasapi, d.name.lower() != needle, len(d.name), d.index))
    return matches[0]


# ── open streams ─────────────────────────────────────────────────────────────

class OpenedStream:
    """A running (or startable) capture / render stream and how it was opened."""

    def __init__(self, device: DeviceInfo, kind: str, channels: int, stream=None, note: str = ""):
        self.device = device
        self.kind = kind
        self.channels = channels
        self.note = note
        self._stream = stream

    def start(self) -> None:
        if self._stream is not None:
            self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:                          # noqa: BLE001 — a device that vanished
                _log.debug("stopping %s failed: %r", self.device.name, exc)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception as exc:                          # noqa: BLE001
                _log.debug("closing %s failed: %r", self.device.name, exc)
            self._stream = None

    @property
    def active(self) -> bool:
        return bool(self._stream is not None and getattr(self._stream, "active", True))


class SoundDeviceBackend:
    """PortAudio via the ``sounddevice`` package."""

    name = "sounddevice"

    def __init__(self, sd=None, latency: str | float = "low", block: int = 0):
        if sd is None:
            try:
                import sounddevice                             # noqa: PLC0415 — optional dependency
            except (ImportError, OSError) as exc:
                raise BackendError(f"sounddevice / PortAudio is not available: {exc}. "
                                   f"Install it with:  py -m pip install sounddevice") from exc
            sd = sounddevice
        self._sd = sd
        self.latency = latency
        self.block = block

    # devices
    def devices(self) -> list[DeviceInfo]:
        sd = self._sd
        try:
            hostapis = sd.query_hostapis()
            raw = sd.query_devices()
        except Exception as exc:                               # noqa: BLE001
            raise BackendError(f"cannot list sound devices: {exc}") from exc
        out: list[DeviceInfo] = []
        for i, d in enumerate(raw):
            api = hostapis[d.get("hostapi", 0)] if hostapis else {}
            index = int(d.get("index", i))
            # every host API has its own default device; find_device("default") then picks the WASAPI one
            out.append(DeviceInfo(index=index, name=str(d["name"]), hostapi=str(api.get("name", "?")),
                                  max_inputs=int(d["max_input_channels"]), max_outputs=int(d["max_output_channels"]),
                                  default_rate=float(d.get("default_samplerate", RATE)),
                                  default_input=(index == api.get("default_input_device", -1)),
                                  default_output=(index == api.get("default_output_device", -1))))
        return out

    def refresh(self) -> None:
        """Make PortAudio look at the devices again. PortAudio lists what existed when it started, so a
        headset switched on later (or a cable installed since) only shows up after a re-initialisation.
        Every stream must be closed first."""
        sd = self._sd
        try:
            sd._terminate()
            sd._initialize()
        except Exception as exc:                               # noqa: BLE001 — private API; may change
            raise BackendError(f"cannot re-read the device list: {exc}") from exc

    # streams
    def open_input(self, device: DeviceInfo, channels: int, callback: Callable[[np.ndarray], None],
                   status: Callable[[str], None] | None = None) -> OpenedStream:
        return self._open("input", device, channels, callback, status)

    def open_output(self, device: DeviceInfo, channels: int, callback: Callable[[np.ndarray], None],
                    status: Callable[[str], None] | None = None) -> OpenedStream:
        return self._open("output", device, channels, callback, status)

    def _extras(self, device: DeviceInfo) -> list[tuple[str, object]]:
        if not device.wasapi:
            return [("", None)]
        sd = self._sd
        extras: list[tuple[str, object]] = []
        try:
            extras.append(("shared, auto-convert", sd.WasapiSettings(exclusive=False, auto_convert=True)))
        except TypeError:                                      # an older sounddevice without auto_convert
            pass
        try:
            extras.append(("shared", sd.WasapiSettings(exclusive=False)))
        except Exception:                                      # noqa: BLE001
            extras.append(("", None))
        return extras

    @staticmethod
    def _channel_options(wanted: int, available: int) -> list[int]:
        options: list[int] = []
        for count in (wanted, available, 2, 1):
            if 1 <= count <= available and count not in options:
                options.append(count)
        return options

    def _open(self, kind: str, device: DeviceInfo, wanted: int, callback, status) -> OpenedStream:
        sd = self._sd
        last_error: Exception | None = None
        for channels in self._channel_options(wanted, device.channels(kind)):
            for note, extra in self._extras(device):
                if kind == "input":
                    def cb(indata, frames, time_info, flags, _cb=callback):
                        try:
                            if flags and status:
                                status(str(flags))
                            _cb(indata)
                        except Exception:                      # noqa: BLE001 — never raise into PortAudio
                            _log.exception("input callback failed")
                    cls = sd.InputStream
                else:
                    def cb(outdata, frames, time_info, flags, _cb=callback):
                        try:
                            if flags and status:
                                status(str(flags))
                            _cb(outdata)
                        except Exception:                      # noqa: BLE001
                            _log.exception("output callback failed")
                            outdata[:] = 0.0
                    cls = sd.OutputStream
                kwargs = dict(samplerate=RATE, blocksize=self.block, device=device.index, channels=channels,
                              dtype="float32", latency=self.latency, callback=cb)
                if extra is not None:
                    kwargs["extra_settings"] = extra
                try:
                    stream = cls(**kwargs)
                except Exception as exc:                       # noqa: BLE001 — PortAudioError, ValueError, ...
                    last_error = exc
                    _log.debug("open %s %r with %d ch %s failed: %s", kind, device.name, channels, note, exc)
                    continue
                _log.info("opened %s %s: %d ch%s", kind, device.label(), channels, f" ({note})" if note else "")
                return OpenedStream(device, kind, channels, stream, note)
        raise BackendError(f"cannot open {kind} device {device.name!r}: {last_error}. "
                           f"Check that it is set to 48000 Hz in the Windows sound settings and that no program "
                           f"has it open in exclusive mode.")


# ── a backend with no sound card: for tests and --selftest ───────────────────

class FakeStream(OpenedStream):
    """Drives the engine's callbacks by hand (``deliver`` / ``collect``) or from a clock thread."""

    def __init__(self, device: DeviceInfo, kind: str, channels: int, callback, status=None, backend=None):
        super().__init__(device, kind, channels, None)
        self.callback = callback
        self.running = False
        self.frames = 0                                        # frames delivered / collected so far
        self.closed = False
        self._backend = backend

    def start(self) -> None:
        backend = self._backend
        if backend is not None and backend.fail_start.get(self.device.name, 0) > 0:
            backend.fail_start[self.device.name] -= 1          # the endpoint is still "in use" (AUDCLNT_E_DEVICE_IN_USE)
            raise RuntimeError("Error starting stream: Unanticipated host error [PaErrorCode -9999]: "
                               "'AUDCLNT_E_DEVICE_IN_USE'")
        self.running = True

    def stop(self) -> None:
        self.running = False

    def close(self) -> None:
        self.running = False
        self.closed = True

    @property
    def active(self) -> bool:
        return self.running

    def deliver(self, data: np.ndarray) -> None:
        """Capture side: hand ``(frames, channels)`` audio to the engine."""
        data = np.asarray(data, dtype=np.float32)
        self.callback(data)
        self.frames += data.shape[0]

    def collect(self, frames: int) -> np.ndarray:
        """Render side: ask the engine for *frames* of audio."""
        out = np.zeros((frames, self.channels), dtype=np.float32)
        self.callback(out)
        self.frames += frames
        return out


class FakeBackend:
    """Devices that exist only in memory."""

    name = "fake"

    def __init__(self, devices: list[DeviceInfo] | None = None, max_channels: dict[str, int] | None = None):
        self._devices = list(devices or [])
        self.hotplugged: list[DeviceInfo] = []                 # devices that show up after the next refresh()
        self.refreshes = 0
        self.opened: list[FakeStream] = []
        self._cap = max_channels or {}                         # name -> channels it will actually accept
        self.fail_open: set[str] = set()                       # names that refuse to open right now
        self.fail_start: dict[str, int] = {}                   # name -> how many times its stream refuses to *start*
        self.min_channels: dict[str, int] = {}                 # name -> fewest channels it accepts (a stereo-only mic)

    def devices(self) -> list[DeviceInfo]:
        return list(self._devices)

    def refresh(self) -> None:
        self.refreshes += 1
        self._devices += self.hotplugged
        self.hotplugged = []

    def _open(self, kind: str, device: DeviceInfo, channels: int, callback, status) -> FakeStream:
        accept = min(channels, self._cap.get(device.name, device.channels(kind)))
        if device.name in self.min_channels:                    # e.g. a stereo-only microphone
            accept = max(accept, min(self.min_channels[device.name], device.channels(kind)))
        if accept < 1 or device.name in self.fail_open:
            raise BackendError(f"cannot open {kind} device {device.name!r}")
        stream = FakeStream(device, kind, accept, callback, status, backend=self)
        self.opened.append(stream)
        return stream

    def open_input(self, device, channels, callback, status=None) -> FakeStream:
        return self._open("input", device, channels, callback, status)

    def open_output(self, device, channels, callback, status=None) -> FakeStream:
        return self._open("output", device, channels, callback, status)

    def stream(self, name: str, kind: str) -> FakeStream:
        for s in reversed(self.opened):
            if s.kind == kind and name.lower() in s.device.name.lower():
                return s
        raise KeyError((name, kind))
