# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""A stateful, click-free multichannel biquad cascade.

PipeWire clears a biquad's history whenever its parameters change, which is a (small) click on every
slider move. Here a parameter change keeps the filter memory and crossfades the old cascade into the
new one over the next block, so dragging an EQ band is silent.

Audio is ``float64`` shaped ``(channels, frames)``; every channel runs the same cascade.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import sosfilt


def _zeros_zi(sections: int, channels: int) -> np.ndarray:
    return np.zeros((sections, channels, 2), dtype=np.float64)


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
        if sos.shape[0] == 0:
            return x, zi
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
