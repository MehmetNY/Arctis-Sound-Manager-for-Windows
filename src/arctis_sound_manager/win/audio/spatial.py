# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""HeSuVi 7.1 virtual surround, the same chain ``generate_hesuvi_conf`` builds for PipeWire.

    8ch in -> Immersion (broadband high shelf, 0..12 dB) -> HRIR convolution (8 -> left/right ear)
           -> Distance (plate reverb, dry/wet 0..1, only when > 0)
           -> limiter (-1 dB, 0.1 s release; its input gain cancels the channel's Volume Boost)
           -> Volume Boost make-up shelf (only when Boost > 0.01 dB)  -> 2ch out

Two small liberties, both exact in a linear system: the Immersion shelf is applied once per ear after
the convolution instead of once per input before it (linear filters commute, and it is a quarter of
the work), and changing the HRIR crossfades from the old convolver to the new one instead of cutting.

Also here: the ITU stereo downmix used when Spatial Audio is off but the destination is stereo.
"""
from __future__ import annotations

import numpy as np

from arctis_sound_manager.win.audio import biquad
from arctis_sound_manager.win.audio.convolver import PartitionedConvolver, hrir_matrix
from arctis_sound_manager.win.audio.dynamics import Limiter
from arctis_sound_manager.win.audio.eq import BiquadCascade
from arctis_sound_manager.win.audio.reverb import PlateReverb

CHANNELS_71 = ("FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR")

# ITU-R BS.775 downmix, as in sonar_to_pipewire._DOWNMIX_GAINS (index order FL FR FC LFE RL RR SL SR)
_DOWNMIX = np.zeros((2, 8))
_DOWNMIX[0, [0, 2, 6, 4]] = (1.0, 0.7071, 0.7071, 0.7071)    # L = FL + .7071 (FC + SL + RL)
_DOWNMIX[1, [1, 2, 7, 5]] = (1.0, 0.7071, 0.7071, 0.7071)    # R = FR + .7071 (FC + SR + RR)


def downmix_to_stereo(x8: np.ndarray) -> np.ndarray:
    """Fold 7.1 into stereo; the LFE is left out. A stereo programme (only FL/FR filled) passes unchanged."""
    return _DOWNMIX @ np.asarray(x8, dtype=np.float64)


class _Bank:
    """An HRIR as a convolver, with inputs that share an impulse response summed before the transform.

    The 8 -> 2 matrix of the HeSuVi chain feeds the centre and the LFE through the same pair of
    responses, so one of the eight convolutions is redundant: ``conv(FC) + conv(LFE) = conv(FC + LFE)``.
    """

    def __init__(self, hrir14: np.ndarray, block: int):
        m = hrir_matrix(hrir14)
        self.keep: list[int] = []
        self.fold: list[tuple[int, int]] = []          # (input to add, position in keep to add it to)
        for j in range(m.shape[0]):
            for position, i in enumerate(self.keep):
                if np.array_equal(m[j], m[i]):
                    self.fold.append((j, position))
                    break
            else:
                self.keep.append(j)
        self.conv = PartitionedConvolver(m[self.keep], block)

    def process(self, x8: np.ndarray) -> np.ndarray:
        x = x8[self.keep]
        for src, position in self.fold:
            x[position] += x8[src]
        return self.conv.process(x)


class VirtualSurround:
    def __init__(self, hrir14: np.ndarray, rate: int = 48000, block: int = 480,
                 immersion_db: float = 6.0, distance_wet: float = 0.5, boost_db: float = 0.0):
        self.rate = rate
        self.block = block
        self._conv = _Bank(hrir14, block)
        self._old_conv: _Bank | None = None
        self._fade_total = 0
        self._fade_left = 0
        self._shelf = BiquadCascade(2)
        self._plate = PlateReverb(2.5, 0.5, 0.5, instances=2)    # row 0 = left ear's reverb, row 1 = right ear's
        self._limiter = Limiter(rate, 2, limit_db=-1.0, release_s=0.1)
        self._boost = BiquadCascade(2)
        self.immersion_db = self.distance_wet = self.boost_db = 0.0
        self.set_params(immersion_db, distance_wet, boost_db, crossfade=False)

    @property
    def latency_frames(self) -> int:
        """The limiter's lookahead; the convolution itself adds none beyond the block."""
        return self._limiter.delay

    def set_params(self, immersion_db: float, distance_wet: float, boost_db: float = 0.0,
                   crossfade: bool = True) -> None:
        # the Linux conf writes these with one / two / one decimals; keep the same resolution
        self.immersion_db = round(max(0.0, min(12.0, immersion_db)), 1)
        wet = round(max(0.0, min(1.0, distance_wet)), 2)
        self.boost_db = round(max(-12.0, min(12.0, boost_db)), 1)

        shelf = np.zeros((0, 6)) if self.immersion_db == 0.0 else \
            biquad.to_sos_row(biquad.design("highshelf", 10.0, 0.7071, self.immersion_db, self.rate))[None, :]
        self._shelf.set_sos(shelf, crossfade)

        if wet > 0.0 and wet != self.distance_wet:
            if self.distance_wet == 0.0:           # coming back from bypass: do not play an old tail
                self._plate.reset()
            self._plate.set_params(2.5, 0.5, wet)
        self.distance_wet = wet

        makeup = self.boost_db > 0.01
        self._limiter.set_params(-1.0, 0.1, -self.boost_db if makeup else 0.0)
        boost = biquad.to_sos_row(biquad.design("highshelf", 10.0, 0.7071, self.boost_db, self.rate))[None, :] \
            if makeup else np.zeros((0, 6))
        self._boost.set_sos(boost, crossfade)

    def set_hrir(self, hrir14: np.ndarray, fade_blocks: int = 4) -> None:
        """Swap the impulse responses, crossfading over *fade_blocks* blocks."""
        self._old_conv = self._conv
        self._conv = _Bank(hrir14, self.block)
        self._fade_total = self._fade_left = max(1, int(fade_blocks))

    def reset(self) -> None:
        self._conv.conv.reset()
        self._old_conv = None
        self._fade_left = 0
        self._shelf.reset()
        self._plate.reset()
        self._limiter.reset()
        self._boost.reset()

    def process(self, x8: np.ndarray) -> np.ndarray:
        """``(8, block)`` in the order FL FR FC LFE RL RR SL SR -> ``(2, block)``."""
        ears = self._conv.process(x8)
        if self._old_conv is not None:
            old = self._old_conv.process(x8)
            w = (self._fade_total - self._fade_left + 1) / self._fade_total
            ears = old + (ears - old) * w
            self._fade_left -= 1
            if self._fade_left <= 0:
                self._old_conv = None
        ears = self._shelf.process(ears)
        if self.distance_wet > 0.0:
            left, right = self._plate.process(ears)
            ears = np.stack([left[0], right[1]])           # plate_L feeds the left output, plate_R the right
        ears = self._limiter.process(ears)
        return self._boost.process(ears)
