# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Biquad design identical to PipeWire's ``bq_*`` filter-chain builtins.

The formulas below are a line-for-line port of ``spa/plugins/audioconvert/biquad.c`` (originally
the WebKit/Chromium-OS ``Biquad``): the RBJ cookbook in its *Q form* — ``alpha = sin(w0) / (2 Q)``,
shelves use ``2 sqrt(A) alpha`` — with ``A = 10^(gain/40)``, and the same limit handling at 0 Hz,
Nyquist and Q = 0. PipeWire hands the filter ``freq * 2 / rate`` (frequency relative to Nyquist);
so does :func:`design`. Matching the maths exactly is what makes an EQ curve sound the same on
Windows as the Linux conf the same preset produces.

Differences that only help: coefficients stay float64 (PipeWire stores float32), and the filter
state is carried across parameter changes by :class:`~.eq.BiquadCascade` instead of being cleared.
"""
from __future__ import annotations

import math

import numpy as np

# EqBand.type (Sonar's names) -> PipeWire builtin label, mirrored from eq_types.PW_LABEL so this
# package never imports the Qt-free-but-heavier daemon modules.
KIND_FOR_BAND_TYPE = {
    "peakingEQ": "peaking",
    "lowPass": "lowpass",
    "highPass": "highpass",
    "lowShelving": "lowshelf",
    "highShelving": "highshelf",
    "notchFilter": "notch",
    "allPass": "allpass",
    "bandPassPeak0dB": "bandpass",
    "bandPassPeakQ": "bandpass",
}

KINDS = frozenset(KIND_FOR_BAND_TYPE.values()) | {"identity"}

IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0)


def _norm(b0: float, b1: float, b2: float, a0: float, a1: float, a2: float):
    inv = 1.0 / a0
    return (b0 * inv, b1 * inv, b2 * inv, a1 * inv, a2 * inv)


def _clip01(x: float) -> float:
    return max(0.0, min(x, 1.0))


def design(kind: str, freq_hz: float, q: float, gain_db: float, rate: float):
    """Coefficients ``(b0, b1, b2, a1, a2)`` (a0 normalised to 1) for one section."""
    if kind == "identity":
        return IDENTITY
    if kind not in KINDS:
        raise ValueError(f"unknown biquad kind {kind!r}")
    f = freq_hz * 2.0 / rate  # PipeWire passes the frequency relative to Nyquist

    if kind == "lowpass":
        f = _clip01(f)
        if f in (0.0, 1.0):
            return _norm(f, 0, 0, 1, 0, 0)
        if q <= 0:
            return _norm(0, 0, 0, 1, 0, 0)
        w0 = math.pi * f
        alpha = math.sin(w0) / (2 * q)
        k = math.cos(w0)
        return _norm((1 - k) / 2, 1 - k, (1 - k) / 2, 1 + alpha, -2 * k, 1 - alpha)

    if kind == "highpass":
        f = _clip01(f)
        if f in (0.0, 1.0):
            return _norm(1 - f, 0, 0, 1, 0, 0)
        if q <= 0:
            return _norm(0, 0, 0, 1, 0, 0)
        w0 = math.pi * f
        alpha = math.sin(w0) / (2 * q)
        k = math.cos(w0)
        return _norm((1 + k) / 2, -(1 + k), (1 + k) / 2, 1 + alpha, -2 * k, 1 - alpha)

    if kind == "bandpass":
        f = max(0.0, f)
        q = max(0.0, q)
        if f <= 0 or f >= 1:
            return _norm(0, 0, 0, 1, 0, 0)
        if q <= 0:
            return _norm(1, 0, 0, 1, 0, 0)
        w0 = math.pi * f
        alpha = math.sin(w0) / (2 * q)
        k = math.cos(w0)
        return _norm(alpha, 0, -alpha, 1 + alpha, -2 * k, 1 - alpha)

    if kind in ("lowshelf", "highshelf"):
        f = _clip01(f)
        a = 10.0 ** (gain_db / 40.0)
        low = kind == "lowshelf"
        if f == 1:
            return _norm(a * a if low else 1, 0, 0, 1, 0, 0)
        if f <= 0:
            return _norm(1 if low else a * a, 0, 0, 1, 0, 0)
        if q <= 0:
            return _norm(a, 0, 0, 1, 0, 0)
        w0 = math.pi * f
        alpha = math.sin(w0) / (2 * q)
        k = math.cos(w0)
        k2 = 2 * math.sqrt(a) * alpha
        ap1, am1 = a + 1, a - 1
        if low:
            return _norm(a * (ap1 - am1 * k + k2), 2 * a * (am1 - ap1 * k), a * (ap1 - am1 * k - k2),
                         ap1 + am1 * k + k2, -2 * (am1 + ap1 * k), ap1 + am1 * k - k2)
        return _norm(a * (ap1 + am1 * k + k2), -2 * a * (am1 + ap1 * k), a * (ap1 + am1 * k - k2),
                     ap1 - am1 * k + k2, 2 * (am1 - ap1 * k), ap1 - am1 * k - k2)

    # peaking / notch / allpass share the same guards
    f = _clip01(f)
    q = max(0.0, q)
    if f <= 0 or f >= 1:
        return _norm(1, 0, 0, 1, 0, 0)

    if kind == "peaking":
        a = 10.0 ** (gain_db / 40.0)
        if q <= 0:
            return _norm(a * a, 0, 0, 1, 0, 0)
        w0 = math.pi * f
        alpha = math.sin(w0) / (2 * q)
        k = math.cos(w0)
        return _norm(1 + alpha * a, -2 * k, 1 - alpha * a, 1 + alpha / a, -2 * k, 1 - alpha / a)

    if kind == "notch":
        if q <= 0:
            return _norm(0, 0, 0, 1, 0, 0)
        w0 = math.pi * f
        alpha = math.sin(w0) / (2 * q)
        k = math.cos(w0)
        return _norm(1, -2 * k, 1, 1 + alpha, -2 * k, 1 - alpha)

    # allpass
    if q <= 0:
        return _norm(-1, 0, 0, 1, 0, 0)
    w0 = math.pi * f
    alpha = math.sin(w0) / (2 * q)
    k = math.cos(w0)
    return _norm(1 - alpha, -2 * k, 1 + alpha, 1 + alpha, -2 * k, 1 - alpha)


def to_sos_row(coefficients) -> np.ndarray:
    """``(b0, b1, b2, a1, a2)`` -> one scipy SOS row ``[b0, b1, b2, 1, a1, a2]``."""
    b0, b1, b2, a1, a2 = coefficients
    return np.array([b0, b1, b2, 1.0, a1, a2], dtype=np.float64)


def gain_stage(multiplier: float) -> np.ndarray:
    """A pure gain as an SOS row (PipeWire's ``linear`` builtin: ``Out = In * Mult + 0``)."""
    return np.array([multiplier, 0.0, 0.0, 1.0, 0.0, 0.0], dtype=np.float64)


def response_db(sos: np.ndarray, freqs_hz, rate: float) -> np.ndarray:
    """Magnitude response of a cascade in dB at *freqs_hz* (for tests and the EQ curve)."""
    from scipy.signal import sosfreqz

    sos = np.atleast_2d(sos)
    if sos.size == 0:
        return np.zeros(len(freqs_hz))
    w = 2.0 * np.pi * np.asarray(freqs_hz, dtype=np.float64) / rate
    _, h = sosfreqz(sos, worN=w)
    return 20.0 * np.log10(np.maximum(np.abs(h), 1e-12))
