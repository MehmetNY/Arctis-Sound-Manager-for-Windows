# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The running engine: devices -> ports -> graph -> devices, with the threads that pace it.

Two *processing domains* run independently, each paced by the device it ends in::

    playback   virtual cables (game / chat / media / aux) -> EngineGraph -> headset   (+ optional 2nd output)
    microphone real microphone -> MicChain -> a virtual cable that other programs record from

A domain's thread sleeps until its master output's device callback wakes it, then produces just enough
blocks to refill that output to its target. Everything else follows from that: inputs are resampled a
few ppm to follow their own clocks (:mod:`.ports`), a device that stops delivering is noticed and
re-opened (``supervise``), and a changed state file is picked up without a click (``StateWatcher``).

The classes here do no sleeping of their own and take their devices from a *backend*, so the whole
engine can be driven step by step against :class:`~.backend.FakeBackend` in tests.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from arctis_sound_manager.win.audio import fast
from arctis_sound_manager.win.audio.backend import BackendError, DeviceInfo, OpenedStream, find_device
from arctis_sound_manager.win.audio.engine_config import EngineConfig
from arctis_sound_manager.win.audio.graph import CHANNELS, CONTROLS, MASTER, EngineGraph, StateSnapshot, read_state
from arctis_sound_manager.win.audio.params import default_config_dir
from arctis_sound_manager.win.audio.ports import InputPort, OutputPort
from arctis_sound_manager.win.audio.realtime import ThreadBoost

_log = logging.getLogger(__name__)

RATE = 48000
MAX_CATCHUP = 12                        # blocks per wake-up, however late the thread was
STALE_POLLS = 2                         # supervisor polls without a callback before a device counts as dead
RETRY_SECONDS = 2.0
REFRESH_SECONDS = 15.0
SETTINGS_FILES = ("sonar_micro_state.json", "general_settings.yaml", "channel_volumes.json", "stream_mix.json")
VOLUME_KEYS = {"Arctis_Game": "game", "Arctis_Chat": "chat", "Arctis_Media": "media", "Arctis_Master": "master"}


# ── helpers ──────────────────────────────────────────────────────────────────

def to_channels(audio: np.ndarray, channels: int) -> np.ndarray:
    """``(2, n)`` stereo -> what a device with *channels* wants (mono: the average; more: extra ones silent)."""
    if channels == 2:
        return audio
    if channels == 1:
        return audio.mean(axis=0, keepdims=True)
    return np.vstack([audio, np.zeros((channels - 2, audio.shape[1]))])


class SnapshotBox:
    """Hands the newest :class:`StateSnapshot` from the monitor thread to the audio threads."""

    def __init__(self):
        self.snapshot: StateSnapshot | None = None
        self.version = 0

    def publish(self, snapshot: StateSnapshot) -> None:
        self.snapshot = snapshot                      # first the data ...
        self.version += 1                             # ... then the counter the readers compare


class _Stats:
    """Per-pipeline timing: how much of the real-time budget a block costs."""

    def __init__(self, block: int):
        self.budget = block / RATE
        self.blocks = 0
        self.errors = 0
        self.load = 0.0                               # smoothed processing time / block time
        self.peak = 0.0                               # worst block since the last ``take_peak``

    def add(self, seconds: float) -> None:
        self.blocks += 1
        ratio = seconds / self.budget
        self.load += 0.02 * (ratio - self.load)
        self.peak = max(self.peak, ratio)

    def take_peak(self) -> float:
        peak, self.peak = self.peak, 0.0
        return peak

    def as_dict(self) -> dict:
        return {"blocks": self.blocks, "errors": self.errors, "load": round(self.load, 4)}


# ── the two processing domains ───────────────────────────────────────────────

