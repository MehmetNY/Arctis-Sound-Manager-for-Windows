# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""A stateful, click-free multichannel biquad cascade.

PipeWire clears a biquad's history whenever its parameters change, which is a (small) click on every
slider move. Here a parameter change keeps the filter memory and crossfades the old cascade into the
new one over the next block, so dragging an EQ band is silent.

Audio is ``float64`` shaped ``(channels, frames)``; every channel runs the same cascade.

The filtering itself is a few thousand multiply-adds per 10 ms block; ``scipy.signal.sosfilt`` spends nearly all of
its time checking and reshaping its arguments (about 200 microseconds per call, several calls per channel strip per
block), which was the largest single cost of the audio engine. With numba the same recursion runs as one compiled
call (same direct form II transposed arithmetic, results equal to within rounding); without it scipy does the work.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import sosfilt

from arctis_sound_manager.win.audio import fast


def _zeros_zi(sections: int, channels: int) -> np.ndarray:
    return np.zeros((sections, channels, 2), dtype=np.float64)


def _sosfilt_py(sos, zi, x):
    """Direct form II transposed, section after section, ``zi`` ``(sections, channels, 2)`` updated in place.
    ``sos`` rows are ``b0 b1 b2 a0 a1 a2`` with ``a0 == 1``."""
    sections = sos.shape[0]
    channels, frames = x.shape
    y = np.empty_like(x)
    for c in range(channels):
        for i in range(frames):
            v = x[c, i]
            for k in range(sections):
                out = sos[k, 0] * v + zi[k, c, 0]
                zi[k, c, 0] = sos[k, 1] * v - sos[k, 4] * out + zi[k, c, 1]
                zi[k, c, 1] = sos[k, 2] * v - sos[k, 5] * out
                v = out
            y[c, i] = v
    return y


_SOSFILT_KERNEL = fast.compile(_sosfilt_py)


class BiquadCascade:
    def __init__(self, channels: int, sos: np.ndarray | None = None):
        self.channels = channels
        self._sos = np.zeros((0, 6))
        self._zi = _zeros_zi(0, channels)
        self._fading: tuple[np.ndarray, np.ndarray] | None = None  # (old sos, old zi)
        if sos is not None:
            self.set_sos(sos, crossfade=False)

    @property
    def sections(self) -> int:
        return self._sos.shape[0]

    @property
    def sos(self) -> np.ndarray:
        return self._sos

    def reset(self) -> None:
        self._zi = _zeros_zi(self.sections, self.channels)
        self._fading = None

    def set_sos(self, sos: np.ndarray, crossfade: bool = True) -> bool:
        """Replace the filter. Returns False when nothing changed."""
        sos = np.atleast_2d(np.asarray(sos, dtype=np.float64)).reshape(-1, 6)
        a0 = sos[:, 3]
        if sos.size and not np.all(a0 == 1.0):
            if np.any(a0 == 0.0):
                raise ValueError("a biquad section needs a0 != 0")
            sos = sos / a0[:, None]                                  # the kernel (and scipy) want every section normalised
        sos = np.ascontiguousarray(sos)
        if sos.shape == self._sos.shape and np.array_equal(sos, self._sos):
            return False
        old_sos, old_zi = self._sos, self._zi
        if sos.shape[0] == old_sos.shape[0]:
            new_zi = old_zi.copy()          # same structure: the memory stays valid
        else:
            new_zi = _zeros_zi(sos.shape[0], self.channels)
        if crossfade and (old_sos.shape[0] or sos.shape[0]):
            self._fading = (old_sos, old_zi.copy())
        else:
            self._fading = None
        self._sos, self._zi = sos, new_zi
        return True

    def _run(self, sos: np.ndarray, zi: np.ndarray, x: np.ndarray):
        """Filter ``x``; returns ``(y, new state)``. The compiled kernel updates ``zi`` in place and returns it, which is
        safe: it is this cascade's own state, or the private copy kept for the crossfade."""
        if sos.shape[0] == 0:
            return x, zi
        kernel = _SOSFILT_KERNEL
        if kernel is not None and x.ndim == 2 and x.shape[0] == zi.shape[1] and zi.flags.c_contiguous:
            try:
                return kernel(sos, zi, x if x.flags.c_contiguous else np.ascontiguousarray(x)), zi
            except Exception as exc:                                # noqa: BLE001 — e.g. a compile problem: stay on scipy
                globals()["_SOSFILT_KERNEL"] = None
                fast.disable(f"biquad: {exc!r}")
        y, zf = sosfilt(sos, x, axis=-1, zi=zi)
        return y, zf

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        y, self._zi = self._run(self._sos, self._zi, x)
        if self._fading is not None:
            old_sos, old_zi = self._fading
            y_old, _ = self._run(old_sos, old_zi, x)
            n = x.shape[-1]
            ramp = (np.arange(n, dtype=np.float64) + 1.0) / n
            y = y_old + (y - y_old) * ramp
            self._fading = None
        return y if y is not x else x.copy()
