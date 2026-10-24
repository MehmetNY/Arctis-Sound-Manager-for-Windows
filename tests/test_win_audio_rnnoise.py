"""RNNoise wrapper: the voice-activity gate (scripted probabilities) and the real library when present."""
from __future__ import annotations

import numpy as np
import pytest

from arctis_sound_manager.win.audio import rnnoise
from arctis_sound_manager.win.audio.rnnoise import FRAME, RNNoise, RNNoiseUnavailable


class FakeNative:
    """Passes audio through and reports the scripted speech probabilities, one per frame."""

    def __init__(self, probs):
        self.probs = list(probs)
        self.created = self.destroyed = 0
        self.calls = 0

    def create(self):
        self.created += 1
        return object()

    def destroy(self, state):
        self.destroyed += 1

    def process(self, state, frame):
        vad = self.probs[self.calls] if self.calls < len(self.probs) else 0.0
        self.calls += 1
        return vad


def frames(n, value=0.5):
    return np.full(n * FRAME, value)


def frame_levels(y):
    """The output level at the end of every frame: the gate's state (open = the input level, shut = 0)."""
    return [float(abs(y[i + FRAME - 1])) for i in range(0, len(y), FRAME)]


def test_frames_below_the_threshold_are_silenced_and_the_rest_passes_unchanged():
    r = RNNoise(vad_threshold=0.5, grace_ms=0.0, native=FakeNative([0.9, 0.9, 0.1, 0.1, 0.9]))
    y = r.process(frames(5))
    lv = frame_levels(y)
    assert lv[0] == lv[1] == pytest.approx(0.5, abs=1e-6)       # float32 round trip only
    assert lv[3] == 0.0
    assert lv[4] == pytest.approx(0.5, abs=1e-6)
    assert np.allclose(y[:2 * FRAME], 0.5, atol=1e-6)


def test_the_gate_ramps_over_the_frame_instead_of_cutting():
    r = RNNoise(vad_threshold=0.5, grace_ms=0.0, native=FakeNative([0.9, 0.1, 0.9]))
    y = r.process(frames(3))
    closing = y[FRAME:2 * FRAME]
    assert closing[0] > 0.49 and closing[-1] == pytest.approx(0.0, abs=1e-9)
    assert np.all(np.diff(closing) < 0)                         # monotone fade, no step
    opening = y[2 * FRAME:]
    assert opening[0] < 0.01 and opening[-1] == pytest.approx(0.5, abs=1e-6)
    assert np.all(np.diff(opening) > 0)
    assert np.max(np.abs(np.diff(y))) < 0.01                    # nowhere a jump


def test_grace_period_keeps_the_gate_open_after_speech():
    probs = [0.9] + [0.0] * 8
    r = RNNoise(vad_threshold=0.5, grace_ms=30.0, native=FakeNative(probs))
    lv = frame_levels(r.process(frames(9)))
    assert [l > 0.4 for l in lv[:4]] == [True, True, True, True]    # speech frame + 3 grace frames
    assert lv[4:] == [0.0] * 5                                      # then it closes


def test_grace_renews_on_every_speech_frame():
    probs = [0.9, 0, 0, 0.9, 0, 0, 0, 0]
    r = RNNoise(vad_threshold=0.5, grace_ms=20.0, native=FakeNative(probs))
    lv = frame_levels(r.process(frames(8)))
    assert all(l > 0.4 for l in lv[:6])
    assert lv[7] == 0.0


def test_zero_threshold_never_gates():
    r = RNNoise(vad_threshold=0.0, grace_ms=0.0, native=FakeNative([0.0] * 6))
    assert np.allclose(r.process(frames(6)), 0.5, atol=1e-6)


def test_threshold_one_gates_everything_but_certainty():
    r = RNNoise(vad_threshold=1.0, grace_ms=0.0, native=FakeNative([0.99, 1.0, 0.99]))
    lv = frame_levels(r.process(frames(3)))
    assert lv[0] == 0.0 and lv[1] > 0.4 and lv[2] == 0.0


def test_retroactive_grace_releases_frames_before_speech_at_the_cost_of_latency():
    probs = [0.0, 0.0, 0.0, 0.9, 0.0, 0.0, 0.0, 0.0]
    r = RNNoise(vad_threshold=0.5, grace_ms=0.0, retro_ms=20.0, native=FakeNative(probs))
    assert r.latency_frames == 2 * FRAME
    x = np.concatenate([np.full(FRAME, v) for v in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)])
    lv = frame_levels(r.process(x))
    # output frame k is input frame k-2; frames 1 and 2 (0.2, 0.3) are rescued by the speech in frame 3 (0.4)
    assert lv == pytest.approx([0.0, 0.0, 0.0, 0.2, 0.3, 0.4, 0.0, 0.0], abs=1e-6)