class Pipeline:
    """Common wake / produce loop; subclasses say what one block is and when there is a master."""

    name = "pipeline"

    def __init__(self, box: SnapshotBox, block: int):
        self.box = box
        self.block = block
        self.stats = _Stats(block)
        self._applied = -1
        self._error_log = 0
        self.failed = False

    # to be provided
    def master(self) -> OutputPort | None: raise NotImplementedError
    def process_block(self) -> None: raise NotImplementedError
    def apply(self, snapshot: StateSnapshot) -> None: raise NotImplementedError

    def sync_state(self) -> None:
        if self.box.version != self._applied:
            version, snapshot = self.box.version, self.box.snapshot
            self._applied = version
            if snapshot is not None:
                try:
                    self.apply(snapshot)
                except Exception:                                   # noqa: BLE001 — a bad setting must not stop the audio
                    _log.exception("%s: applying the new settings failed", self.name)

    def prefill(self) -> None:
        self.sync_state()
        master = self.master()
        if master is None:
            return
        for _ in range(MAX_CATCHUP):
            if master.fill >= master.target:
                break
            self._guarded_block()

    def _guarded_block(self) -> None:
        t0 = time.perf_counter()
        try:
            self.process_block()
            self.failed = False
        except Exception:                                           # noqa: BLE001 — fail silent, never freeze
            self.stats.errors += 1
            self.failed = True
            if self._error_log < 3 or self.stats.errors % 200 == 0:
                _log.exception("%s: processing a block failed (%d so far)", self.name, self.stats.errors)
            self._error_log += 1
            self.silence_master()
        self.stats.add(time.perf_counter() - t0)

    def silence_master(self) -> None:
        master = self.master()
        if master is not None:
            master.push(np.zeros((master.channels, self.block)))

    def step(self) -> int:
        """Produce what the master output is missing; returns the number of blocks made."""
        self.sync_state()
        master = self.master()
        made = 0
        if master is None:
            return 0
        while master.fill < master.target and made < MAX_CATCHUP:
            self._guarded_block()
            made += 1
        return made

    def run(self, stop: threading.Event) -> None:
        with ThreadBoost():
            while not stop.is_set():
                master = self.master()
                if master is None:
                    stop.wait(0.1)
                    continue
                master.wake.wait(0.05)
                master.wake.clear()
                self.step()


class PlaybackPipeline(Pipeline):
    name = "playback"

    def __init__(self, graph: EngineGraph, box: SnapshotBox, block: int, master_bus: str = "headset"):
        super().__init__(box, block)
        self.graph = graph
        self.inputs: dict[str, InputPort] = {}
        self.outputs: dict[str, OutputPort] = {}
        self.master_bus = master_bus
        self.mic_tap: InputPort | None = None                       # the processed microphone, for Streamer mode's mix
        self._stream_was_on = False

    def master(self) -> OutputPort | None:
        return self.outputs.get(self.master_bus)

    def apply(self, snapshot: StateSnapshot) -> None:
        self.graph.apply_playback(snapshot)

    def process_block(self) -> None:
        pulled = {ch: port.pull(self.block) for ch, port in list(self.inputs.items())}
        stream_on = self.graph.stream.enabled and "stream" in self.outputs
        mic = None
        if stream_on:
            tap = self.mic_tap
            if tap is not None:
                if not self._stream_was_on:
                    tap.reset()                                     # start from the microphone as it is now, not as it was
                mic = tap.pull(self.block)[0]
        self._stream_was_on = stream_on
        buses = self.graph.process(pulled, mic=mic, stream=stream_on)
        for bus, port in list(self.outputs.items()):
            audio = buses.get(bus)
            if audio is None:
                audio = np.zeros((2, self.block))
            port.push(to_channels(audio, port.channels))


class MicPipeline(Pipeline):
    name = "microphone"

    def __init__(self, graph: EngineGraph, box: SnapshotBox, block: int):
        super().__init__(box, block)
        self.graph = graph
        self.source: InputPort | None = None
        self.sink: OutputPort | None = None
        self.tap: InputPort | None = None                           # hands the processed microphone to the playback side

    def master(self) -> OutputPort | None:
        return self.sink if self.source is not None else None

    def apply(self, snapshot: StateSnapshot) -> None:
        self.graph.apply_mic(snapshot)

    def process_block(self) -> None:
        source, sink = self.source, self.sink
        x = source.pull(self.block)
        mono = x[0] if x.shape[0] == 1 else x.mean(axis=0)
        y = self.graph.process_mic(mono)
        sink.push(to_channels(np.vstack([y, y]), sink.channels))
        tap = self.tap
        if tap is not None and self.graph.stream.enabled:
            tap.on_audio(np.ascontiguousarray(y, dtype=np.float32)[:, None])


