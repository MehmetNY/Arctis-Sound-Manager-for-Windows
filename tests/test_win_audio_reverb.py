"""The vectorised plate reverb against a literal sample-by-sample port of swh's C code."""
from __future__ import annotations

import numpy as np
import pytest

from arctis_sound_manager.win.audio import fast, reverb
from arctis_sound_manager.win.audio.reverb import PlateReverb

SIZES = (2389, 4742, 4623, 2142, 5597, 3692, 5611, 3703)
DA = (0.04, 0.17, 0.52, 0.48, 0.32, 0.89, 0.28, 0.29)
LP_INNER, LP_OUTER = 0.96, 0.983


@pytest.fixture(params=["numpy", "compiled"], autouse=True)
def code_path(request, monkeypatch):
    """Every test in this file runs on the numpy path and, where numba is installed, on the compiled one."""
    if request.param == "numpy":
        monkeypatch.setattr(reverb, "_KERNEL", None)
    elif reverb._KERNEL is None:
        pytest.skip("numba is not installed")
    return request.param


class ReferencePlate:
    """plate_1423 and waveguide_nl.h, transcribed statement by statement."""

    def __init__(self, time, damping, wet):
        self.wet = wet
        self.size = list(SIZES)
        self.buf = [[[0.0] * s, [0.0] * s] for s in SIZES]
        self.ptr = [0] * 8
        self.lp = [[0.0, 0.0] for _ in range(8)]
        self.zm1 = [[0.0, 0.0] for _ in range(8)]
        self.a1a = [(1.0 - d) / (1.0 + d) for d in DA]
        self.out = [0.0] * 32
        scale = (time * 0.117647) ** 1.34
        lpscale = 1.0 - damping * 0.93
        self.delay = [max(1, min(s, int(s * scale))) for s in SIZES]
        self.fc = [(LP_INNER if i < 4 else LP_OUTER) * lpscale for i in range(8)]

    def _wg(self, n, in0, in1):
        s, d, p = self.size[n], self.delay[n], self.ptr[n]
        res = []
        for k, inp in ((0, in0), (1, in1)):
            o = self.buf[n][k][(p + d) % s]
            o = self.lp[n][k] * (self.fc[n] - 1.0) + self.fc[n] * o
            self.lp[n][k] = o
            tmp = o * -self.a1a[n] + self.zm1[n][k]
            self.zm1[n][k] = tmp * self.a1a[n] + o
            res.append(tmp)
        self.buf[n][0][p] = in0
        self.buf[n][1][p] = in1
        self.ptr[n] = (p - 1) % s
        return res

    def run(self, x):
        out = self.out
        L, R = [], []
        for inp in x:
            alpha = (out[0] + out[2] + out[4] + out[6]) * 0.5 + inp
            beta = (out[1] + out[9] + out[14]) * 0.666666666
            gamma = (out[3] + out[8] + out[11]) * 0.666666666
            delta = (out[5] + out[10] + out[13]) * 0.666666666
            epsilon = (out[7] + out[12] + out[15]) * 0.666666666
            for n, ja, jb in ((0, beta, alpha), (1, gamma, alpha), (2, delta, alpha), (3, epsilon, alpha),
                              (4, beta, gamma), (5, gamma, delta), (6, delta, epsilon), (7, epsilon, beta)):
                out[2 * n], out[2 * n + 1] = self._wg(n, ja - out[2 * n + 1], jb - out[2 * n])
            L.append(beta * self.wet + inp * (1.0 - self.wet))
            R.append(gamma * self.wet + inp * (1.0 - self.wet))
        return np.array(L), np.array(R)


@pytest.mark.parametrize("time,damping,wet", [(2.5, 0.5, 0.5), (2.5, 0.5, 1.0), (1.0, 0.2, 0.3), (6.0, 0.9, 0.7), (0.4, 0.0, 0.6)])
def test_matches_the_c_code_sample_for_sample(time, damping, wet):
    rng = np.random.default_rng(1)
    x = rng.standard_normal(6000) * 0.3
    x[:1000] *= 3
    ref = ReferencePlate(time, damping, wet)
    want_l, want_r = ref.run(x)
    rev = PlateReverb(time, damping, wet)
    got = [rev.process(x[i:j]) for i, j in zip([0, 480, 1000, 2000, 2001, 4800], [480, 1000, 2000, 2001, 4800, 6000])]
    got_l = np.concatenate([g[0] for g in got])
    got_r = np.concatenate([g[1] for g in got])
    assert np.allclose(got_l, want_l, atol=1e-9, rtol=1e-9)
    assert np.allclose(got_r, want_r, atol=1e-9, rtol=1e-9)
    assert np.max(np.abs(want_l)) > 0.01          # the comparison is not trivially about silence


def test_asm_settings_use_a_415_sample_minimum_delay():
    assert PlateReverb(2.5, 0.5, 1.0).min_delay == 415


def test_dry_is_untouched_and_wet_is_only_reverb():
    x = np.random.default_rng(2).standard_normal(4800) * 0.2
    dry_l, dry_r = PlateReverb(2.5, 0.5, 0.0).process(x)
    assert np.allclose(dry_l, x) and np.allclose(dry_r, x)
    wet_l, wet_r = PlateReverb(2.5, 0.5, 1.0).process(x)
    assert not np.allclose(wet_l, x) and not np.allclose(wet_r, wet_l)   # decorrelated left/right


