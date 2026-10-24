"""The microphone chain: each stage against what its Linux conf does, and the order they run in."""
from __future__ import annotations

import numpy as np
import pytest

from arctis_sound_manager.eq_types import EqBand
from arctis_sound_manager.win.audio import biquad
from arctis_sound_manager.win.audio.dynamics import db2lin
from arctis_sound_manager.win.audio.mic import MicChain
from arctis_sound_manager.win.audio.params import mic_spec

RATE, B = 48000, 480


def spec(**state):
    return mic_spec(state)


def run(chain, x):
    n = x.size - x.size % B
    return np.concatenate([chain.process(x[i:i + B]) for i in range(0, n, B)])


def sine(freq, db, seconds=1.0):
    t = np.arange(int(RATE * seconds)) / RATE
    return np.sin(2 * np.pi * freq * t) * db2lin(db)


def gain_db(chain, freq, level_db=-30.0, skip=0.5):
    x = sine(freq, level_db)
    y = run(chain, x)
    k = int(skip * RATE)
    return 20 * np.log10(np.sqrt(np.mean(y[k:] ** 2)) / np.sqrt(np.mean(x[k:len(y)] ** 2)))


def make(**state):
    c = MicChain(RATE)
    c.set_spec(spec(**state))
    return c


class FakeNative:
    def __init__(self, vad=1.0):
        self.vad = vad

    def create(self):
        return object()

    def destroy(self, state):
        pass

    def process(self, state, frame):
        return self.vad


def test_default_spec_is_a_bit_exact_passthrough():
    c = make()
    x = np.random.default_rng(0).standard_normal(B * 20) * 0.1
    assert np.array_equal(run(c, x), x)
    assert c.latency_frames == 0 and c.noise_engine_active is None


def test_eq_has_no_headroom_compensation_and_follows_the_macros():
    c = make(voix_db=6.0)
    assert gain_db(c, 2000.0) == pytest.approx(6.0, abs=0.05)         # the playback EQ would have lowered it
    assert gain_db(c, 100.0) == pytest.approx(0.0, abs=0.3)
    c = make(bands=[EqBand(freq=1000.0, gain=-5.0, q=1.0, type="peakingEQ")], boost_db=4.0)
    assert gain_db(c, 1000.0) == pytest.approx(-5.0 + 4.0, abs=0.1)
    assert gain_db(c, 8000.0) == pytest.approx(4.0, abs=0.1)


def test_background_noise_reduction_is_a_high_pass_between_30_and_350_hz():
    c = make(noise_reduction={"bgReduction": {"enabled": True, "value": 0.5}})
    corner = 30.0 + 0.5 * 320.0
    assert gain_db(c, corner) == pytest.approx(-3.0, abs=0.2)
    assert gain_db(c, corner / 4) < -20
    assert gain_db(c, corner * 8) == pytest.approx(0.0, abs=0.1)
    disabled = make(noise_reduction={"bgReduction": {"enabled": False, "value": 1.0}})
    assert gain_db(disabled, 50.0) == pytest.approx(0.0, abs=1e-6)


def test_impact_noise_reduction_is_a_4khz_shelf_down_to_12_db():
    c = make(noise_reduction={"impactReduction": {"enabled": True, "value": 0.5}})
    assert gain_db(c, 12000.0) == pytest.approx(-6.0, abs=0.3)
    assert gain_db(c, 4000.0) == pytest.approx(-3.0, abs=0.3)
    assert gain_db(c, 200.0) == pytest.approx(0.0, abs=0.2)


def test_filters_run_in_the_conf_order_eq_then_highpass_then_shelf():
    c = make(voix_db=3.0, noise_reduction={"bgReduction": {"enabled": True, "value": 0.2},
                                           "impactReduction": {"enabled": True, "value": 1.0}})
    from arctis_sound_manager.win.audio.params import eq_sections
    eq = eq_sections([], 0, 3.0, 0, 0, RATE, headroom=False)
    sos = c._filters.sos
    assert np.array_equal(sos[:len(eq)], eq)
    assert np.allclose(sos[len(eq)], biquad.to_sos_row(biquad.design("highpass", 30.0 + 0.2 * 320, 0.7071, 0.0, RATE)))
    assert np.allclose(sos[len(eq) + 1], biquad.to_sos_row(biquad.design("highshelf", 4000.0, 0.7071, -12.0, RATE)))
    assert len(sos) == len(eq) + 2


def test_noise_gate_uses_the_fixed_timings_and_adds_five_ms_of_latency():
    c = make(noise_reduction={"noiseGate": {"enabled": True, "value": -40.0}})
    assert c.latency_frames == 240
    loud = sine(500, -20, 0.5)
    quiet = sine(500, -55, 1.0) * 1.0
    x = np.concatenate([loud, quiet])
    y = run(c, x)
    assert np.max(np.abs(y[:24000])) > 0.05                          # speech-level audio passes
    assert np.max(np.abs(y[-4800:])) < db2lin(-55) * 0.1             # 0.5 s later the room noise is gone (-90 dB range)
    assert c._gate._hold == 2400 and c._gate._cut == 0.0           # swh's -90 dB is digital silence


