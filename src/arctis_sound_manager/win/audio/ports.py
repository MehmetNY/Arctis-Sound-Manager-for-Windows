# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Audio ports: the glue between a sound device's own clock and the engine's processing loop.

A device calls back whenever *it* is ready, at *its* idea of 48000 Hz; the engine processes blocks
whenever the *output* wants them. Between the two sit rings, and — to keep a ring from slowly filling
or emptying because the two clocks differ by a few tens of ppm — a resampler whose step a small
controller nudges (see :mod:`.resample`).

* :class:`InputPort`  — device callback -> ring -> (resampler) -> ``pull(n)`` for the processing loop.
* :class:`OutputPort` — ``push(block)`` from the processing loop -> ring -> device callback. The
  *master* output of a processing domain paces the whole loop; further outputs are resampled to their
  own clocks.

Nothing here knows about PortAudio or WASAPI; devices are anything that calls ``on_audio`` /
``fill`` with ``(frames, channels)`` float32 arrays, which is what makes the timing logic testable.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from arctis_sound_manager.win.audio.resample import LOOKAHEAD, StreamResampler

RATE = 48000


class FrameRing:
    """Single-producer / single-consumer ring of ``(frames, channels)`` float32 audio."""

    def __init__(self, channels: int, capacity: int = RATE):
        self.channels = channels
        self.capacity = capacity
        self._buf = np.zeros((capacity, channels), dtype=np.float32)
        self._written = 0
        self._read = 0
        self._lock = threading.Lock()
        self.overruns = 0                 # frames thrown away because the reader fell behind
        self.underruns = 0                # times the reader asked for more than there was

    @property
    def available(self) -> int:
        return self._written - self._read

    def clear(self) -> None:
        with self._lock:
            self._read = self._written

    def write(self, data: np.ndarray) -> None:
        data = np.asarray(data, dtype=np.float32)
        n = data.shape[0]
        if n == 0:
            return
        if n > self.capacity:
            data = data[-self.capacity:]
            n = self.capacity
        with self._lock:
            free = self.capacity - (self._written - self._read)
            if n > free:                                      # the reader is too slow: drop the oldest audio
                drop = n - free
                self._read += drop
                self.overruns += drop
            start = self._written % self.capacity
            first = min(n, self.capacity - start)
            self._buf[start:start + first] = data[:first]
            if first < n:
                self._buf[:n - first] = data[first:]
            self._written += n

    def read(self, n: int) -> np.ndarray | None:
        """Exactly *n* frames, or ``None`` (and an underrun is counted) when there are fewer."""
        with self._lock:
            if self._written - self._read < n:
                self.underruns += 1
                return None
            return self._take(n)

    def read_available(self, limit: int | None = None) -> np.ndarray:
        with self._lock:
            n = self._written - self._read
            if limit is not None:
                n = min(n, limit)
            return self._take(n)

    def _take(self, n: int) -> np.ndarray:
        start = self._read % self.capacity
        first = min(n, self.capacity - start)
        out = np.empty((n, self.channels), dtype=np.float32)
        out[:first] = self._buf[start:start + first]
        if first < n:
            out[first:] = self._buf[:n - first]
        self._read += n
        return out


