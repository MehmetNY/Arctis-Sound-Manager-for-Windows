# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Uniform-partition FFT convolution, and loading of HeSuVi HRIR files.

:class:`PartitionedConvolver` is a small matrix convolver: ``n_in`` input channels, ``n_out`` outputs,
one impulse response per (input, output) pair, ``out[o] = sum_i in[i] * ir[i, o]``. It uses
overlap-save with a frequency-domain delay line (UPOLS): every block costs one forward FFT per input,
one inverse FFT per output, and a single einsum over the partitions — a few milliseconds for the
longest bundled HRIR (16802 frames) at a 10 ms block. It adds no latency beyond the block itself.
"""
from __future__ import annotations

from math import gcd
from pathlib import Path

import numpy as np

# HeSuVi 14-channel layout: FL_L FL_R SL_L SL_R RL_L RL_R FC_L FR_R FR_L SR_R SR_L RR_R RR_L FC_R
HESUVI_14 = ("FL_L", "FL_R", "SL_L", "SL_R", "RL_L", "RL_R", "FC_L",
             "FR_R", "FR_L", "SR_R", "SR_L", "RR_R", "RR_L", "FC_R")


class PartitionedConvolver:
    def __init__(self, irs: np.ndarray, block: int):
        """``irs`` has shape ``(n_in, n_out, length)``."""
        irs = np.asarray(irs, dtype=np.float64)
        if irs.ndim != 3:
            raise ValueError("irs must have shape (n_in, n_out, length)")
        self.n_in, self.n_out, length = irs.shape
        self.block = int(block)
        self.length = max(1, length)
        self.partitions = max(1, -(-length // self.block))
        padded = np.zeros((self.n_in, self.n_out, self.partitions * self.block))
        padded[..., :length] = irs
        parts = padded.reshape(self.n_in, self.n_out, self.partitions, self.block)
        # H[i, o, p, k]: spectrum of partition p, zero-padded to 2 * block
        self._h = np.fft.rfft(parts, n=2 * self.block, axis=-1)
        self._subsets: dict[bytes, np.ndarray] = {}
        self.reset()

    def reset(self) -> None:
        bins = self.block + 1
        self._fdl = np.zeros((self.n_in, self.partitions, bins), dtype=np.complex128)
        self._prev = np.zeros((self.n_in, self.block))
        self._pos = 0
        self._quiet = np.full(self.n_in, self.partitions + 2)    # blocks since each input was last non-silent

    def _subset(self, live: np.ndarray) -> np.ndarray:
        """The spectra of the inputs in *live* (cached: the set rarely changes, and copying them is not free)."""
        key = live.tobytes()
        cached = self._subsets.get(key)
        if cached is None:
            if len(self._subsets) >= 16:
                self._subsets.pop(next(iter(self._subsets)))
            cached = self._subsets[key] = np.ascontiguousarray(self._h[live])
        return cached

    def process(self, x: np.ndarray) -> np.ndarray:
        """``x``: ``(n_in, block)`` -> ``(n_out, block)``.

        Inputs that have been digitally silent for longer than the impulse response lasts are left out
        of the work entirely (a stereo programme on a 7.1 convolver costs a quarter), which changes
        nothing: their contribution is exactly zero.
        """
        x = np.asarray(x, dtype=np.float64)
        if x.shape != (self.n_in, self.block):
            raise ValueError(f"expected {(self.n_in, self.block)}, got {x.shape}")
        nonzero = x.any(axis=1)
        self._quiet = np.where(nonzero, 0, np.minimum(self._quiet + 1, self.partitions + 2))
        # a slot is non-zero if this block or the one before it (the overlap) was; skip the FFT otherwise
        needs_fft = self._quiet <= 1
        live = self._quiet <= self.partitions                   # still has non-zero slots in its delay line

        window = np.concatenate([self._prev, x], axis=1)
        self._prev = x.copy()
        self._pos = (self._pos - 1) % self.partitions            # newest block lives at self._pos
        if needs_fft.all():
            self._fdl[:, self._pos, :] = np.fft.rfft(window, axis=-1)
        else:
            self._fdl[~needs_fft, self._pos, :] = 0.0
            if needs_fft.any():
                self._fdl[needs_fft, self._pos, :] = np.fft.rfft(window[needs_fft], axis=-1)

        if not live.any():
            return np.zeros((self.n_out, self.block))
        order = (self._pos + np.arange(self.partitions)) % self.partitions
        if live.all():
            ordered = self._fdl if self.partitions == 1 else self._fdl[:, order, :]
            spectrum = np.einsum("iopk,ipk->ok", self._h, ordered)
        else:
            idx = np.flatnonzero(live)
            ordered = self._fdl[idx[:, None], order[None, :], :]
            spectrum = np.einsum("iopk,ipk->ok", self._subset(live), ordered)
        return np.fft.irfft(spectrum, n=2 * self.block, axis=-1)[:, self.block:]


# ── HRIR files ───────────────────────────────────────────────────────────────

def expand_hrir(data: np.ndarray) -> np.ndarray:
    """Any HeSuVi HRIR (7 or 14 channels, ``(frames, channels)``) as ``(14, frames)`` in HeSuVi order.

    A 7-channel file stores the left-hand speakers only (FL_L FL_R SL_L SL_R RL_L RL_R FC_L); the
    right-hand ones are their mirror images, exactly as HeSuVi expands it. A mono/stereo file is the
    front pair alone.
    """
    data = np.asarray(data, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    frames, channels = data.shape
    out = np.zeros((14, frames))
    if channels >= 14:
        out[:] = data[:, :14].T
    elif channels >= 7:
        left = data[:, :7].T                      # FL_L FL_R SL_L SL_R RL_L RL_R FC_L
        out[:7] = left
        out[7:13] = left[:6]                      # FR_R=FL_L FR_L=FL_R SR_R=SL_L SR_L=SL_R RR_R=RL_L RR_L=RL_R
        out[13] = left[6]                         # FC_R = FC_L
    elif channels == 2:
        out[0], out[1] = data[:, 0], data[:, 1]   # FL -> both ears; FR is the mirror
        out[7], out[8] = data[:, 0], data[:, 1]
    else:
        out[0] = out[1] = out[6] = out[13] = data[:, 0]
        out[7] = out[8] = data[:, 0]
    return out


def resample_ir(ir: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample ``(channels, frames)`` impulse responses (polyphase, so the length scales exactly)."""
    if src_rate == dst_rate:
        return ir
    from scipy.signal import resample_poly

    g = gcd(int(src_rate), int(dst_rate))
    return resample_poly(ir, int(dst_rate) // g, int(src_rate) // g, axis=-1)


def load_hrir(path: str | Path, rate: int = 48000) -> np.ndarray:
    """Read a HeSuVi WAV and return ``(14, frames)`` float64 at *rate*."""
    import warnings

    from scipy.io import wavfile

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", wavfile.WavFileWarning)   # HeSuVi files carry a LIST chunk
        src_rate, data = wavfile.read(str(path))
    if data.dtype.kind in "iu":
        data = data.astype(np.float64) / float(np.iinfo(data.dtype).max + 1) if data.dtype.kind == "i" else \
            (data.astype(np.float64) - 128.0) / 128.0
    return resample_ir(expand_hrir(data), src_rate, rate)


def hrir_matrix(hrir14: np.ndarray, lfe_as_center: bool = True) -> np.ndarray:
    """The 8 -> 2 filter matrix of ASM's HeSuVi chain, ``(8, 2, frames)``.

    Inputs are PipeWire's 7.1 order ``FL FR FC LFE RL RR SL SR``; outputs are ``(left ear, right ear)``.
    The wiring is exactly ``_HESUVI_CONVOLVERS`` / ``_HESUVI_COPY_CONV_LINKS`` in ``sonar_to_pipewire``:
    each speaker uses its pair of HRIR channels, and the LFE is treated as the centre.
    """
    h = hrir14
    frames = h.shape[1]
    m = np.zeros((8, 2, frames))
    #            left ear   right ear
    m[0] = h[[0, 1]]        # FL
    m[1] = h[[8, 7]]        # FR  (FR_L, FR_R)
    m[2] = h[[6, 13]]       # FC
    m[3] = h[[6, 13]] if lfe_as_center else 0.0   # LFE
    m[4] = h[[4, 5]]        # RL
    m[5] = h[[12, 11]]      # RR  (RR_L, RR_R)
    m[6] = h[[2, 3]]        # SL
    m[7] = h[[10, 9]]       # SR  (SR_L, SR_R)
    return m
