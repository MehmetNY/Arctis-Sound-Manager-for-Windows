"""PipeWire-exact biquads and the click-free cascade.

The reference below derives every filter independently: analog prototype -> prewarped bilinear
transform with scipy. The RBJ cookbook *is* that derivation, so agreement to ~1e-9 on the complex
response proves the ported coefficient formulas have no typo in any of the nine types.
"""
from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from scipy.signal import bilinear, freqz, sosfreqz

from arctis_sound_manager.win.audio import biquad
from arctis_sound_manager.win.audio.eq import BiquadCascade

RATE = 48000.0


def reference(kind, f0, q, gain_db, rate=RATE):
    """(b, a) of the analog prototype at w0 = 1, bilinear-transformed with prewarping at f0."""
    A = 10.0 ** (gain_db / 40.0)
    sA = math.sqrt(A)
    # descending powers of the normalised s
    protos = {
        "lowpass": ([1], [1, 1 / q, 1]),
        "highpass": ([1, 0, 0], [1, 1 / q, 1]),
        "bandpass": ([1 / q, 0], [1, 1 / q, 1]),
        "notch": ([1, 0, 1], [1, 1 / q, 1]),
        "allpass": ([1, -1 / q, 1], [1, 1 / q, 1]),
        "peaking": ([1, A / q, 1], [1, 1 / (A * q), 1]),
        # A (s^2 + sqrt(A)/Q s + A) / (A s^2 + sqrt(A)/Q s + 1)
        "lowshelf": ([A, A * sA / q, A * A], [A, sA / q, 1]),
        # A (A s^2 + sqrt(A)/Q s + 1) / (s^2 + sqrt(A)/Q s + A)
        "highshelf": ([A * A, A * sA / q, A], [1, sA / q, A]),
    }
    b, a = protos[kind]
    w0 = 2 * math.pi * f0 / rate
    wa = 2 * rate * math.tan(w0 / 2)  # prewarped: s_normalised = s / wa maps f0 to the unit frequency

    def denorm(poly):
        n = len(poly) - 1
        return [c / (wa ** (n - i)) for i, c in enumerate(poly)]

    return bilinear(denorm(b), denorm(a), fs=rate)


def complex_response(coeffs, freqs):
    b0, b1, b2, a1, a2 = coeffs
    _, h = freqz([b0, b1, b2], [1.0, a1, a2], worN=2 * np.pi * np.asarray(freqs) / RATE)
    return h


GRID_F = [60.0, 250.0, 1000.0, 4000.0, 9000.0, 16000.0]
GRID_Q = [0.3, 0.7071, 1.5, 4.0]
GRID_G = [-12.0, -3.0, 4.5, 12.0]
PROBE = np.geomspace(20, 20000, 60)


@pytest.mark.parametrize("kind", ["lowpass", "highpass", "bandpass", "notch", "allpass"])
def test_gainless_types_match_the_independent_derivation(kind):
    for f0, q in itertools.product(GRID_F, GRID_Q):
        mine = complex_response(biquad.design(kind, f0, q, 0.0, RATE), PROBE)
        b, a = reference(kind, f0, q, 0.0)
        _, ref = freqz(b, a, worN=2 * np.pi * PROBE / RATE)
        assert np.allclose(mine, ref, atol=1e-8), (kind, f0, q)


def test_peaking_matches_the_independent_derivation():
    for f0, q, g in itertools.product(GRID_F, GRID_Q, GRID_G):
        mine = complex_response(biquad.design("peaking", f0, q, g, RATE), PROBE)
        b, a = reference("peaking", f0, q, g)
        _, ref = freqz(b, a, worN=2 * np.pi * PROBE / RATE)
        assert np.allclose(mine, ref, atol=1e-8), (f0, q, g)


@pytest.mark.parametrize("kind", ["lowshelf", "highshelf"])
def test_shelves_match_the_independent_derivation(kind):
    for f0, q, g in itertools.product(GRID_F, GRID_Q, GRID_G):
        mine = complex_response(biquad.design(kind, f0, q, g, RATE), PROBE)
        b, a = reference(kind, f0, q, g)
        _, ref = freqz(b, a, worN=2 * np.pi * PROBE / RATE)
        assert np.allclose(mine, ref, atol=1e-8), (kind, f0, q, g)


