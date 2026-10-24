# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The swh-plugins plate reverb (``plate_1423``) — what ASM's "Distance" slider drives.

A port of the plugin ("a physical model of a steel plate", after Josep Comajuncosas' gong model):
eight linear waveguides, each two delay lines with a one-pole filter and a first-order all-pass on
the way out, cross-coupled through four junctions.

The C code runs sample by sample, but every feedback path goes through a delay line of at least
``min_delay`` samples (415 at ASM's settings). Within any stretch shorter than that, the lines' outputs
depend only on what was written *before* the stretch, so a block is computed in a few vectorised
passes and the result is the same as the sample loop, to float rounding:

* all sixteen delay-line reads are one gather, all sixteen writes one scatter;
* the one-pole and the all-pass of a waveguide are folded into one second-order ``lfilter``;
* several independent reverbs (ASM runs one per ear) share every pass: ``PlateReverb(instances=2)``.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import lfilter

from arctis_sound_manager.win.audio import fast

LP_INNER = 0.96
LP_OUTER = 0.983

# (delay-line length, damping filter base cutoff, all-pass coefficient parameter) per waveguide
_SIZES = (2389, 4742, 4623, 2142, 5597, 3692, 5611, 3703)
_DA = (0.04, 0.17, 0.52, 0.48, 0.32, 0.89, 0.28, 0.29)
_FC = (LP_INNER,) * 4 + (LP_OUTER,) * 4

# (junction feeding line 0, junction feeding line 1) per waveguide; see RUN_WG in the plugin
_JUNCTION_NAMES = ("alpha", "beta", "gamma", "delta", "epsilon")
_JUNCTIONS = (("beta", "alpha"), ("gamma", "alpha"), ("delta", "alpha"), ("epsilon", "alpha"),
              ("beta", "gamma"), ("gamma", "delta"), ("delta", "epsilon"), ("epsilon", "beta"))
# per line (row 2w / 2w+1): which junction feeds it, and its partner row (the other line of the guide)
_ROW_JUNCTION = np.array([_JUNCTION_NAMES.index(j) for pair in _JUNCTIONS for j in pair])
_ROW_PARTNER = np.arange(16) ^ 1

_TWO_THIRDS = 0.666666666


def _plate_kernel(x, ring, t, delays, b, a, zi, prev, wet, row_junction, left, right):
    """The sample loop itself, for numba: exactly the C code's order of operations, all instances.

    ``ring`` is ``(instances, 16, length)``, ``zi`` is ``(8, instances * 2, 2)`` (transposed direct form II
    states of the combined one-pole/all-pass per waveguide), ``prev`` is ``(instances, 16)`` — the previous
    sample's outputs, left holding the last sample's on return.
    """
    instances, m = x.shape
    mask = ring.shape[2] - 1                  # the ring length is a power of two: ``& mask`` replaces ``%``
    out = np.empty(16)
    junction = np.empty(5)
    for i in range(instances):
        for s in range(m):
            tt = (t + s) & mask
            xs = x[i, s]
            for w in range(8):
                for k in range(2):
                    r = 2 * w + k
                    v = ring[i, r, (tt - delays[r]) & mask]
                    z0 = zi[w, 2 * i + k, 0]
                    y = b[w, 0] * v + z0
                    zi[w, 2 * i + k, 0] = b[w, 1] * v - a[w, 1] * y + zi[w, 2 * i + k, 1]
                    zi[w, 2 * i + k, 1] = b[w, 2] * v - a[w, 2] * y
                    out[r] = y
            junction[0] = (prev[i, 0] + prev[i, 2] + prev[i, 4] + prev[i, 6]) * 0.5 + xs
            junction[1] = (prev[i, 1] + prev[i, 9] + prev[i, 14]) * 0.666666666
            junction[2] = (prev[i, 3] + prev[i, 8] + prev[i, 11]) * 0.666666666
            junction[3] = (prev[i, 5] + prev[i, 10] + prev[i, 13]) * 0.666666666
            junction[4] = (prev[i, 7] + prev[i, 12] + prev[i, 15]) * 0.666666666
            for r in range(16):
                ring[i, r, tt] = junction[row_junction[r]] - prev[i, r ^ 1]
            dry = xs * (1.0 - wet)
            left[i, s] = junction[1] * wet + dry
            right[i, s] = junction[2] * wet + dry
            for r in range(16):
                prev[i, r] = out[r]


_KERNEL = fast.compile(_plate_kernel)


class PlateReverb:
    """Mono in, stereo out. ``wet`` 0 is dry, 1 is the reverb alone (the plugin's "Dry/wet mix").

    ``process`` takes a 1-D block and returns ``(left, right)``; with ``instances > 1`` it takes
    ``(instances, n)`` and returns two ``(instances, n)`` arrays — each row an independent reverb.
    """

    def __init__(self, time: float = 2.5, damping: float = 0.5, wet: float = 0.5, instances: int = 1):
        self.instances = int(instances)
        self._ring_len = 1 << (max(_SIZES) + 1024 - 1).bit_length()       # a power of two (see _plate_kernel)
        self.set_params(time, damping, wet)
        self.reset()

    def set_params(self, time: float, damping: float, wet: float) -> None:
        self.time = float(time)
        self.damping = float(damping)
        self.wet = max(0.0, min(1.0, float(wet)))
        scale = (self.time * 0.117647) ** 1.34
        lpscale = 1.0 - self.damping * 0.93
        self._delay = [max(1, min(size, int(size * scale))) for size in _SIZES]
        self._delay_rows = np.repeat(self._delay, 2)
        self._fc = [fc * lpscale for fc in _FC]
        self._ap = [(1.0 - da) / (1.0 + da) for da in _DA]
        self.min_delay = min(self._delay)
        # one-pole  fc / (1 + (1-fc) z^-1)  followed by all-pass  (-a + z^-1) / (1 - a z^-1)
        self._b, self._a = [], []
        for fc, a in zip(self._fc, self._ap):
            self._b.append(np.array([-a * fc, fc, 0.0]))
            self._a.append(np.array([1.0, (1.0 - fc) - a, -a * (1.0 - fc)]))
        self._b_arr, self._a_arr = np.array(self._b), np.array(self._a)
        self._delay_arr = np.array(self._delay_rows, dtype=np.int64)

    def reset(self) -> None:
        i = self.instances
        self._ring = np.zeros((i, 16, self._ring_len))
        self._t = 0
        self._out_prev = np.zeros((i, 16))
        self._zi = np.zeros((8, i * 2, 2))

    def process(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        x = np.asarray(x, dtype=np.float64)
        single = x.ndim == 1
        x = x.reshape(1, -1) if single else x
        if x.shape[0] != self.instances:
            raise ValueError(f"expected {self.instances} rows, got {x.shape[0]}")
        n = x.shape[-1]
        left, right = np.empty((self.instances, n)), np.empty((self.instances, n))
        if _KERNEL is not None:
            try:
                _KERNEL(np.ascontiguousarray(x), self._ring, self._t, self._delay_arr, self._b_arr, self._a_arr,
                        self._zi, self._out_prev, self.wet, _ROW_JUNCTION, left, right)
                self._t = (self._t + n) % self._ring_len
                return (left[0], right[0]) if single else (left, right)
            except Exception as exc:                          # noqa: BLE001 — e.g. a compile problem: stay on numpy
                globals()["_KERNEL"] = None
                fast.disable(f"plate reverb: {exc!r}")
        step = self.min_delay
        for lo in range(0, n, step):
            hi = min(n, lo + step)
            left[:, lo:hi], right[:, lo:hi] = self._run(x[:, lo:hi])
        return (left[0], right[0]) if single else (left, right)

    def _run(self, x: np.ndarray):
        inst, m = x.shape
        idx = np.arange(m)
        # what was written `delay` steps ago, for all 16 lines at once
        read = (self._t - self._delay_rows[:, None] + idx[None, :]) % self._ring_len     # (16, m)
        raw = self._ring[:, np.arange(16)[:, None], read]                                # (inst, 16, m)
        out = np.empty_like(raw)
        for w in range(8):
            block = raw[:, 2 * w:2 * w + 2, :].reshape(inst * 2, m)
            y, self._zi[w] = lfilter(self._b[w], self._a[w], block, axis=-1, zi=self._zi[w])
            out[:, 2 * w:2 * w + 2, :] = y.reshape(inst, 2, m)

        # every junction at step n sees the outputs of step n - 1
        prev = np.concatenate([self._out_prev[:, :, None], out[:, :, :-1]], axis=2)
        alpha = (prev[:, 0] + prev[:, 2] + prev[:, 4] + prev[:, 6]) * 0.5 + x
        beta = (prev[:, 1] + prev[:, 9] + prev[:, 14]) * _TWO_THIRDS
        gamma = (prev[:, 3] + prev[:, 8] + prev[:, 11]) * _TWO_THIRDS
        delta = (prev[:, 5] + prev[:, 10] + prev[:, 13]) * _TWO_THIRDS
        epsilon = (prev[:, 7] + prev[:, 12] + prev[:, 15]) * _TWO_THIRDS
        junctions = np.stack([alpha, beta, gamma, delta, epsilon], axis=1)               # (inst, 5, m)

        write = (self._t + idx) % self._ring_len
        self._ring[:, :, write] = junctions[:, _ROW_JUNCTION, :] - prev[:, _ROW_PARTNER, :]

        self._out_prev = out[:, :, -1].copy()
        self._t = (self._t + m) % self._ring_len
        dry = x * (1.0 - self.wet)
        return beta * self.wet + dry, gamma * self.wet + dry
