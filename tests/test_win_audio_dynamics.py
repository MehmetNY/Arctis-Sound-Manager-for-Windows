"""Compressor, gate and limiter against the swh-plugins behaviour they port."""
from __future__ import annotations

import math

import numpy as np
import pytest

from arctis_sound_manager.win.audio import dynamics
from arctis_sound_manager.win.audio.dynamics import Compressor, Gate, Limiter, db2lin, lin2db

RATE = 48000
BLOCK = 480


@pytest.fixture(params=["numpy", "compiled"])
def limiter_path(request, monkeypatch):
    """The limiter tests run on the numpy path and, where numba is installed, on the compiled one."""
    if request.param == "numpy":
        monkeypatch.setattr(dynamics, "_LIMITER_KERNEL", None)
    elif dynamics._LIMITER_KERNEL is None:
        pytest.skip("numba is not installed")
    return request.param


def tone(db_rms, seconds, channels=1, freq=1000.0, rate=RATE):
    t = np.arange(int(seconds * rate)) / rate
    amp = db2lin(db_rms) * math.sqrt(2.0)
    return np.tile(amp * np.sin(2 * np.pi * freq * t), (channels, 1))


def run(proc, x, block=BLOCK):
    return np.concatenate([proc.process(x[:, i:i + block]) for i in range(0, x.shape[1], block)], axis=1)


def rms_db(x):
    return float(20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-30))


# ── compressor ───────────────────────────────────────────────────────────────

def expected_gain_db(level_db, threshold, ratio, knee):
    """The plugin's static curve (soft knee over threshold +- knee), in dB."""
    rs = (ratio - 1.0) / ratio
    if level_db <= threshold - knee:
        return 0.0
    if level_db < threshold + knee:
        x = -(threshold - knee - level_db) / knee
        return -knee * rs * x * x * 0.25
    return (threshold - level_db) * rs


@pytest.mark.parametrize("threshold,ratio,knee,makeup", [(-18.0, 4.0, 6.0, 0.0), (-27.0, 6.0, 8.0, 4.0), (-9.0, 3.0, 4.0, 12.0), (-20.0, 20.0, 1.0, 0.0)])
def test_static_curve_matches_the_plugin_formula(threshold, ratio, knee, makeup):
    for level in (-50, -35, threshold - knee - 1, threshold - knee / 2, threshold, threshold + knee / 2, threshold + knee + 1, threshold + 9, -3):
        c = Compressor(RATE, 1)
        c.set_params(0.0, 10.0, 200.0, threshold, ratio, knee, makeup)
        y = run(c, tone(level, 3.0))
        got = rms_db(y[:, -RATE:]) - level
        want = expected_gain_db(level, threshold, ratio, knee) + makeup
        assert abs(got - want) < 0.35, (level, got, want)


def test_passthrough_when_ratio_one_and_no_makeup():
    c = Compressor(RATE, 2)
    c.set_params(0.0, 10.0, 200.0, -18.0, 1.0, 6.0, 0.0)
    assert c.active
    x = np.random.default_rng(0).standard_normal((2, 4800)) * 0.3
    assert np.allclose(run(c, x), x, atol=1e-12)
    c.set_params(0.0, 10.0, 200.0, -18.0, 4.0, 6.0, 0.0)
    assert not c.active
    c.set_params(0.0, 10.0, 200.0, -18.0, 1.0, 6.0, 3.0)
    assert not c.active