# ── state files ──────────────────────────────────────────────────────────────

@dataclass
class EngineState:
    """What the monitor thread read: the graph snapshot plus the controls kept in other files."""

    snapshot: StateSnapshot
    volumes: dict[str, float] = field(default_factory=dict)         # only when channel_volumes.json itself changed
    chatmix_channels: list[str] | None = None                       # only when general_settings.yaml itself changed


class StateWatcher:
    """Notices changes in ASM's state files and reads them once they have settled."""

    def __init__(self, config_dir: Path, hrir_id: str | None = None, rate: int = RATE,
                 reader: Callable[..., StateSnapshot] = read_state):
        self.config_dir = config_dir
        self.hrir_id = hrir_id
        self.rate = rate
        self._reader = reader
        self._read_stamp: tuple | None = None
        self._last_stamp: tuple | None = None
        self._volumes_stamp: tuple | None = None
        self._settings_stamp: tuple | None = None

    def stamp(self) -> tuple:
        files = [*self.config_dir.glob("sonar_eq_state_*.json"), *self.config_dir.glob("sonar_spatial_audio*.json"),
                 *(self.config_dir / name for name in SETTINGS_FILES)]
        out = []
        for path in sorted(files):
            try:
                st = path.stat()
            except OSError:
                continue
            out.append((path.name, st.st_mtime_ns, st.st_size))
        return tuple(out)

    def poll(self) -> EngineState | None:
        """Call regularly. Returns a fresh state after a change has been stable for one poll, else ``None``."""
        stamp = self.stamp()
        settled = stamp == self._last_stamp                         # unchanged since the previous poll: not mid-write
        self._last_stamp = stamp
        if not settled or stamp == self._read_stamp:
            return None
        return self.read(stamp)

    def read(self, stamp: tuple | None = None) -> EngineState:
        self._read_stamp = stamp if stamp is not None else self.stamp()
        snapshot = self._reader(self.config_dir, self.hrir_id, self.rate)
        # Volumes and the ChatMix channel list are *controls* that also arrive live over the control channel;
        # take them from the files only when those files themselves changed, so an EQ edit can never
        # snap a slider back to an old saved value.
        volumes_stamp = self._file_stamp("channel_volumes.json")
        settings_stamp = self._file_stamp("general_settings.yaml")
        volumes = self._read_volumes() if volumes_stamp != self._volumes_stamp or self._volumes_stamp is None else {}
        channels = self._read_chatmix_channels() if settings_stamp != self._settings_stamp or self._settings_stamp is None else None
        self._volumes_stamp, self._settings_stamp = volumes_stamp, settings_stamp
        return EngineState(snapshot, volumes, channels)

    def _file_stamp(self, name: str) -> tuple:
        try:
            st = (self.config_dir / name).stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return ()

    def _read_volumes(self) -> dict[str, float]:
        import json
        try:
            raw = json.loads((self.config_dir / "channel_volumes.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict[str, float] = {}
        if isinstance(raw, dict):
            for node, channel in VOLUME_KEYS.items():
                value = raw.get(node)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    out[channel] = float(max(0, min(100, value)))
        return out

    def _read_chatmix_channels(self) -> list[str]:
        try:
            from arctis_sound_manager.settings import GeneralSettings
            return list(GeneralSettings.read_from_file().chatmix_channels_or_default())
        except Exception:                                           # noqa: BLE001 — settings must never stop the audio
            return ["game"]


# ── devices ──────────────────────────────────────────────────────────────────

@dataclass
class Endpoint:
    """One device the engine wants open: what it is called, how it is doing, and its port once opened."""

    key: str                       # "in:game", "out:headset", "mic:in", "mic:out"
    kind: str                      # "input" | "output"
    query: str                     # the name piece from the config
    wanted: int                    # channels to ask for
    port: InputPort | OutputPort | None = None
    stream: OpenedStream | None = None
    state: str = "closed"          # closed | running | waiting
    device: str = ""
    error: str = ""
    attempts: int = 0
    next_try: float = 0.0
    last_count: int = -1
    stale: int = 0
    reopened: int = 0

    def info(self) -> dict:
        port = self.port
        out = {"state": self.state, "device": self.device, "channels": self.stream.channels if self.stream else 0,
               "callbacks": getattr(port, "callbacks", 0), "reopened": self.reopened}
        if self.error:
            out["error"] = self.error
        if isinstance(port, InputPort):
            out.update(underruns=port.underruns, resyncs=port.resyncs, overruns=port.ring.overruns,
                       drift_ppm=round(port.controller.ppm, 1), level=int(port.level))
        elif isinstance(port, OutputPort):
            out.update(underruns=port.underruns, fill=port.fill)
        return out


class Engine:
    """Owns the graph, the devices and the threads."""

    def __init__(self, config: EngineConfig, backend, config_dir: Path | None = None, native=None,
                 hrir_id: str | None = None, probe: Callable[[], list[DeviceInfo]] | None = None,
                 clock: Callable[[], float] = time.monotonic, port_clock: Callable[[], float] = time.perf_counter):
        self.config = config
        self.backend = backend
        self.config_dir = config_dir or default_config_dir()
        self._clock = clock
        self._port_clock = port_clock                                # what the ports stamp device callbacks with (tests: simulated time)
        self._probe = probe                                          # a fresh device listing from another process
        self.box = SnapshotBox()
        self.mic_tap: InputPort | None = None
        self.profile = config.profile
        self.block = config.playback_block
        self.mic_block = config.microphone_block
        self.graph = EngineGraph(RATE, self.block, master_limiter=config.master_limiter,
                                 routing=dict(config.routing), native=native)
        self.watcher = StateWatcher(self.config_dir, hrir_id, RATE)
        self.playback = PlaybackPipeline(self.graph, self.box, self.block)
        self.mic = MicPipeline(self.graph, self.box, self.mic_block)
        if config.mic_enabled:
            # the two domains run on different clocks (the microphone's cable and the headset): the tap is a
            # drift-controlled port between them. It must be able to hold one microphone block plus a margin.
            self.mic_tap = InputPort("mic:tap", 1, self.block, target_blocks=self.mic_block / self.block + 1.5,
                                      clock=self._port_clock)
            self.playback.mic_tap = self.mic_tap
            self.mic.tap = self.mic_tap
        self.endpoints: dict[str, Endpoint] = {}
        self._devices: list[DeviceInfo] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        self._last_refresh = -1e9
        self.notes: list[str] = []
        self.running = False
        # live controls
        self.volumes = {ch: 100.0 for ch in CONTROLS}
        self.muted = {ch: False for ch in CONTROLS}
        self.mix = (100.0, 100.0)                                    # (non-chat channels, chat)
        self.chatmix_channels = ["game"]
        self.preset: dict = {}                                       # controls to apply right after the files are read
        self._declare_endpoints()

    # ── set-up ──────────────────────────────────────────────────────────────

    def _declare_endpoints(self) -> None:
        cfg = self.config
        for ch in CHANNELS:
            if cfg.inputs.get(ch):
                self.endpoints[f"in:{ch}"] = Endpoint(f"in:{ch}", "input", cfg.inputs[ch], 8 if ch != "chat" else 2)
        for bus, query in cfg.outputs.items():
            if query:
                self.endpoints[f"out:{bus}"] = Endpoint(f"out:{bus}", "output", query, 2)
        if cfg.mic_enabled:
            self.endpoints["mic:in"] = Endpoint("mic:in", "input", cfg.mic_input, 1)
            self.endpoints["mic:out"] = Endpoint("mic:out", "output", cfg.mic_output, 2)

    def start(self, threaded: bool = True) -> None:
        """Open every device that can be opened, pre-fill the outputs, start the streams and the threads."""
        fast.wait_ready()                                            # kernels compiled before any audio callback runs
        with self._lock:
            self._devices = self.backend.devices()
            self.load_state(self.watcher.read())
            self._apply_preset()
            for ep in self.endpoints.values():
                self._try_open(ep, self._clock(), start_stream=False)
            self.playback.prefill()
            self.mic.prefill()
            for ep in self.endpoints.values():
                if ep.stream is not None:
                    ep.stream.start()
            self.running = True
        if threaded:
            self._stop.clear()
            for target, name in ((self.playback.run, "asm-playback"), (self.mic.run, "asm-microphone")):
                t = threading.Thread(target=target, args=(self._stop,), name=name, daemon=True)
                t.start()
                self._threads.append(t)
            t = threading.Thread(target=self._monitor, name="asm-monitor", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for pipeline in (self.playback, self.mic):
            master = pipeline.master()
            if master is not None:
                master.wake.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        with self._lock:
            for ep in self.endpoints.values():
                self._close(ep)
            self.running = False

    # ── devices ─────────────────────────────────────────────────────────────

    def _buffering(self, ep: Endpoint) -> tuple[int, float]:
        """(processing block, blocks of audio to keep in hand) for an endpoint's port."""
        block = self.mic_block if ep.key.startswith("mic") else self.block
        return block, self.profile.input_blocks if ep.kind == "input" else self.profile.output_blocks

    def _try_open(self, ep: Endpoint, now: float, start_stream: bool = True) -> bool:
        """Find the device, open it, build its port and hook it into its pipeline. ``False`` (and a retry time) on failure."""
        ep.attempts += 1
        device = find_device(self._devices, ep.query, ep.kind)
        if device is None:
            ep.state, ep.error = "waiting", f"no device matching '{ep.query}'"
            ep.next_try = now + RETRY_SECONDS
            return False
        block, blocks = self._buffering(ep)
        try:
            if ep.kind == "input":
                stream = self.backend.open_input(device, ep.wanted, lambda data, ep=ep: self._on_input(ep, data),
                                                 lambda flags, ep=ep: self._on_status(ep, flags))
            else:
                stream = self.backend.open_output(device, ep.wanted, lambda out, ep=ep: self._on_output(ep, out),
                                                  lambda flags, ep=ep: self._on_status(ep, flags))
        except BackendError as exc:
            ep.state, ep.error = "waiting", str(exc)
            ep.next_try = now + RETRY_SECONDS
            _log.warning("%s: %s", ep.key, exc)
            return False
        ep.stream, ep.device, ep.error = stream, device.name, ""
        ep.port = self._make_port(ep, stream.channels, block, blocks)
        self._attach(ep)
        ep.state, ep.attempts, ep.last_count, ep.stale = "running", 0, -1, 0
        if start_stream:
            stream.start()
        _log.info("%s: %s (%d ch)", ep.key, device.name, stream.channels)
        return True

    def _make_port(self, ep: Endpoint, channels: int, block: int, blocks: float):
        name = ep.key
        if ep.kind == "input":
            return InputPort(name, channels, block, target_blocks=blocks, clock=self._port_clock)
        master = ep.key in ("out:headset", "mic:out")
        return OutputPort(name, channels, block, master=master, target_blocks=blocks, clock=self._port_clock)

    def _attach(self, ep: Endpoint) -> None:
        if ep.key.startswith("in:"):
            self.playback.inputs[ep.key[3:]] = ep.port
        elif ep.key.startswith("out:"):
            self.playback.outputs[ep.key[4:]] = ep.port
        elif ep.key == "mic:in":
            self.mic.source = ep.port
        elif ep.key == "mic:out":
            self.mic.sink = ep.port

    def _detach(self, ep: Endpoint) -> None:
        if ep.key.startswith("in:"):
            self.playback.inputs.pop(ep.key[3:], None)
        elif ep.key.startswith("out:"):
            self.playback.outputs.pop(ep.key[4:], None)
        elif ep.key == "mic:in":
            self.mic.source = None
        elif ep.key == "mic:out":
            self.mic.sink = None

    def _close(self, ep: Endpoint) -> None:
        stream, ep.stream = ep.stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        self._detach(ep)
        ep.port = None
        ep.state = "closed"

    def _on_input(self, ep: Endpoint, data: np.ndarray) -> None:
        port = ep.port
        if port is not None:
            port.on_audio(data)

    def _on_output(self, ep: Endpoint, out: np.ndarray) -> None:
        port = ep.port
        if port is None:
            out[:] = 0.0
        else:
            port.fill_device(out)

    def _on_status(self, ep: Endpoint, flags: str) -> None:
        _log.debug("%s: stream status %s", ep.key, flags)

    # ── supervision ─────────────────────────────────────────────────────────

    def supervise(self, now: float | None = None) -> None:
        """Notice dead devices, re-open what is closed, refresh PortAudio's device list when it must."""
        now = self._clock() if now is None else now
        with self._lock:
            failing = False
            for ep in self.endpoints.values():
                if ep.state == "running" and ep.port is not None:
                    count = ep.port.callbacks
                    if count == ep.last_count:
                        ep.stale += 1
                    else:
                        ep.stale, ep.last_count = 0, count
                    if ep.stale >= STALE_POLLS:
                        _log.warning("%s: %s stopped delivering audio; closing it", ep.key, ep.device)
                        self._close(ep)
                        ep.state, ep.error, ep.next_try = "waiting", "the device stopped responding", now
                if ep.state != "running":
                    failing = failing or ep.attempts >= 3 or "no device matching" in ep.error
                    if now >= ep.next_try:
                        if self._try_open(ep, now):
                            ep.reopened += 1
                            self._after_reopen(ep)
            if failing:
                self._maybe_refresh(now)

    def _after_reopen(self, ep: Endpoint) -> None:
        for pipeline in (self.playback, self.mic):
            master = pipeline.master()
            if master is not None and master is ep.port:
                master.reset()
                pipeline.prefill()

    def _maybe_refresh(self, now: float) -> None:
        """PortAudio only lists the devices that existed when it started; a rebuild re-reads them.

        It interrupts every stream, so it is rationed, and when a *probe* (a fresh listing from another
        process) is available it only happens if the probe shows that a wanted device really is there now.
        Without a probe it only happens while no output is running (nothing audible to interrupt).
        """
        if now - self._last_refresh < REFRESH_SECONDS or not hasattr(self.backend, "refresh"):
            return
        audible = any(ep.state == "running" for key, ep in self.endpoints.items() if key in ("out:headset", "mic:out"))
        if self._probe is None and audible:
            return                                                    # a blind rebuild would interrupt sound someone hears
        self._last_refresh = now
        if self._probe is not None:
            try:
                fresh = self._probe()
            except Exception as exc:                                  # noqa: BLE001
                _log.debug("device probe failed: %r", exc)
                return
            wanted = [ep for ep in self.endpoints.values() if ep.state != "running"]
            if not any(find_device(fresh, ep.query, ep.kind) is not None for ep in wanted):
                return
        _log.info("a wanted device appeared; rebuilding the sound system")
        for ep in self.endpoints.values():
            self._close(ep)
        try:
            self.backend.refresh()
            self._devices = self.backend.devices()
        except BackendError as exc:
            _log.warning("could not refresh the device list: %s", exc)
        for ep in self.endpoints.values():
            ep.attempts = 0
            self._try_open(ep, now, start_stream=False)
        for pipeline in (self.playback, self.mic):
            pipeline.prefill()
        for ep in self.endpoints.values():
            if ep.stream is not None:
                ep.stream.start()

    # ── state and controls ──────────────────────────────────────────────────

    def load_state(self, state: EngineState) -> None:
        """Apply a freshly read state: graph snapshot (picked up by the audio threads), volumes, ChatMix channels."""
        self.box.publish(state.snapshot)
        for ch, pct in state.volumes.items():
            self.volumes[ch] = pct
        if state.chatmix_channels is not None:
            self.chatmix_channels = list(state.chatmix_channels)
        self._push_controls()

    def _apply_preset(self) -> None:
        """What the daemon / GUI set before this engine existed (a restart must not lose it)."""
        preset = self.preset
        for ch, pct in preset.get("volumes", {}).items():
            self.volumes[ch] = pct
        for ch, muted in preset.get("muted", {}).items():
            self.muted[ch] = muted
        if preset.get("mix") is not None:
            self.mix = tuple(preset["mix"])
        if preset.get("mix_channels") is not None:
            self.chatmix_channels = list(preset["mix_channels"])
        self._push_controls()

    def reload(self) -> None:
        self.load_state(self.watcher.read())

    def _push_controls(self) -> None:
        channels_pct, chat_pct = self.mix
        for ch in CHANNELS:
            if ch == "chat":
                mix = chat_pct
            else:
                mix = channels_pct if ch in self.chatmix_channels else 100.0
            self.graph.strips[ch].set_controls(volume_pct=self.volumes[ch], muted=self.muted[ch], chatmix_pct=mix)
        self.graph.set_master(volume_pct=self.volumes[MASTER], muted=self.muted[MASTER])

    def set_volume(self, channel: str, pct: float) -> None:
        if channel not in CONTROLS:
            raise ValueError(f"unknown channel {channel!r}")
        self.volumes[channel] = float(max(0.0, min(100.0, pct)))
        self._push_controls()

    def set_mute(self, channel: str, muted: bool) -> None:
        if channel not in CONTROLS:
            raise ValueError(f"unknown channel {channel!r}")
        self.muted[channel] = bool(muted)
        self._push_controls()

    def set_chatmix(self, channels_pct: float, chat_pct: float, channels: list[str] | None = None) -> None:
        self.mix = (float(max(0.0, min(100.0, channels_pct))), float(max(0.0, min(100.0, chat_pct))))
        if channels is not None:
            self.chatmix_channels = [c for c in channels if c in CHANNELS and c != "chat"]
        self._push_controls()

    def poll_state(self) -> bool:
        """One tick of the file watcher; ``True`` if something new was loaded."""
        try:
            state = self.watcher.poll()
        except Exception:                                            # noqa: BLE001 — a half-written file, a bad HRIR ...
            _log.exception("reading the state files failed")
            return False
        if state is None:
            return False
        self.load_state(state)
        return True

    # ── threads and reporting ───────────────────────────────────────────────

    def _monitor(self) -> None:
        tick = 0
        while not self._stop.wait(0.25):
            tick += 1
            self.poll_state()
            if tick % 4 == 0:
                try:
                    self.supervise()
                except Exception:                                    # noqa: BLE001
                    _log.exception("supervising the devices failed")
            if tick % 40 == 0:
                self._log_health()

    def _log_health(self) -> None:
        for pipeline in (self.playback, self.mic):
            peak = pipeline.stats.take_peak()
            if pipeline.stats.load > 0.8 or peak > 1.0:
                _log.warning("%s: using %.0f%% of its time budget (worst block %.0f%%) - "
                             "try the 'safe' latency profile if the sound crackles",
                             pipeline.name, pipeline.stats.load * 100, peak * 100)

    def status(self) -> dict:
        notes = list(self.notes) + list(self.graph.mic.notes)
        if self.graph.stream.enabled and "out:stream" not in self.endpoints:
            notes.append("Streamer mode is on, but no stream output cable is chosen (Settings > Audio engine).")
        return {
            "running": self.running,
            "profile": self.config.latency,
            "block": self.block,
            "mic_block": self.mic_block,
            "latency_ms": round(self.profile.approx_latency_ms(), 1),
            "endpoints": {key: ep.info() for key, ep in self.endpoints.items()},
            "playback": {**self.playback.stats.as_dict(), "active": self.playback.master() is not None,
                         "spatial": [ch for ch, s in self.graph.strips.items() if s.spatial_active]},
            "microphone": {**self.mic.stats.as_dict(), "active": self.mic.master() is not None,
                           "noise_engine": self.graph.mic.noise_engine_active},
            "volumes": dict(self.volumes), "muted": dict(self.muted),
            "stream": {"enabled": self.graph.stream.enabled, "output": "out:stream" in self.endpoints},
            "chatmix": {"channels_pct": self.mix[0], "chat_pct": self.mix[1], "channels": list(self.chatmix_channels)},
            "notes": notes,
            "problems": self.config.problems(),
        }

    def handle(self, request: dict) -> dict:
        """The control-channel commands (see :mod:`.control`)."""
        cmd = request.get("cmd")
        if cmd == "ping":
            return {"pong": True}
        if cmd == "status":
            return {"status": self.status()}
        if cmd == "volume":
            self.set_volume(str(request["channel"]), float(request["pct"]))
        elif cmd == "mute":
            self.set_mute(str(request["channel"]), bool(request["muted"]))
        elif cmd == "chatmix":
            channels = request.get("channels")
            self.set_chatmix(float(request["channels_pct"]), float(request["chat_pct"]),
                             list(channels) if isinstance(channels, list) else None)
        elif cmd == "reload":
            self.reload()
        else:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        return {}
