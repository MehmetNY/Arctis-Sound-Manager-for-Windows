# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The dynamics processors ASM's Linux chains get from swh-plugins, ported to numpy.

* :class:`Compressor` — "SC4 mono" (``sc4m_1916``): Smart Volume and the microphone compressor.
* :class:`Gate` — "Gate" (``gate_1410``): the microphone noise gate.
* :class:`Limiter` — "Fast Lookahead limiter" (``fast_lookahead_limiter_1913``): the output limiter
  of the HeSuVi virtual-surround chain.

They follow the plugins' algorithms (the sources are Steve Harris' GPL code): same static curves,
same attack/release maths, same lookahead logic. Two deliberate differences, both improvements that
the sample-by-sample C code makes impractical in Python:

* the compressor and gate run their *detectors* every ``DETECTOR_STEP`` samples (0.17 ms at 48 kHz;
  the C code already updates its RMS window every 4) and interpolate the gain between steps — far
  faster than any attack time they implement, so the behaviour is indistinguishable, and the
  recursion is 8x shorter;
* the compressor's detector is *linked* across channels, so a 7.1 or stereo programme is attenuated
  as a whole instead of each channel by itself (which would shift the stereo image).

Audio is ``float64`` shaped ``(channels, frames)``; block lengths must be a multiple of
``DETECTOR_STEP`` (the engine always uses 480).
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter

from arctis_sound_manager.win.audio import fast

DETECTOR_STEP = 8

_TINY = 1e-30


def db2lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def lin2db(lin: np.ndarray | float):
    return 20.0 * np.log10(np.maximum(lin, 1e-10))


def _db_co(db: float) -> float:
    """swh's ``DB_CO``: dB to gain, with everything at or below -90 dB meaning silence."""
    return 10.0 ** (db * 0.05) if db > -90.0 else 0.0


# ── compressor ───────────────────────────────────────────────────────────────

class Compressor:
    """swh SC4 mono, with a channel-linked detector.

    ``rms_peak`` blends the RMS (0) and peak (1) envelope followers. ``knee_db`` is the knee *radius*:
    the gain curve bends over ``threshold ± knee``.
    """

    A_TBL = 256  # the plugin quantises attack/release to a 256-entry table (seconds * 255 / 256)

    def __init__(self, rate: float = 48000.0, channels: int = 2):
        self.rate = float(rate)
        self.channels = channels
        self.set_params(rms_peak=0.0, attack_ms=10.0, release_ms=200.0, threshold_db=-18.0,
                        ratio=4.0, knee_db=6.0, makeup_db=0.0)
        self.reset()

    # the table the C code indexes: as[i] = exp(-1 / (rate * i / 256)), as[0] = 1
    def _table_coeff(self, ms: float) -> float:
        i = int(round(ms * 0.001 * (self.A_TBL - 1)))
        i = max(0, min(self.A_TBL - 1, i))
        return 1.0 if i == 0 else math.exp(-1.0 / (self.rate * i / self.A_TBL))

    def set_params(self, rms_peak, attack_ms, release_ms, threshold_db, ratio, knee_db, makeup_db) -> None:
        self.rms_peak = max(0.0, min(1.0, rms_peak))
        ga = 0.0 if attack_ms < 2.0 else self._table_coeff(attack_ms)
        gr = self._table_coeff(release_ms)
        self._ga, self._gr = ga, gr
        d = DETECTOR_STEP
        self._ga_d, self._gr_d = ga ** d, gr ** d       # the same time constants, per detector step
        self.threshold_db = threshold_db
        self.knee_db = max(knee_db, 1e-6)
        self.ratio = max(ratio, 1.0)
        self._rs = (self.ratio - 1.0) / self.ratio
        self._makeup = db2lin(makeup_db)
        self._knee_min = db2lin(threshold_db - self.knee_db)
        self._knee_max = db2lin(threshold_db + self.knee_db)
        ef_a = ga * 0.25                                  # the plugin's per-sample gain smoother
        self._ef_a = ef_a

    def reset(self) -> None:
        self._env_rms = 0.0
        self._env_peak = 0.0
        self._rms_hist = np.zeros(self._rms_window() - 1)
        self._gain_zi = np.array([1.0 * self._ef_a])      # lfilter state for gain = 1
        self._last_gain = 1.0

    def _rms_window(self) -> int:
        return 256 // DETECTOR_STEP                        # 256 samples, like the C code's 64 x 4

    @property
    def active(self) -> bool:
        """False when the settings are a plain passthrough (the engine then skips the call)."""
        return self.ratio <= 1.0 and abs(self._makeup - 1.0) < 1e-9

    def _gain_target(self, env: np.ndarray) -> np.ndarray:
        """The static curve: gain for an envelope level (linear in, linear out)."""
        db = lin2db(env)
        thr, knee, rs = self.threshold_db, self.knee_db, self._rs
        above = np.power(10.0, (thr - db) * rs / 20.0)
        x = -(thr - knee - db) / knee
        soft = np.power(10.0, (-knee * rs * x * x * 0.25) / 20.0)
        return np.where(env <= self._knee_min, 1.0, np.where(env < self._knee_max, soft, above))

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        n = x.shape[-1]
        d = DETECTOR_STEP
        if n % d:
            raise ValueError(f"block length must be a multiple of {d}, got {n}")
        steps = n // d
        blocks = x.reshape(x.shape[0], steps, d)

        # linked detector: the loudest channel drives all of them
        msq = (blocks * blocks).mean(axis=2).max(axis=0)                   # (steps,)
        window = self._rms_window()
        padded = np.concatenate([self._rms_hist, msq])
        sums = np.convolve(padded, np.ones(window), mode="valid")           # moving sum over `window` steps
        self._rms_hist = padded[-(window - 1):] if window > 1 else padded[:0]
        amp = np.sqrt(np.maximum(sums, 0.0) / window)
        peak_in = np.abs(blocks).max(axis=(0, 2)) if self.rms_peak > 0.0 else None

        env = np.empty(steps)
        e_rms, e_peak = self._env_rms, self._env_peak
        ga, gr = self._ga_d, self._gr_d
        mix = self.rms_peak
        for i in range(steps):
            a = amp[i]
            e_rms = e_rms * ga + a * (1.0 - ga) if a > e_rms else e_rms * gr + a * (1.0 - gr)
            if e_rms < 1e-20:
                e_rms = 0.0
            if peak_in is not None:
                p = peak_in[i]
                e_peak = e_peak * ga + p * (1.0 - ga) if p > e_peak else e_peak * gr + p * (1.0 - gr)
                if e_peak < 1e-20:
                    e_peak = 0.0
                env[i] = e_rms + mix * (e_peak - e_rms)
            else:
                env[i] = e_rms
        self._env_rms, self._env_peak = e_rms, e_peak

        gain_t = self._gain_target(env)
        per_sample = np.repeat(gain_t, d)
        ef_a = self._ef_a
        gain, self._gain_zi = lfilter([1.0 - ef_a], [1.0, -ef_a], per_sample, zi=self._gain_zi)
        self._last_gain = float(gain[-1])
        return x * (gain * self._makeup)

    def gain_reduction_db(self) -> float:
        """The last block's gain reduction in dB (<= 0), for meters and diagnostics."""
        return float(lin2db(self._last_gain))