def test_envelope_follows_the_plugins_time_constants():
    # attack 10 ms is table entry round(0.010 * 255) = 3 -> tau = 3/256 s; release 200 ms -> 51/256 s
    c = Compressor(RATE, 1)
    c.set_params(0.0, 10.0, 200.0, -18.0, 4.0, 6.0, 0.0)
    loud = tone(-10, 1.0)
    # attack: how long until the RMS envelope reaches 63% of the final level
    final = db2lin(-10)
    hit = None
    step = 8
    for i in range(0, RATE // 2, step):
        c.process(loud[:, i:i + step])
        if hit is None and c._env_rms >= (1 - 1 / math.e) * final:
            hit = i + step
    tau_attack = 3 / 256
    assert hit is not None and abs(hit / RATE - tau_attack) < 0.35 * tau_attack + 0.003, hit / RATE

    # release: from loud into silence, 37% of the level remains after tau
    c2 = Compressor(RATE, 1)
    c2.set_params(0.0, 10.0, 200.0, -18.0, 4.0, 6.0, 0.0)
    run(c2, tone(-10, 1.5))
    start = c2._env_rms
    silence = np.zeros((1, BLOCK))
    n = 0
    while c2._env_rms > start / math.e and n < 400:
        c2.process(silence)
        n += 1
    tau_release = 51 / 256
    assert abs(n * BLOCK / RATE - tau_release) < 0.15 * tau_release + 0.02, n * BLOCK / RATE


def test_gain_smoother_is_the_plugins_quarter_of_the_attack_coefficient():
    c = Compressor(RATE, 1)
    c.set_params(0.0, 10.0, 200.0, -18.0, 4.0, 6.0, 0.0)
    assert c._ef_a == pytest.approx(math.exp(-1.0 / (RATE * 3 / 256)) * 0.25)      # ga * 0.25
    c.set_params(0.0, 1.5, 200.0, -18.0, 4.0, 6.0, 0.0)
    assert c._ef_a == 0.0                                                          # attack < 2 ms: no smoothing


def test_stereo_is_linked():
    c = Compressor(RATE, 2)
    c.set_params(0.0, 10.0, 200.0, -30.0, 8.0, 4.0, 0.0)
    x = np.vstack([tone(-12, 2.0), tone(-45, 2.0)])
    y = run(c, x)
    g = [rms_db(y[i, -RATE:]) - rms_db(x[i, -RATE:]) for i in (0, 1)]
    assert abs(g[0] - g[1]) < 1e-6 and g[0] < -5.0, g


def test_block_size_independence():
    x = np.random.default_rng(3).standard_normal((2, 9600)) * 0.2
    a = Compressor(RATE, 2); a.set_params(0.5, 10, 150, -20, 5, 6, 3)
    b = Compressor(RATE, 2); b.set_params(0.5, 10, 150, -20, 5, 6, 3)
    ya, yb = run(a, x, 480), run(b, x, 96)
    assert np.allclose(ya, yb, atol=1e-10)


def test_peak_detector_compresses_a_sine_harder_than_rms():
    # a sine's peak is 3 dB above its RMS, so the peak follower sees a louder signal
    def reduction(mix):
        c = Compressor(RATE, 1)
        c.set_params(mix, 10.0, 200.0, -24.0, 4.0, 4.0, 0.0)
        y = run(c, tone(-20, 3.0))
        return rms_db(tone(-20, 1.0)) - rms_db(y[:, -RATE:])

    r0, r5, r1 = reduction(0.0), reduction(0.5), reduction(1.0)
    assert r0 < r5 < r1
    assert 0.5 < r1 - r0 < 5.0
    c = Compressor(RATE, 1)
    c.set_params(0.0, 10.0, 200.0, -24.0, 4.0, 4.0, 0.0)
    run(c, tone(-20, 0.5))
    assert c._env_peak == 0.0                        # an RMS-only compressor never runs the peak follower


def test_silence_and_extremes_stay_finite():
    c = Compressor(RATE, 2)
    c.set_params(0.3, 10, 200, -18, 4, 6, 8)
    for x in (np.zeros((2, 960)), np.full((2, 960), 1e-30), np.full((2, 960), 1e6), np.random.default_rng(0).standard_normal((2, 960)) * 100):
        assert np.all(np.isfinite(c.process(x)))


def test_block_length_must_fit_the_detector_step():
    with pytest.raises(ValueError):
        Compressor(RATE, 1).process(np.zeros((1, 13)))


# ── gate ─────────────────────────────────────────────────────────────────────

def burst(db_rms, start_s, length_s, total_s, freq=800.0):
    x = np.zeros(int(total_s * RATE))
    a, b = int(start_s * RATE), int((start_s + length_s) * RATE)
    t = np.arange(b - a) / RATE
    x[a:b] = db2lin(db_rms) * math.sqrt(2) * np.sin(2 * np.pi * freq * t)
    return x[None, :]


def test_closed_gate_silences_at_90_db_range():
    g = Gate(RATE, threshold_db=-40.0, lookahead_ms=0.0)
    y = run(g, tone(-60, 0.5))
    assert np.max(np.abs(y)) == 0.0 and not g.is_open


def test_range_attenuates_instead_of_muting():
    g = Gate(RATE, threshold_db=-40.0, range_db=-20.0, lookahead_ms=0.0)
    x = tone(-60, 0.5)
    y = run(g, x)
    assert np.allclose(y, x * 0.1, atol=1e-9)


def test_opens_holds_and_closes():
    g = Gate(RATE, threshold_db=-40.0, attack_ms=5.0, hold_ms=50.0, decay_ms=100.0, lookahead_ms=0.0)
    x = burst(-20, 0.2, 0.3, 1.5)
    y = run(g, x)
    env = np.abs(y[0])
    at = lambda s: float(env[int(s * RATE): int((s + 0.002) * RATE)].max())  # noqa: E731
    assert at(0.1) == 0.0                                  # closed before the burst
    assert at(0.2) < at(0.215) <= db2lin(-20) * math.sqrt(2) + 1e-9   # ramping up over ~5 ms
    assert at(0.3) > 0.95 * db2lin(-20) * math.sqrt(2)     # fully open mid-burst
    assert not g.is_open                                   # long after the burst the gate has closed again
    # The detector releases slowly (~208 ms time constant, like the plugin), then hold and decay add to it:
    # a -48 dB probe (too quiet to open the gate itself) is still passed 50 ms after a -20 dB tone ...
    def probe_at(seconds):
        p = np.zeros((1, int(1.6 * RATE)))
        p[0, int(seconds * RATE): int((seconds + 0.01) * RATE)] = 0.004
        return p

    tone_burst = burst(-20, 0.2, 0.1, 1.6)
    open_gate = Gate(RATE, threshold_db=-40.0, attack_ms=5.0, hold_ms=50.0, decay_ms=100.0, lookahead_ms=0.0)
    y = run(open_gate, tone_burst + probe_at(0.35))
    assert np.abs(y[0, int(0.35 * RATE): int(0.36 * RATE)]).max() > 0.0035
    # ... and silenced again well over a second later
    closed_gate = Gate(RATE, threshold_db=-40.0, lookahead_ms=0.0)
    y = run(closed_gate, tone_burst + probe_at(1.45))
    assert np.abs(y[0, int(1.45 * RATE): int(1.46 * RATE)]).max() == 0.0


def test_threshold_decides():
    for level, should_open in ((-30, True), (-37, True), (-45, False), (-60, False)):
        g = Gate(RATE, threshold_db=-40.0, lookahead_ms=0.0)
        y = run(g, tone(level, 0.4, freq=800.0))
        assert (np.abs(y[0, -2000:]).max() > 0) == should_open, level


def test_lookahead_keeps_the_onset():
    x = burst(-15, 0.1, 0.3, 0.6)
    plain = run(Gate(RATE, lookahead_ms=0.0), x)[0]
    ahead = run(Gate(RATE, lookahead_ms=5.0), x)[0]
    look = int(0.005 * RATE)
    onset = int(0.1 * RATE)
    # without lookahead the first 3 ms are ramped; with it the delayed onset arrives with the gate open
    early = slice(onset + look, onset + look + int(0.003 * RATE))
    ref = np.abs(x[0, onset: onset + int(0.003 * RATE)])
    assert np.abs(ahead[early]).max() > 0.9 * ref.max()
    assert np.abs(plain[onset: onset + int(0.003 * RATE)]).max() < 0.7 * ref.max()
    # lookahead is a pure 5 ms delay of the audio
    assert np.allclose(ahead[look + onset + 1000: look + onset + 5000], x[0, onset + 1000: onset + 5000], atol=1e-9)


def test_gate_block_independence_and_shapes():
    x = burst(-20, 0.05, 0.2, 0.6) + burst(-50, 0.3, 0.1, 0.6, freq=300)
    a, b = Gate(RATE), Gate(RATE)
    assert np.allclose(run(a, x, 480), run(b, x, 96), atol=1e-12)
    stereo = np.vstack([x, x * 0.0])
    out = Gate(RATE).process(stereo[:, :480])
    assert out.shape == (1, 480) or out.shape == (2, 480)
    with pytest.raises(ValueError):
        Gate(RATE).process(np.zeros((1, 13)))


# ── limiter ──────────────────────────────────────────────────────────────────

@pytest.mark.usefixtures("limiter_path")
def test_never_exceeds_the_limit():
    lim = Limiter(RATE, 2, limit_db=-1.0)
    rng = np.random.default_rng(5)
    x = np.vstack([tone(0, 1.0, freq=300)[0] * 3.0, rng.standard_normal(RATE) * 0.8])
    x[0, 20000:20050] = 5.0
    y = run(lim, x)
    assert np.max(np.abs(y)) <= lim.limit + 1e-12


@pytest.mark.usefixtures("limiter_path")
def test_in_limit_programme_is_a_pure_delay():
    lim = Limiter(RATE, 2)
    x = np.random.default_rng(6).uniform(-0.5, 0.5, (2, 9600))
    y = run(lim, x)
    assert np.allclose(y[:, lim.delay:], x[:, :-lim.delay], atol=1e-12)
    assert np.all(y[:, :lim.delay] == 0.0)


@pytest.mark.usefixtures("limiter_path")
def test_latency_is_five_milliseconds():
    lim = Limiter(RATE, 1)
    x = np.zeros((1, 2400)); x[0, 0] = 0.5
    y = run(lim, x)
    assert np.argmax(np.abs(y[0])) == lim.delay == 240


@pytest.mark.usefixtures("limiter_path")
def test_gain_recovers_over_the_release_time():
    lim = Limiter(RATE, 1, limit_db=-1.0, release_s=0.1)
    x = np.concatenate([tone(6, 0.2), tone(-30, 1.0)], axis=1)
    y = run(lim, x)
    quiet = tone(-30, 1.0)[0]
    tail = y[0, -1000:]
    assert np.allclose(tail, np.roll(x[0], lim.delay)[-1000:], atol=1e-4)
    # right after the loud part the quiet tone is still attenuated, and it recovers
    just_after = y[0, int(0.22 * RATE): int(0.23 * RATE)]
    ref_after = x[0, int(0.22 * RATE) - lim.delay: int(0.23 * RATE) - lim.delay]
    assert np.abs(just_after).max() < 0.98 * np.abs(ref_after).max()
    assert quiet.size


@pytest.mark.usefixtures("limiter_path")
def test_attenuation_recovers_towards_unity_without_overshoot():
    lim = Limiter(RATE, 1, release_s=0.1)
    x = np.concatenate([tone(6, 0.1), tone(-40, 0.6)], axis=1)
    seen = []
    for i in range(0, x.shape[1], BLOCK):
        lim.process(x[:, i:i + BLOCK])
        seen.append(lim._atten)
    assert max(seen) <= 1.0
    assert min(seen) < 0.6                       # it did clamp down on the +6 dB burst
    # exponential recovery with ~release_s: 0.5 s after the burst less than 1% remains
    assert 1.0 - lim._atten < 0.01


@pytest.mark.usefixtures("limiter_path")
def test_limiting_is_not_clipping():
    lim = Limiter(RATE, 1, limit_db=-1.0)
    x = tone(3, 1.0, freq=200)           # peaks at ~+6 dBFS
    y = run(lim, x)[0, RATE // 2:]
    hard = np.mean(np.abs(y) >= lim.limit - 1e-12)
    assert hard < 0.05, hard
    assert np.abs(y).max() > 0.75 * lim.limit      # and it still uses the headroom (not over-attenuating)


@pytest.mark.usefixtures("limiter_path")
def test_state_is_independent_of_block_size():
    rng = np.random.default_rng(8)
    x = np.vstack([rng.standard_normal(14400), rng.standard_normal(14400)]) * 1.5
    a, b, c = Limiter(RATE, 2), Limiter(RATE, 2), Limiter(RATE, 2)
    ya = run(a, x, 480)
    yb = run(b, x, 128)
    yc = np.concatenate([c.process(x[:, i:j]) for i, j in zip([0, 17, 480, 481, 5000, 9000], [17, 480, 481, 5000, 9000, 14400])], axis=1)
    assert np.allclose(ya, yb, atol=1e-12) and np.allclose(ya, yc, atol=1e-12)


@pytest.mark.usefixtures("limiter_path")
def test_input_gain():
    lim = Limiter(RATE, 1, limit_db=-1.0, input_gain_db=-6.0)
    x = tone(-12, 0.5)
    y = run(lim, x)
    assert np.allclose(y[0, lim.delay + 100:], x[0, 100:-lim.delay] * db2lin(-6.0), atol=1e-9)


@pytest.mark.usefixtures("limiter_path")
def test_multichannel_block_is_limited_as_one_unit():
    lim = Limiter(RATE, 8)
    x = np.zeros((8, 4800)); x[3] = tone(6, 0.1)[0]
    y = run(lim, x)
    assert np.max(np.abs(y)) <= lim.limit + 1e-12


# ── limiter against a literal port of fast_lookahead_limiter_1913 ────────────

class ReferenceLimiter:
    """The plugin's run loop, statement by statement (two channels, one sample at a time)."""

    def __init__(self, fs, limit_db, release, ingain_db):
        self.fs, self.release = float(fs), release
        self.max = 10 ** (limit_db / 20)
        self.trim = 10 ** (ingain_db / 20)
        self.delay = int(0.005 * fs)
        self.chunk_size = int(fs / 2000)
        self.buf = [[0.0, 0.0] for _ in range(16384)]
        self.pos = 0
        self.chunks = [0.0] * 16
        self.chunk_pos = self.chunk_num = 0
        self.peak, self.atten, self.atten_lp, self.delta = 0.0, 1.0, 1.0, 0.0

    def run(self, x):
        out = np.zeros_like(x)
        for pos in range(x.shape[1]):
            if self.chunk_pos == self.chunk_size:
                self.delta = (1.0 - self.atten) / (self.fs * self.release)
                for i in range(10):
                    p = (self.chunk_num - 9 + i) & 15
                    c = self.chunks[p]
                    this = ((self.max / c if c > 0 else float("inf")) - self.atten) / ((i + 1) * self.fs * 0.0005 + 1.0)
                    if this < self.delta:
                        self.delta = this
                self.chunks[self.chunk_num & 15] = self.peak
                self.chunk_num += 1
                self.peak = 0.0
                self.chunk_pos = 0
            else:
                self.chunk_pos += 1
            l, r = x[0, pos], x[1, pos]
            self.buf[self.pos & 16383] = [l * self.trim + 1e-30, r * self.trim + 1e-30]
            sig = max(abs(l), abs(r)) + 1e-30
            if sig * self.trim > self.peak:
                self.peak = sig * self.trim
            self.atten += self.delta
            self.atten_lp = self.atten * 0.1 + self.atten_lp * 0.9
            if self.delta > 0.0 and self.atten > 1.0:
                self.atten, self.delta = 1.0, 0.0
            d = self.buf[(self.pos - self.delay) & 16383]
            for ch in (0, 1):
                v = d[ch] * self.atten_lp
                out[ch, pos] = max(-self.max, min(self.max, v))
            self.pos += 1
        return out


def _limiter_programme():
    rng = np.random.default_rng(21)
    n = np.arange(RATE * 3 // 2)
    env = np.where(n < 9000, 0.05, np.where(n < 21000, 2.4, np.where(n < 40000, 0.1, 1.3)))
    left = env * np.sin(2 * np.pi * 220 * n / RATE) + rng.standard_normal(n.size) * 0.02
    right = env * 0.8 * np.sin(2 * np.pi * 330 * n / RATE) + rng.standard_normal(n.size) * 0.02
    left[60000:60040] = 4.0                                   # a click
    return np.vstack([left, right])


@pytest.mark.usefixtures("limiter_path")
@pytest.mark.parametrize("limit_db,release,ingain", [(-1.0, 0.1, 0.0), (-6.0, 0.5, -3.0), (-0.1, 0.05, 6.0), (-1.0, 0.0002, 0.0)])  # the last one: unity is reached mid-chunk
def test_limiter_matches_the_c_code_sample_for_sample(limit_db, release, ingain):
    x = _limiter_programme()
    ref = ReferenceLimiter(RATE, limit_db, release, ingain).run(x)
    lim = Limiter(RATE, 2, limit_db=limit_db, release_s=release, input_gain_db=ingain)
    got = np.concatenate([lim.process(x[:, i:i + BLOCK]) for i in range(0, x.shape[1] - x.shape[1] % BLOCK, BLOCK)], axis=1)
    want = ref[:, :got.shape[1]]
    assert np.max(np.abs(want)) > 0.5
    # (the C code feeds its smoothing filter the un-clamped step on the one sample that crosses unity,
    # which only matters for a sub-millisecond release; the real 0.1 s release never gets there)
    tol = 2e-4 if release < 0.001 else 1e-6
    assert np.allclose(got, want, atol=tol, rtol=tol), np.max(np.abs(got - want))


def test_limiter_compiled_and_numpy_paths_are_bit_identical(monkeypatch):
    if dynamics._LIMITER_KERNEL is None:
        pytest.skip("numba is not installed")
    x = _limiter_programme()
    outs = []
    for kernel in (None, dynamics._LIMITER_KERNEL):
        monkeypatch.setattr(dynamics, "_LIMITER_KERNEL", kernel)
        lim = Limiter(RATE, 2, limit_db=-3.0, release_s=0.05)
        # blocks that are not a multiple of the 25-sample chunk, so chunk phase is carried across calls
        sizes = [960, 777, 1, 480, 1919, 25, 26, 3000]
        parts, pos = [], 0
        while pos + max(sizes) < x.shape[1]:
            for n in sizes:
                parts.append(lim.process(x[:, pos:pos + n]))
                pos += n
        outs.append((np.concatenate(parts, axis=1), lim._atten, lim._delta, lim._peak, lim._chunk_num,
                     lim._chunks.copy()))
    assert np.array_equal(outs[0][0], outs[1][0])
    assert outs[0][1:5] == outs[1][1:5]
    assert np.array_equal(outs[0][5], outs[1][5])


def test_limiter_failing_kernel_falls_back_without_losing_the_block(monkeypatch):
    calls = []

    def broken(*args):
        calls.append(1)
        raise RuntimeError("compile error")
    monkeypatch.setattr(dynamics, "_LIMITER_KERNEL", broken)
    x = _limiter_programme()[:, :4800]
    got = Limiter(RATE, 2).process(x)
    assert calls == [1] and dynamics._LIMITER_KERNEL is None
    want = Limiter(RATE, 2).process(x)                                  # the numpy path now
    assert np.array_equal(got, want)