def test_block_sizes_must_be_whole_frames():
    r = RNNoise(native=FakeNative([]))
    with pytest.raises(ValueError):
        r.process(np.zeros(500))
    assert r.process(np.zeros(2 * FRAME)).shape == (2 * FRAME,)


def test_reset_recreates_the_native_state_and_clears_the_gate():
    fake = FakeNative([0.9] + [0.0] * 20)
    r = RNNoise(vad_threshold=0.5, grace_ms=100.0, native=fake)
    r.process(frames(2))
    r.reset()
    assert fake.created == 2 and fake.destroyed == 1
    fake.probs, fake.calls = [0.0] * 5, 0
    assert frame_levels(r.process(frames(3))) == [0.0, 0.0, 0.0]    # grace is gone (the gate had been re-armed open)
    r.close()
    assert fake.destroyed == 2
    r.close()                                                      # idempotent
    assert fake.destroyed == 2


def test_the_library_is_found_through_the_environment(tmp_path, monkeypatch):
    lib = tmp_path / "elsewhere.dll"
    lib.write_bytes(b"x")
    monkeypatch.setenv("ASM_RNNOISE_LIB", str(lib))
    assert rnnoise.find_library() == lib
    monkeypatch.setenv("ASM_RNNOISE_LIB", str(tmp_path / "missing.dll"))
    assert rnnoise.find_library() != tmp_path / "missing.dll"


def test_a_file_that_is_not_a_library_is_reported_as_unavailable(tmp_path):
    junk = tmp_path / "rnnoise.dll"
    junk.write_bytes(b"MZ not really")
    with pytest.raises(RNNoiseUnavailable):
        rnnoise.NativeRNNoise(junk)


# ── the real library (skipped where it is not installed) ─────────────────────

real = pytest.mark.skipif(not rnnoise.available(), reason="RNNoise library (pyrnnoise) not installed")
rms = lambda a: float(np.sqrt(np.mean(np.square(a))))


def _voice(n=48000):
    t = np.arange(n) / 48000
    f0 = 120 + 20 * np.sin(2 * np.pi * 3 * t)
    ph = 2 * np.pi * np.cumsum(f0) / 48000
    return sum(np.sin(k * ph) / k for k in range(1, 25)) * 0.1 * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))


@real
def test_real_library_removes_broadband_noise():
    noise = np.random.default_rng(0).standard_normal(48000) * 0.05
    y = RNNoise(vad_threshold=0.0).process(noise)
    assert 20 * np.log10(rms(noise[9600:]) / rms(y[9600:])) > 30


@real
def test_real_library_keeps_a_voice_like_signal_and_reports_speech():
    r = RNNoise(vad_threshold=0.0)
    v = _voice()
    vads, out = [], []
    for i in range(0, v.size, FRAME):
        out.append(r.process(v[i:i + FRAME]))
        vads.append(r.last_vad)
    y = np.concatenate(out)
    assert np.mean(vads[20:]) > 0.5
    assert rms(y[9600:]) > 0.3 * rms(v[9600:])


@real
def test_real_library_vad_threshold_gates_noise_but_not_voice():
    mixed = _voice() + np.random.default_rng(1).standard_normal(48000) * 0.002
    for thr, expect_open in ((0.5, True),):
        y = RNNoise(vad_threshold=thr, grace_ms=0.0).process(mixed)
        assert (rms(y[24000:]) > 0.2 * rms(mixed[24000:])) == expect_open
    hiss = np.random.default_rng(2).standard_normal(48000) * 0.01
    y = RNNoise(vad_threshold=0.5, grace_ms=0.0).process(hiss)
    assert rms(y[9600:]) < 1e-6                                    # the gate is shut: digital silence


@real
def test_real_library_is_deterministic_and_instances_are_independent():
    v = _voice(9600)
    a, b = RNNoise(vad_threshold=0.0), RNNoise(vad_threshold=0.0)
    ya = np.concatenate([a.process(v[i:i + FRAME]) for i in range(0, 9600, FRAME)])
    yb = np.concatenate([b.process(v[i:i + FRAME]) for i in range(0, 9600, FRAME)])
    assert np.array_equal(ya, yb)
    a.reset()
    ya2 = np.concatenate([a.process(v[i:i + FRAME]) for i in range(0, 9600, FRAME)])
    assert np.array_equal(ya, ya2)