class DriftController:
    """Turns a buffer fill level into a resampling step close to 1.0.

    It is a proportional controller with a dead zone: while the (smoothed) level stays within
    ``deadband`` frames of its target the step is exactly 1.0, which is a bit-exact copy and costs
    nothing; clocks that agree never get touched. Past the dead zone the step is pulled back in
    proportion to the excess, clamped to ``±max_ppm``. Without an integral term the level settles a
    little off target (``ppm * block / gain`` frames, ~70 frames for 300 ppm), which is harmless and
    keeps the loop from ever winding up.
    """

    def __init__(self, target: float, block: int, gain: float = 4e-3, smoothing: float = 0.02,
                 max_ppm: float = 2000.0, deadband: float = 64.0):
        self.target = float(target)
        self.block = block
        self.gain = gain
        self.smoothing = smoothing
        self.max = max_ppm * 1e-6
        self.deadband = float(deadband)
        self._level: float | None = None
        self.ratio = 1.0

    def reset(self, target: float | None = None) -> None:
        if target is not None:
            self.target = float(target)
        self._level = None
        self.ratio = 1.0

    def update(self, level: float) -> float:
        self._level = level if self._level is None else self._level + self.smoothing * (level - self._level)
        error = self._level - self.target
        excess = error - max(-self.deadband, min(self.deadband, error))
        self.ratio = 1.0 + max(-self.max, min(self.max, self.gain * excess / self.block))
        return self.ratio

    @property
    def ppm(self) -> float:
        return (self.ratio - 1.0) * 1e6


class InputPort:
    """Audio coming from a device: ``on_audio`` is the device callback, ``pull`` feeds the engine.

    A device hands its audio over in chunks (10 ms is typical), so the number of frames sitting in the
    ring jumps by a chunk whenever a callback fires; as the two clocks slide against each other that
    is a sawtooth as tall as the chunk, which a controller cannot tell from drift. The port therefore
    stamps every callback and adds what the device has accumulated since (``elapsed * rate``, at most
    one chunk) to the level it reports to the controller: the level becomes a smooth line that only
    real clock drift bends. ``clock`` is injectable so tests can run on simulated time.
    """

    def __init__(self, name: str, channels: int, block: int, target_blocks: float = 2.0,
                 capacity: int = RATE, clock=time.perf_counter, deadband: float = 64.0):
        self.name = name
        self.channels = channels
        self.block = block
        self.ring = FrameRing(channels, capacity)
        self._resampler = StreamResampler(channels)
        self._clock = clock
        self._stamp: tuple[float, int] | None = None           # (time of the last callback, its frames)
        self.target = target_blocks * block + LOOKAHEAD
        self.controller = DriftController(self.target, block, deadband=deadband)
        self.primed = False
        self.underruns = 0
        self.resyncs = 0
        self.callbacks = 0                                     # device callbacks seen (a stalled count means a dead device)

    # device side
    def on_audio(self, indata: np.ndarray) -> None:
        self.callbacks += 1
        self.ring.write(indata)
        self._stamp = (self._clock(), indata.shape[0])

    # engine side
    def reset(self) -> None:
        self.ring.clear()
        self._resampler.reset()
        self.controller.reset(self.target)
        self.primed = False

    @property
    def level(self) -> float:
        """Frames ready for the engine (what ``pull`` can actually use)."""
        return self._resampler.ahead + self.ring.available

    def _partial(self) -> float:
        """Frames the device has gathered since its last callback but not delivered yet."""
        stamp = self._stamp
        if stamp is None:
            return 0.0
        return min(max((self._clock() - stamp[0]) * RATE, 0.0), float(stamp[1]))

    def _rebase(self) -> None:
        """Start regulating around wherever the stream landed (the level is on target right now)."""
        self.controller.reset(self.target + self._partial())

    def pull(self, n: int | None = None) -> np.ndarray:
        """``(channels, n)`` float64. Silence — and a counted underrun — when the device is not keeping up."""
        n = n or self.block
        fresh = self.ring.read_available()
        if fresh.shape[0]:
            self._resampler.push(fresh.T)
        if not self.primed:
            if self._resampler.ahead < self.target:
                return np.zeros((self.channels, n))
            self._resampler.skip_to(self.target)             # start exactly on the target, not up to a chunk above it
            self._rebase()
            self.primed = True
        # far off target (a device stalled, or just woke up): jump instead of waiting for the controller
        if self._resampler.ahead > 6 * self.block + self.target:
            self._resampler.skip_to(self.target)
            self.resyncs += 1
            self._rebase()
        ratio = self.controller.update(self._resampler.ahead + self._partial())
        out = self._resampler.pull(n, ratio)
        if out is None:
            self.underruns += 1
            self.primed = False                                # refill to the target before playing again
            return np.zeros((self.channels, n))
        return out


