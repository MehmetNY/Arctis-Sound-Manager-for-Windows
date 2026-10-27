# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Variable-rate resampling for clock-drift compensation.

Two audio devices never run at exactly the same speed: a USB headset, a virtual cable and the sound
card each count their own "48000 Hz". Left alone, the buffer between them slowly fills or empties and
the sound glitches every few seconds. The engine therefore reads each input a hair faster or slower
than real time (a few tens of ppm) — a *fractional* step through the input stream — and this class
does that reading.

The interpolator is a Kaiser-windowed sinc, 32 taps, 512 phases (linearly interpolated between
phases): flat within 0.01 dB up to 20 kHz at every fractional position, and about 120 dB of
signal-to-error, so a constant 0.5-sample offset or a ratio that wanders does not colour the sound.
When the step is exactly 1.0 and the position is on a sample the reader copies instead of filtering.
It does not delay the stream, but it needs ``LOOKAHEAD`` input samples beyond the last one it returns.
"""
from __future__ import annotations

import numpy as np

from arctis_sound_manager.win.audio import fast

TAPS = 32
PHASES = 512
BETA = 9.0
HISTORY = TAPS // 2 - 1          # taps before the sample position
LOOKAHEAD = TAPS // 2            # taps after it


def _build_table() -> np.ndarray:
    half = TAPS // 2
    frac = np.arange(PHASES + 1) / PHASES
    offsets = np.arange(TAPS) - HISTORY                         # -15 .. +16 around floor(position)
    d = offsets[None, :] - frac[:, None]
    window = np.i0(BETA * np.sqrt(np.clip(1.0 - (d / half) ** 2, 0.0, None))) / np.i0(BETA)
    table = np.sinc(d) * window
    return table / table.sum(axis=1, keepdims=True)


_TABLE = _build_table()
_OFFSETS = np.arange(TAPS) - HISTORY


def _fractional_read_py(buf, table, pos0, ratio, n, out):
    """``out[c, k] = sum_t buf[c, floor(pos_k) - HISTORY + t] * lerp(table, phase_k)[t]`` with ``pos_k = pos0 + k * ratio``:
    the same arithmetic as the vectorised code in :meth:`StreamResampler._interpolate`, as plain loops for numba.

    The vectorised code builds an ``(n, 32)`` matrix of taps and gathers an ``(channels, n, 32)`` block of samples for
    every call - a megabyte of memory traffic per input per block, about a millisecond. Doing that for the four
    channel inputs of the engine was most of its CPU use."""
    channels = buf.shape[0]
    taps_n = table.shape[1]
    phases = table.shape[0] - 1
    hist = taps_n // 2 - 1
    taps = np.empty(taps_n)
    for k in range(n):
        pos = pos0 + k * ratio
        base = int(np.floor(pos))
        phase = (pos - base) * phases
        p0 = int(phase)
        if p0 > phases - 1:
            p0 = phases - 1
        w = phase - p0
        for t in range(taps_n):
            taps[t] = table[p0, t] * (1.0 - w) + table[p0 + 1, t] * w
        start = base - hist
        for c in range(channels):
            acc = 0.0
            for t in range(taps_n):
                acc += buf[c, start + t] * taps[t]
            out[c, k] = acc


_READ_KERNEL = fast.compile(_fractional_read_py)


class StreamResampler:
    """Fractional-step reader over a stream pushed in arbitrary chunks.

    ``push`` appends input; ``pull(n, ratio)`` returns *n* output samples advancing ``ratio`` input
    samples per output sample (``ratio`` > 1 consumes the input faster), or ``None`` when the input
    does not reach far enough yet.
    """

    def __init__(self, channels: int):
        self.channels = channels
        self.reset()

    def reset(self) -> None:
        self._buf = np.zeros((self.channels, HISTORY), dtype=np.float64)   # silence before the first sample
        self._pos = float(HISTORY)                                          # where the next output sample sits

    @property
    def ahead(self) -> float:
        """Input samples buffered at or after the read position (includes the ``LOOKAHEAD`` it keeps back)."""
        return self._buf.shape[1] - self._pos

    def skip_to(self, ahead: float) -> int:
        """Discard the oldest input ahead of the read position until only *ahead* samples remain.

        For when a stream has stalled and a backlog built up: jumping (one click) beats playing it all
        out late. Returns how many samples were dropped.
        """
        drop = int(self.ahead - ahead)
        if drop <= 0:
            return 0
        cut = int(np.floor(self._pos))
        self._buf = np.concatenate([self._buf[:, :cut], self._buf[:, cut + drop:]], axis=1)
        return drop

    def push(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=np.float64)
        self._buf = np.concatenate([self._buf, x], axis=1) if x.size else self._buf

    def pull(self, n: int, ratio: float = 1.0) -> np.ndarray | None:
        last = self._pos + (n - 1) * ratio
        need = int(np.floor(last)) + LOOKAHEAD + 1                # highest index touched + 1
        if need > self._buf.shape[1]:
            return None
        first = self._pos
        if ratio == 1.0 and first == np.floor(first):
            i0 = int(first)
            out = self._buf[:, i0:i0 + n].copy()
        elif not self._buf[:, int(first) - HISTORY:need].any():
            out = np.zeros((self.channels, n))                    # nothing but silence under the filter: nothing to filter
        else:
            out = self._interpolate(first, n, ratio)
        self._pos = first + n * ratio
        drop = int(np.floor(self._pos)) - HISTORY
        if drop > 0:
            self._buf = self._buf[:, drop:]
            self._pos -= drop
        return out

    def _interpolate(self, first: float, n: int, ratio: float) -> np.ndarray:
        global _READ_KERNEL
        if _READ_KERNEL is not None:
            buf = self._buf if self._buf.flags.c_contiguous else np.ascontiguousarray(self._buf)
            out = np.empty((self.channels, n))
            try:
                _READ_KERNEL(buf, _TABLE, float(first), float(ratio), int(n), out)
                return out
            except Exception as exc:                              # noqa: BLE001 — fall back to numpy, once and for all
                _READ_KERNEL = None
                fast.disable(f"resampler: {exc!r}")
        return self._interpolate_numpy(first, n, ratio)

    def _interpolate_numpy(self, first: float, n: int, ratio: float) -> np.ndarray:
        pos = first + np.arange(n) * ratio
        base = np.floor(pos)
        frac = pos - base
        phase = frac * PHASES
        p0 = np.minimum(phase.astype(np.int64), PHASES - 1)
        w = (phase - p0)[:, None]
        taps = _TABLE[p0] * (1.0 - w) + _TABLE[p0 + 1] * w        # (n, TAPS)
        idx = base.astype(np.int64)[:, None] + _OFFSETS[None, :]
        return np.einsum("cnt,nt->cn", self._buf[:, idx], taps)