# ── gate ─────────────────────────────────────────────────────────────────────

class Gate:
    """swh Gate (Drawmer DS-201 style), mono, with optional lookahead.

    The key signal is lightly filtered (low shelf -40 dB at 33.6 Hz, high shelf -50 dB at 23.5 kHz —
    the plugin's defaults) before the level detector. ``lookahead_ms`` delays the audio so the gate
    can open *before* a word starts instead of chopping the first attack milliseconds (the C plugin
    has none; 0 reproduces it exactly).
    """

    _CLOSED, _OPENING, _OPEN, _CLOSING = 1, 2, 3, 4
    ENV_TR = 0.0001

    def __init__(self, rate: float = 48000.0, threshold_db: float = -40.0, attack_ms: float = 5.0,
                 hold_ms: float = 50.0, decay_ms: float = 100.0, range_db: float = -90.0,
                 lookahead_ms: float = 5.0):
        from arctis_sound_manager.win.audio import biquad
        from arctis_sound_manager.win.audio.eq import BiquadCascade

        self.rate = float(rate)
        self.set_params(threshold_db, attack_ms, hold_ms, decay_ms, range_db)
        self._look = int(round(lookahead_ms * 0.001 * rate))
        # swh's ls/hs_set_params take a shelf *slope* S; the equivalent Q is 1 / sqrt((A + 1/A)(1/S - 1) + 2)
        def q_for(slope: float, gain_db: float) -> float:
            a = 10.0 ** (gain_db / 40.0)
            return 1.0 / math.sqrt((a + 1.0 / a) * (1.0 / slope - 1.0) + 2.0)

        key = np.array([
            biquad.to_sos_row(biquad.design("lowshelf", 0.0007 * rate, q_for(0.6, -40.0), -40.0, rate)),
            biquad.to_sos_row(biquad.design("highshelf", 0.49 * rate, q_for(0.6, -50.0), -50.0, rate))])
        self._key = BiquadCascade(1, key)
        self.reset()

    def set_params(self, threshold_db, attack_ms, hold_ms, decay_ms, range_db) -> None:
        self._t_level = _db_co(threshold_db)
        self._cut = _db_co(range_db)
        self._a_rate = 1000.0 / (max(attack_ms, 1e-3) * self.rate)
        self._d_rate = 1000.0 / (max(decay_ms, 1e-3) * self.rate)
        self._hold = int(round(hold_ms * self.rate * 0.001))

    def reset(self) -> None:
        self._env = 0.0
        self._gate = 0.0
        self._state = self._CLOSED
        self._hold_count = 0
        self._delay = np.zeros(self._look)
        self._prev_gate = 0.0
        self._key.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        mono = x[0] if x.ndim == 2 else x
        n = mono.shape[-1]
        d = DETECTOR_STEP
        if n % d:
            raise ValueError(f"block length must be a multiple of {d}, got {n}")
        steps = n // d

        keyed = np.abs(self._key.process(mono[None, :])[0]).reshape(steps, d)
        step_max, step_mean = keyed.max(axis=1), keyed.mean(axis=1)
        decay = (1.0 - self.ENV_TR) ** d

        gate_at_end = np.empty(steps)
        env, gate, state, hold = self._env, self._gate, self._state, self._hold_count
        t, a_step, d_step = self._t_level, self._a_rate * d, self._d_rate * d
        for i in range(steps):
            m = step_max[i]
            env = m if m >= env else max(m, env * decay + step_mean[i] * (1.0 - decay))
            if state == self._CLOSED:
                if env >= t:
                    state = self._OPENING
            elif state == self._OPENING:
                gate += a_step
                if gate >= 1.0:
                    gate, state, hold = 1.0, self._OPEN, self._hold
            elif state == self._OPEN:
                if hold <= 0:
                    if env < t:
                        state = self._CLOSING
                else:
                    hold -= d
            elif state == self._CLOSING:
                gate -= d_step
                if env >= t:
                    state = self._OPENING
                elif gate <= 0.0:
                    gate, state = 0.0, self._CLOSED
            gate_at_end[i] = gate
        self._env, self._gate, self._state, self._hold_count = env, gate, state, hold

        # interpolate the gate between detector steps; the previous block's last value is the start
        prev = self._prev_gate
        knots_x = np.concatenate([[-1], np.arange(steps)]) * d + d - 1
        knots_y = np.concatenate([[prev], gate_at_end])
        g = np.interp(np.arange(n), knots_x, knots_y)
        self._prev_gate = float(gate_at_end[-1])

        audio = mono
        if self._look:
            joined = np.concatenate([self._delay, mono])
            audio = joined[:n]
            self._delay = joined[n:]
        out = audio * (self._cut * (1.0 - g) + g)
        return out[None, :] if x.ndim == 2 else out

    @property
    def is_open(self) -> bool:
        return self._state != self._CLOSED

    @property
    def lookahead_frames(self) -> int:
        return self._look