def test_compressor_matches_the_linux_parameters():
    c = make(noise_reduction={"compressor": {"enabled": True, "value": 1.0}})
    comp = c.spec.compressor
    assert (comp.threshold_db, comp.ratio, comp.makeup_db) == (-30.0, 8.0, 10.0)
    loud = gain_db(c, 800.0, level_db=-6.0, skip=0.6)
    soft = gain_db(c, 800.0, level_db=-45.0, skip=0.6)
    assert loud < soft                                               # the loud signal is turned down more
    assert soft == pytest.approx(10.0, abs=1.5)                      # below threshold: only the make-up gain


def test_noise_cancelling_runs_rnnoise_with_the_slider_as_vad_threshold():
    c = MicChain(RATE, native=FakeNative(vad=0.6))
    c.set_spec(spec(noise_canceling={"enabled": True, "engine": "rnnoise", "value": 0.5}))
    assert c.noise_engine_active == "rnnoise"
    y = run(c, np.full(B * 6, 0.25))
    assert np.allclose(y[-B:], 0.25, atol=1e-4)                      # 0.6 >= 0.5: speech, passes
    c.set_spec(spec(noise_canceling={"enabled": True, "engine": "rnnoise", "value": 0.9}))
    c._denoiser.grace_frames = 0
    c._denoiser.reset()
    y = run(c, np.full(B * 6, 0.25))
    assert np.max(np.abs(y[-B:])) == 0.0                             # 0.6 < 0.9: shut
    assert c._denoiser.vad_threshold == 0.9


def test_deepfilternet_request_falls_back_to_rnnoise_and_says_so():
    c = MicChain(RATE, native=FakeNative())
    c.set_spec(spec(noise_canceling={"enabled": True, "engine": "deepfilternet", "value": 0.5}))
    assert c.noise_engine_active == "rnnoise"
    assert any("deepfilternet" in n for n in c.notes)


def test_missing_rnnoise_library_degrades_to_no_noise_cancelling(monkeypatch):
    from arctis_sound_manager.win.audio import rnnoise

    monkeypatch.setattr(rnnoise, "find_library", lambda: None)
    monkeypatch.setattr(rnnoise, "_native", None)
    c = MicChain(RATE)
    c.set_spec(spec(noise_canceling={"enabled": True, "engine": "rnnoise", "value": 0.5}))
    assert c.noise_engine_active is None and any("not found" in n for n in c.notes)
    x = np.random.default_rng(1).standard_normal(B * 4) * 0.1
    assert np.array_equal(run(c, x), x)


def test_stage_order_is_gate_then_noise_cancelling_then_compressor():
    class Recorder(FakeNative):
        seen = []

    log = []
    c = MicChain(RATE, native=FakeNative(vad=1.0))
    c.set_spec(spec(noise_reduction={"noiseGate": {"enabled": True, "value": -40.0},
                                     "compressor": {"enabled": True, "value": 0.5}},
                    noise_canceling={"enabled": True, "engine": "rnnoise", "value": 0.0}))
    for name, stage in (("gate", c._gate), ("nc", c._denoiser), ("comp", c._compressor)):
        original = stage.process
        stage.process = (lambda f, n: (lambda *a, **k: (log.append(n), f(*a, **k))[1]))(original, name)
    c.process(np.zeros(B))
    assert log == ["gate", "nc", "comp"]


def test_switching_stages_on_and_off_is_stateless_and_click_free():
    c = make(voix_db=6.0)
    x = sine(1000, -20, 0.2)
    y1 = run(c, x)
    c.set_spec(spec(voix_db=0.0))
    y2 = run(c, x)
    assert np.array_equal(y2[-B:], x[x.size - x.size % B - B: x.size - x.size % B])
    seam = np.concatenate([y1[-2:], y2[:2]])
    assert np.max(np.abs(np.diff(seam))) < 0.5                        # no jump at the parameter change
    c.set_spec(spec(noise_reduction={"noiseGate": {"enabled": True, "value": -30.0}}))
    assert c.latency_frames == 240
    c.set_spec(spec())
    assert c.latency_frames == 0


def test_real_rnnoise_library_in_the_chain():
    from arctis_sound_manager.win.audio import rnnoise

    if not rnnoise.available():
        pytest.skip("RNNoise library not installed")
    c = MicChain(RATE)
    c.set_spec(spec(noise_canceling={"enabled": True, "engine": "rnnoise", "value": 0.0}))
    hiss = np.random.default_rng(3).standard_normal(RATE) * 0.05
    y = run(c, hiss)
    assert 20 * np.log10(np.std(hiss[9600:len(y)]) / np.std(y[9600:])) > 30


def test_a_stage_that_is_switched_back_on_starts_from_a_clean_state():
    gate_on = spec(noise_reduction={"noiseGate": {"enabled": True, "value": -40.0}})
    c = MicChain(RATE)
    c.set_spec(gate_on)
    run(c, sine(500, -10, 0.3))                                     # gate wide open, hold running
    c.set_spec(spec())
    c.set_spec(gate_on)
    y = run(c, sine(500, -55, 0.1))
    assert np.max(np.abs(y)) == 0.0                                 # not the stale open gate

    comp_on = spec(noise_reduction={"compressor": {"enabled": True, "value": 1.0}})
    c.set_spec(comp_on)
    run(c, sine(500, -3, 0.5))                                      # heavily compressed
    c.set_spec(spec())
    c.set_spec(comp_on)
    fresh = MicChain(RATE)
    fresh.set_spec(comp_on)
    probe = sine(500, -45, 0.05)
    assert np.allclose(run(c, probe), run(fresh, probe))