@pytest.mark.parametrize("kind", ["lowshelf", "highshelf"])
def test_shelves_hit_their_plateau_gains(kind):
    for f0, q, g in itertools.product([100.0, 1000.0, 8000.0], [0.5, 0.7071, 1.0], [-12.0, 6.0, 12.0]):
        c = biquad.design(kind, f0, q, g, RATE)
        lo, hi = complex_response(c, [1.0]), complex_response(c, [23999.0])
        plateau, other = (lo, hi) if kind == "lowshelf" else (hi, lo)
        assert abs(20 * np.log10(abs(plateau[0])) - g) < 0.05, (kind, f0, q, g)
        assert abs(20 * np.log10(abs(other[0]))) < 0.05, (kind, f0, q, g)
        # the knee sits at the midpoint gain (the cookbook's definition), prewarped so it is exact
        mid = complex_response(c, [f0])
        assert abs(20 * np.log10(abs(mid[0])) - g / 2) < 0.05, (kind, f0, q, g)


def test_peaking_gain_at_centre_and_bandwidth_side_effects():
    for f0, q, g in itertools.product(GRID_F, GRID_Q, GRID_G):
        c = biquad.design("peaking", f0, q, g, RATE)
        assert abs(20 * np.log10(abs(complex_response(c, [f0])[0])) - g) < 1e-6
    # zero gain is the identity
    c = biquad.design("peaking", 1000.0, 1.0, 0.0, RATE)
    assert np.allclose(c, (1, 0, 0, 0, 0), atol=1e-12) or np.allclose(complex_response(c, PROBE), 1.0, atol=1e-12)


def test_bandpass_is_0db_at_centre_and_notch_is_a_null():
    c = biquad.design("bandpass", 1000.0, 2.0, 0.0, RATE)
    assert abs(abs(complex_response(c, [1000.0])[0]) - 1.0) < 1e-9
    n = biquad.design("notch", 1000.0, 2.0, 0.0, RATE)
    assert abs(complex_response(n, [1000.0])[0]) < 1e-9


def test_allpass_is_flat_in_magnitude():
    c = biquad.design("allpass", 2000.0, 1.3, 0.0, RATE)
    assert np.allclose(np.abs(complex_response(c, PROBE)), 1.0, atol=1e-9)


def test_boost_shelf_is_a_broadband_gain():
    # Boost = bq_highshelf at 10 Hz, Q 0.7071: flat +g across the audible range
    for g in (-24.0, -6.0, 6.0, 12.0):
        sos = biquad.to_sos_row(biquad.design("highshelf", 10.0, 0.7071, g, RATE))[None, :]
        resp = biquad.response_db(sos, [100, 1000, 10000, 20000], RATE)
        assert np.allclose(resp, g, atol=0.05), (g, resp)


@pytest.mark.parametrize("kind,expect", [
    ("lowpass", (0.0, 0.0)), ("highpass", (1.0, 0.0)),
])
def test_edge_frequencies_follow_pipewire(kind, expect):
    # cutoff 0 -> lowpass passes nothing / highpass everything; cutoff Nyquist -> the opposite
    c0 = biquad.design(kind, 0.0, 0.7, 0.0, RATE)
    assert c0[0] == expect[0] and c0[1:] == (0.0, 0.0, 0.0, 0.0)
    c1 = biquad.design(kind, RATE / 2, 0.7, 0.0, RATE)
    assert c1[0] == (1.0 if kind == "lowpass" else 0.0)


def test_q_zero_limits():
    A = 10 ** (6 / 40)
    assert biquad.design("peaking", 1000, 0, 6, RATE)[0] == pytest.approx(A * A)
    assert biquad.design("lowshelf", 1000, 0, 6, RATE)[0] == pytest.approx(A)
    assert biquad.design("highshelf", 1000, 0, 6, RATE)[0] == pytest.approx(A)
    assert biquad.design("notch", 1000, 0, 0, RATE)[0] == 0.0
    assert biquad.design("allpass", 1000, 0, 0, RATE)[0] == -1.0
    assert biquad.design("bandpass", 1000, 0, 0, RATE)[0] == 1.0


def test_shelf_frequency_limits():
    A2 = 10 ** (6 / 20)
    assert biquad.design("lowshelf", RATE / 2, 0.7, 6, RATE)[0] == pytest.approx(A2)
    assert biquad.design("lowshelf", 0.0, 0.7, 6, RATE)[0] == 1.0
    assert biquad.design("highshelf", 0.0, 0.7, 6, RATE)[0] == pytest.approx(A2)
    assert biquad.design("highshelf", RATE / 2, 0.7, 6, RATE)[0] == 1.0


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        biquad.design("chebyshev", 1000, 1, 0, RATE)


def test_every_sonar_band_type_has_a_kind():
    from arctis_sound_manager.eq_types import PW_LABEL
    assert set(biquad.KIND_FOR_BAND_TYPE) == set(PW_LABEL)
    for band_type, label in PW_LABEL.items():
        assert label == "bq_" + biquad.KIND_FOR_BAND_TYPE[band_type], band_type


