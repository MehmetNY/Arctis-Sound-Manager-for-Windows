# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The microphone chain, in the order ASM's ``generate_sonar_micro_conf`` wires it::

    EQ (bands, Basses/Voix/Aigus, Boost)  ->  background-noise high-pass  ->  impact-noise shelf
      ->  noise gate  ->  noise cancelling (RNNoise)  ->  compressor

Every stage is optional and follows the :class:`~.params.MicSpec` read from ``sonar_micro_state.json``.
The three filters are one SOS cascade (one ``sosfilt`` call, click-free parameter changes); the gate
and the compressor are the swh ports of :mod:`.dynamics`; noise cancelling is :mod:`.rnnoise`.
"""
from __future__ import annotations

import logging

import numpy as np

from arctis_sound_manager.win.audio import biquad, rnnoise
from arctis_sound_manager.win.audio.dynamics import Compressor, Gate
from arctis_sound_manager.win.audio.eq import BiquadCascade
from arctis_sound_manager.win.audio.params import MicSpec, eq_sections

_log = logging.getLogger(__name__)

# fixed in the Linux conf
GATE_ATTACK_MS, GATE_HOLD_MS, GATE_DECAY_MS, GATE_RANGE_DB = 5.0, 50.0, 100.0, -90.0
HIGHPASS_Q = 0.7071
IMPACT_FREQ_HZ, IMPACT_Q = 4000.0, 0.7071


class MicChain:
    """Mono microphone processing for blocks that are a multiple of 480 samples."""

    def __init__(self, rate: int = 48000, native=None):
        self.rate = rate
        self._native = native
        self._filters = BiquadCascade(1)
        self._gate = Gate(rate, lookahead_ms=5.0)
        self._compressor = Compressor(rate, 1)
        self._denoiser: rnnoise.RNNoise | None = None
        self.spec = MicSpec([], 0.0, 0.0, 0.0, 0.0, None, None, None, False, "rnnoise", 0.9, None)
        self._gate_on = False
        self._compressor_on = False
        self.noise_engine_active: str | None = None      # what really runs: "rnnoise" or None
        self.notes: list[str] = []

    # ── configuration ───────────────────────────────────────────────────────

    def set_spec(self, spec: MicSpec) -> None:
        self.spec = spec
        self.notes = []

        rows = eq_sections(spec.eq_bands, spec.basses_db, spec.voix_db, spec.aigus_db, spec.boost_db,
                           self.rate, headroom=False)
        extra = []
        if spec.background_highpass_hz is not None:
            extra.append(biquad.to_sos_row(biquad.design("highpass", spec.background_highpass_hz, HIGHPASS_Q, 0.0, self.rate)))
        if spec.impact_gain_db is not None:
            extra.append(biquad.to_sos_row(biquad.design("highshelf", IMPACT_FREQ_HZ, IMPACT_Q, spec.impact_gain_db, self.rate)))
        sos = np.vstack([rows, *extra]) if (rows.size or extra) else np.zeros((0, 6))
        self._filters.set_sos(sos)

        gate_on = spec.gate_threshold_db is not None
        if gate_on:
            self._gate.set_params(spec.gate_threshold_db, GATE_ATTACK_MS, GATE_HOLD_MS, GATE_DECAY_MS, GATE_RANGE_DB)
            if not self._gate_on:
                self._gate.reset()
        self._gate_on = gate_on

        comp = spec.compressor
        if comp is not None:
            self._compressor.set_params(comp.rms_peak, comp.attack_ms, comp.release_ms, comp.threshold_db,
                                        comp.ratio, comp.knee_db, comp.makeup_db)
            if not self._compressor_on:
                self._compressor.reset()
        self._compressor_on = comp is not None

        self._configure_noise_cancelling(spec)

    def _configure_noise_cancelling(self, spec: MicSpec) -> None:
        if not spec.noise_canceling:
            self.noise_engine_active = None
            return
        if spec.noise_canceling_engine != "rnnoise":
            self.notes.append(f"{spec.noise_canceling_engine} is not available on Windows; using RNNoise instead")
        if self._denoiser is None:
            try:
                self._denoiser = rnnoise.RNNoise(spec.noise_canceling_strength, native=self._native)
            except rnnoise.RNNoiseUnavailable as exc:
                _log.warning("noise cancelling unavailable: %s", exc)
                self.notes.append(f"noise cancelling disabled: {exc}")
                self.noise_engine_active = None
                return
        # the slider is the plugin's "VAD Threshold (%)"; grace period and retroactive grace keep its defaults
        self._denoiser.set_params(spec.noise_canceling_strength)
        self.noise_engine_active = "rnnoise"

    # ── processing ──────────────────────────────────────────────────────────

    @property
    def latency_frames(self) -> int:
        frames = self._gate.lookahead_frames if self._gate_on else 0
        if self.noise_engine_active and self._denoiser is not None:
            frames += self._denoiser.latency_frames
        return frames

    def reset(self) -> None:
        self._filters.reset()
        self._gate.reset()
        self._compressor.reset()
        if self._denoiser is not None:
            self._denoiser.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        """``x``: 1-D float block (multiple of 480) -> same length."""
        y = self._filters.process(x[None, :] if x.ndim == 1 else x[:1])
        if self._gate_on:
            y = self._gate.process(y)
        if self.noise_engine_active and self._denoiser is not None:
            y = self._denoiser.process(y[0])[None, :]
        if self._compressor_on:
            y = self._compressor.process(y)
        return y[0]