# ── limiter ──────────────────────────────────────────────────────────────────

def _limiter_segments(begins, lengths, seg_peaks, chunks, denominators, limit, release_samples,
                      atten, delta, peak, number, seg_atten0, seg_delta):
    """The limiter's recursion over the pieces of a block, for numba: the same statements, in the same
    order, as ``Limiter._boundary`` and the loop in ``Limiter.process`` (which is its numpy fallback).

    Returns the state after the block: ``(atten, delta, peak, chunk_number)``; ``chunks`` is updated in place.
    """
    for k in range(lengths.shape[0]):
        if begins[k]:
            d = (1.0 - atten) / release_samples
            if abs(d) < 1e-20:
                d = 0.0
            for i in range(10):
                c = chunks[(number - 9 + i) & 15]
                if c > 0.0:
                    this_delta = (limit / c - atten) / denominators[i]
                    if this_delta < d:
                        d = this_delta
            chunks[number & 15] = peak
            number += 1
            delta = d
            peak = 0.0
        if seg_peaks[k] > peak:
            peak = seg_peaks[k]
        seg_atten0[k] = atten
        seg_delta[k] = delta
        atten += delta * lengths[k]
        if delta > 0.0 and atten >= 1.0:
            atten = 1.0
            delta = 0.0
    return atten, delta, peak, number


_LIMITER_KERNEL = fast.compile(_limiter_segments)