def test_impulse_response_decays_and_stays_finite():
    # The plate is a long, slowly decaying tail (about 9 dB over ten seconds at ASM's settings); the
    # property that matters is that it never grows and never blows up.
    rev = PlateReverb(2.5, 0.5, 1.0)
    x = np.zeros(48000 * 8); x[0] = 1.0
    l = np.concatenate([rev.process(x[i:i + 480])[0] for i in range(0, len(x), 480)])
    assert np.all(np.isfinite(l))
    per_second = [float(np.sqrt(np.mean(l[i * 48000:(i + 1) * 48000] ** 2))) for i in range(8)]
    assert per_second[0] > 0
    assert all(b <= a * 1.0001 for a, b in zip(per_second, per_second[1:])), per_second


def test_damping_and_time_change_the_sound_and_stay_stable():
    x = np.zeros(48000); x[0] = 1.0
    base = PlateReverb(2.5, 0.5, 1.0).process(x)[0]
    for t, d in ((2.5, 0.0), (2.5, 1.0), (0.5, 0.5), (8.5, 0.5)):
        y = PlateReverb(t, d, 1.0).process(x)[0]
        assert np.all(np.isfinite(y)) and np.max(np.abs(y)) < 10
        assert not np.allclose(y, base)


def test_block_size_independence():
    x = np.random.default_rng(4).standard_normal(9000) * 0.2
    a, b = PlateReverb(), PlateReverb()
    la = np.concatenate([a.process(x[i:i + 480])[0] for i in range(0, 9000 - 9000 % 480 or 9000, 480)])
    lb = np.concatenate([b.process(x[i:i + 37])[0] for i in range(0, len(la), 37)])[:len(la)]
    assert np.allclose(la, lb, atol=1e-10)


def test_reset_clears_the_tail():
    rev = PlateReverb(2.5, 0.5, 1.0)
    x = np.zeros(4800); x[0] = 1.0
    rev.process(x)
    rev.reset()
    l, r = rev.process(np.zeros(4800))
    assert np.all(l == 0.0) and np.all(r == 0.0)


def test_batched_instances_are_independent_and_equal_to_separate_reverbs():
    rng = np.random.default_rng(11)
    x = rng.standard_normal((2, 5000)) * 0.2
    x[1, :1500] = 0.0                                         # different programmes per row
    both = PlateReverb(2.5, 0.5, 0.6, instances=2)
    one, two = PlateReverb(2.5, 0.5, 0.6), PlateReverb(2.5, 0.5, 0.6)
    got = [both.process(x[:, i:i + 480]) for i in range(0, 4800, 480)]
    gl = np.concatenate([g[0] for g in got], axis=1)
    gr = np.concatenate([g[1] for g in got], axis=1)
    for row, solo in ((0, one), (1, two)):
        want = [solo.process(x[row, i:i + 480]) for i in range(0, 4800, 480)]
        assert np.allclose(gl[row], np.concatenate([w[0] for w in want]), atol=1e-12)
        assert np.allclose(gr[row], np.concatenate([w[1] for w in want]), atol=1e-12)
    assert not np.allclose(gl[0], gl[1])


def test_batched_instances_reject_a_wrong_row_count():
    with pytest.raises(ValueError):
        PlateReverb(instances=2).process(np.zeros((3, 480)))


def test_the_compiled_and_numpy_paths_agree_to_rounding(monkeypatch):
    if reverb._KERNEL is None:
        pytest.skip("numba is not installed")
    rng = np.random.default_rng(5)
    x = rng.standard_normal((2, 5000)) * 0.3
    results = []
    for kernel in (None, reverb._KERNEL):
        monkeypatch.setattr(reverb, "_KERNEL", kernel)
        r = PlateReverb(3.0, 0.4, 0.6, instances=2)
        parts = [r.process(x[:, lo:hi]) for lo, hi in ((0, 777), (777, 2500), (2500, 5000))]     # uneven blocks, state carried
        results.append((np.concatenate([p[0] for p in parts], axis=1), np.concatenate([p[1] for p in parts], axis=1)))
    assert np.max(np.abs(results[0][0] - results[1][0])) < 1e-9
    assert np.max(np.abs(results[0][1] - results[1][1])) < 1e-9


def test_a_parameter_change_mid_stream_keeps_both_paths_equal(monkeypatch):
    if reverb._KERNEL is None:
        pytest.skip("numba is not installed")
    rng = np.random.default_rng(6)
    x = rng.standard_normal(4000) * 0.3
    outs = []
    for kernel in (None, reverb._KERNEL):
        monkeypatch.setattr(reverb, "_KERNEL", kernel)
        r = PlateReverb(2.0, 0.5, 0.5)
        a = r.process(x[:2000])
        r.set_params(4.0, 0.2, 0.8)
        b = r.process(x[2000:])
        outs.append(np.concatenate([a[0], b[0], a[1], b[1]]))
    assert np.max(np.abs(outs[0] - outs[1])) < 1e-9


def test_a_failing_kernel_falls_back_to_numpy_without_losing_the_block(monkeypatch):
    calls = []

    def broken(*args):
        calls.append(1)
        raise RuntimeError("compile error")
    monkeypatch.setattr(reverb, "_KERNEL", broken)
    x = np.random.default_rng(7).standard_normal(1000) * 0.2
    r = PlateReverb(2.5, 0.5, 0.5)
    got = r.process(x)
    assert calls == [1] and reverb._KERNEL is None                              # tried once, then disabled
    monkeypatch.setattr(reverb, "_KERNEL", None)
    want = PlateReverb(2.5, 0.5, 0.5).process(x)
    assert np.array_equal(got[0], want[0]) and np.array_equal(got[1], want[1])
    assert fast.HAVE_NUMBA in (True, False)