class OutputPort:
    """Audio going to a device: ``push`` from the engine, ``fill_device`` is the device callback.

    The *master* output of a processing domain is a plain ring: the engine produces exactly what the
    device consumes, because the device paces the engine. Any further (*slave*) output has a clock of
    its own and goes through a resampler whose step the drift controller nudges. As on the input side,
    the engine hands over audio in blocks, which would make the fill level a sawtooth as tall as a
    block; the level the controller sees is corrected for the part of the last block that the engine's
    own clock has not "produced" yet.
    """

    def __init__(self, name: str, channels: int, block: int, master: bool = True, target_blocks: float = 2.0,
                 capacity: int = RATE, clock=time.perf_counter, deadband: float = 64.0):
        self.name = name
        self.channels = channels
        self.block = block
        self.master = master
        self.target = int(target_blocks * block)
        self.ring = FrameRing(channels, capacity)
        self.underruns = 0
        self.started = False                                   # the device has asked for audio at least once
        self.primed = master                                   # a slave waits until it holds its target
        self._clock = clock
        self._stamp: tuple[float, int] | None = None           # (time of the last push, its frames)
        self.callbacks = 0
        self.wake = threading.Event()                          # the master's device callback wakes the engine's loop
        self._resampler = None if master else StreamResampler(channels)
        self.controller = None if master else DriftController(self.target + LOOKAHEAD, block, deadband=deadband)

    # engine side
    @property
    def fill(self) -> int:
        """Frames waiting for the device (for the master output this is what paces the engine)."""
        extra = 0 if self._resampler is None else self._resampler.ahead
        return int(self.ring.available + extra)

    def reset(self) -> None:
        self.ring.clear()
        self.started = False
        if self._resampler is not None:
            self._resampler.reset()
            self.controller.reset(self.target + LOOKAHEAD)
            self.primed = False

    def push(self, block: np.ndarray) -> None:
        self.ring.write(np.ascontiguousarray(block.T, dtype=np.float32))
        self._stamp = (self._clock(), block.shape[1])

    # device side
    def _unproduced(self) -> float:
        """Frames of the last pushed block that, on a smooth clock, would not have been produced yet."""
        stamp = self._stamp
        if stamp is None:
            return 0.0
        return stamp[1] - min(max((self._clock() - stamp[0]) * RATE, 0.0), float(stamp[1]))

    def fill_device(self, outdata: np.ndarray) -> None:
        n = outdata.shape[0]
        self.started = True
        self.callbacks += 1
        if self.master:
            self.wake.set()
        if self._resampler is None:
            got = self.ring.read(n)
            if got is None:                                    # not enough: play what there is, then silence
                part = self.ring.read_available(n)
                outdata[:part.shape[0]] = part
                outdata[part.shape[0]:] = 0.0
                self.underruns += 1
            else:
                outdata[:] = got
            return
        fresh = self.ring.read_available()
        if fresh.shape[0]:
            self._resampler.push(fresh.T)
        if not self.primed:
            if self._resampler.ahead < self.target + LOOKAHEAD:
                outdata[:] = 0.0
                return
            # start exactly on the target (not up to a block above it) and regulate around wherever that lands:
            # the level the controller sees depends on where the device's callbacks happen to fall within the
            # engine's block cycle, and chasing that offset would bend the pitch for seconds after every start
            self._resampler.skip_to(self.target + LOOKAHEAD)
            self.controller.reset(self._resampler.ahead - self._unproduced())
            self.primed = True
        ratio = self.controller.update(self._resampler.ahead - self._unproduced())
        out = self._resampler.pull(n, ratio)
        if out is None:
            outdata[:] = 0.0
            self.underruns += 1
            self.primed = False
        else:
            outdata[:] = out.T