# ── cascade ──────────────────────────────────────────────────────────────────

def make_sos(*specs):
    return np.array([biquad.to_sos_row(biquad.design(*s, RATE)) for s in specs])


def test_cascade_is_block_size_independent():
    sos = make_sos(("peaking", 300, 1.0, 6), ("highshelf", 6000, 0.7, -4), ("lowpass", 12000, 0.7, 0))
    x = np.random.default_rng(1).standard_normal((2, 4800))
    whole = BiquadCascade(2, sos).process(x)
    c = BiquadCascade(2, sos)
    parts = np.concatenate([c.process(x[:, i:i + 480]) for i in range(0, 4800, 480)], axis=1)
    assert np.allclose(whole, parts, atol=1e-12)
    odd = BiquadCascade(2, sos)
    cuts = [0, 17, 480, 481, 1000, 4800]
    parts = np.concatenate([odd.process(x[:, a:b]) for a, b in zip(cuts, cuts[1:])], axis=1)
    assert np.allclose(whole, parts, atol=1e-12)


def test_empty_cascade_is_a_copy():
    c = BiquadCascade(2)
    x = np.ones((2, 480))
    y = c.process(x)
    assert np.array_equal(x, y) and y is not x


def test_channels_are_independent():
    sos = make_sos(("lowpass", 500, 0.7, 0))
    x = np.zeros((2, 480))
    x[0] = np.random.default_rng(2).standard_normal(480)
    y = BiquadCascade(2, sos).process(x)
    assert np.all(y[1] == 0.0) and np.any(y[0] != 0.0)


def test_set_sos_reports_changes():
    sos = make_sos(("peaking", 300, 1.0, 6))
    c = BiquadCascade(1, sos)
    assert c.set_sos(sos.copy()) is False
    assert c.set_sos(make_sos(("peaking", 300, 1.0, 5))) is True


def _max_step(y):
    return float(np.max(np.abs(np.diff(y))))


def test_parameter_change_does_not_click():
    f = 220.0
    t = np.arange(480 * 20) / RATE
    x = 0.5 * np.sin(2 * np.pi * f * t)[None, :]
    a = make_sos(("peaking", 220, 1.0, 0.0001))
    b = make_sos(("peaking", 220, 1.0, 12))
    smooth = BiquadCascade(1, a)
    out = []
    for i in range(20):
        if i == 10:
            smooth.set_sos(b)
        out.append(smooth.process(x[:, i * 480:(i + 1) * 480]))
    smooth_out = np.concatenate(out, axis=1)[0]

    # PipeWire's behaviour: history cleared on every change
    hard = BiquadCascade(1, a)
    hard_out = []
    for i in range(20):
        if i == 10:
            hard = BiquadCascade(1, b)
        hard_out.append(hard.process(x[:, i * 480:(i + 1) * 480]))
    hard_out = np.concatenate(hard_out, axis=1)[0]

    # the 12 dB boost makes a sine bigger, so compare the step at the switch to the signal's own slope
    own = 4 * 0.5 * 2 * np.pi * f / RATE
    assert _max_step(smooth_out[10 * 480 - 5:10 * 480 + 480]) < own
    assert _max_step(hard_out[10 * 480 - 5:10 * 480 + 480]) > 0.05 * own  # sanity: the comparison is meaningful
    assert np.all(np.isfinite(smooth_out))


def test_structure_change_is_finite_and_settles_to_the_new_filter():
    one = make_sos(("peaking", 500, 1.0, 6))
    three = make_sos(("peaking", 500, 1.0, 6), ("lowshelf", 100, 0.7, 3), ("highshelf", 9000, 0.7, -3))
    c = BiquadCascade(1, one)
    x = np.random.default_rng(3).standard_normal((1, 480))
    c.process(x)
    c.set_sos(three)
    y1 = c.process(x)
    assert np.all(np.isfinite(y1))
    for _ in range(50):
        c.process(x)
    fresh = BiquadCascade(1, three)
    for _ in range(50):
        fresh.process(x)
    assert np.allclose(c.process(x), fresh.process(x), atol=1e-6)


def test_response_db_helper_matches_freqz():
    sos = make_sos(("peaking", 1000, 1.0, 6), ("lowpass", 8000, 0.7, 0))
    f = np.array([100.0, 1000.0, 10000.0])
    w, h = sosfreqz(sos, worN=2 * np.pi * f / RATE)
    assert np.allclose(biquad.response_db(sos, f, RATE), 20 * np.log10(np.abs(h)))
    assert np.all(biquad.response_db(np.zeros((0, 6)), f, RATE) == 0)