class Limiter:
    """swh Fast Lookahead limiter: 5 ms attack, no sample above ``limit_db`` (a brick wall at the end).

    Faithful to the C code: peaks are collected in 0.5 ms chunks, the attenuation heads for the
    quietest requirement of the last 10 chunks, recovers over ``release_s``, and the result is
    smoothed by a one-pole filter. Stereo-linked (the loudest of the two channels decides); a
    multichannel block is limited as one unit. Adds ``delay`` samples of latency.
    """

    NUM_CHUNKS = 16

    def __init__(self, rate: float = 48000.0, channels: int = 2, limit_db: float = -1.0,
                 release_s: float = 0.1, input_gain_db: float = 0.0):
        self.rate = float(rate)
        self.channels = channels
        self.delay = int(0.005 * rate)
        self.chunk_size = int(rate / 2000)
        self._denominators = np.array([(i + 1) * self.rate * 0.0005 + 1.0 for i in range(10)])
        self.set_params(limit_db, release_s, input_gain_db)
        self.reset()

    def set_params(self, limit_db: float, release_s: float, input_gain_db: float = 0.0) -> None:
        self.limit = _db_co(limit_db)
        self.release_s = max(release_s, 1e-5)
        self.trim = _db_co(input_gain_db)

    def reset(self) -> None:
        self._hist = np.zeros((self.channels, self.delay))
        self._chunk_pos = 0
        self._chunk_num = 0
        self._chunks = np.zeros(self.NUM_CHUNKS)
        self._peak = 0.0
        self._atten = 1.0
        self._lp_zi = np.array([0.9])      # lfilter state for atten_lp = 1
        self._delta = 0.0

    def _boundary(self, atten: float, peak: float) -> float:
        """What the C code does when a 0.5 ms chunk is complete: pick the slope of the attenuation."""
        mx = self.limit
        delta = (1.0 - atten) / (self.rate * self.release_s)
        if abs(delta) < 1e-20:
            delta = 0.0
        chunks, number = self._chunks, self._chunk_num
        for i, denominator in enumerate(self._denominators):
            c = chunks[(number - 9 + i) & (self.NUM_CHUNKS - 1)]
            this_delta = ((mx / c if c > 0.0 else math.inf) - atten) / denominator
            if this_delta < delta:
                delta = this_delta
        chunks[number & (self.NUM_CHUNKS - 1)] = peak
        self._chunk_num = number + 1
        return delta

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64) * self.trim
        n = x.shape[-1]
        sig = np.abs(x).max(axis=0) + _TINY

        # The C loop tests ``chunk_pos++ == chunk_size`` per sample and resets on a hit, so a chunk is
        # chunk_size + 1 samples and the trigger sample itself starts the next one.
        period = self.chunk_size + 1
        first = self.chunk_size - self._chunk_pos
        triggers = np.arange(first, n, period)
        starts = np.concatenate(([0], triggers[triggers > 0])) if n else np.zeros(0, dtype=int)
        lengths = np.diff(np.append(starts, n))
        begins_chunk = (starts[:, None] == triggers[None, :]).any(axis=1) if triggers.size else np.zeros(starts.size, bool)
        seg_peaks = np.maximum.reduceat(sig, starts) if n else np.zeros(0)

        # the recursion over the ~40 pieces of a block is plain scalar work; the samples are filled in after it
        seg_atten0, seg_delta = np.empty(starts.size), np.empty(starts.size)
        done = False
        if _LIMITER_KERNEL is not None and n:
            try:
                self._atten, self._delta, self._peak, self._chunk_num = _LIMITER_KERNEL(
                    begins_chunk, lengths, seg_peaks, self._chunks, self._denominators, self.limit,
                    self.rate * self.release_s, self._atten, self._delta, self._peak, self._chunk_num,
                    seg_atten0, seg_delta)
                done = True
            except Exception as exc:                          # noqa: BLE001 — e.g. a compile problem: stay on numpy
                globals()["_LIMITER_KERNEL"] = None
                fast.disable(f"limiter: {exc!r}")
        if not done:
            atten, delta, peak = self._atten, self._delta, self._peak
            for k, (begin, length, seg_peak) in enumerate(zip(begins_chunk.tolist(), lengths.tolist(),
                                                              seg_peaks.tolist())):
                if begin:
                    delta = self._boundary(atten, peak)
                    peak = 0.0
                if seg_peak > peak:
                    peak = seg_peak
                seg_atten0[k] = atten
                seg_delta[k] = delta
                atten += delta * length
                if delta > 0.0 and atten >= 1.0:          # reached unity: the C code zeroes delta
                    atten, delta = 1.0, 0.0
            self._atten, self._delta, self._peak = atten, delta, peak

        if n:
            offset = np.arange(n) - np.repeat(starts, lengths) + 1
            slope = np.repeat(seg_delta, lengths)
            ramp = np.repeat(seg_atten0, lengths) + slope * offset
            np.minimum(ramp, 1.0, out=ramp, where=slope > 0.0)
        else:
            ramp = np.zeros(0)

        if triggers.size:
            self._chunk_pos = n - 1 - int(triggers[-1])
        else:
            self._chunk_pos += n

        smooth, self._lp_zi = lfilter([0.1], [1.0, -0.9], ramp, zi=self._lp_zi)

        joined = np.concatenate([self._hist, x], axis=1)
        delayed, self._hist = joined[:, :n], joined[:, n:]
        out = delayed * smooth
        np.clip(out, -self.limit, self.limit, out=out)   # the brick wall at the very end
        return out
