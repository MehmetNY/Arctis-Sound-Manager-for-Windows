"""The drift-compensation resampler: exact at unity, flat at every fractional position, and seamless."""
from __future__ import annotations

import numpy as np
import pytest

from arctis_sound_manager.win.audio.resample import HISTORY, LOOKAHEAD, StreamResampler

RATE = 48000


def drain(r, total, chunk, ratio=1.0):
    out = []
    got = 0
    while got < total:
        y = r.pull(chunk, ratio)
        if y is None:
            break
        out.append(y)
        got += chunk
    return np.concatenate(out, axis=1) if out else np.zeros((r.channels, 0))


def test_unity_ratio_is_a_bit_exact_copy_and_needs_only_the_lookahead():
    x = np.random.default_rng(0).standard_normal((2, 4800))
    r = StreamResampler(2)
    r.push(x[:, :480])
    assert r.pull(480) is None                                   # 16 samples of lookahead are missing
    r.push(x[:, 480:2000])
    y = drain(r, 1900, 480)
    assert np.array_equal(y, x[:, :y.shape[1]])
    assert y.shape[1] == 1920 - 480 or y.shape[1] >= 1440       # all that the lookahead allows


def test_unity_ratio_with_a_half_sample_offset_is_a_flat_fractional_delay():
    t = np.arange(RATE) / RATE
    for freq in (100, 1000, 8000, 15000, 19000):
        x = np.sin(2 * np.pi * freq * t)
        r = StreamResampler(1)
        r.push(x[None, :])
        r._pos += 0.5                                            # sit between two samples for the whole run
        y = drain(r, 40000, 480)[0]
        want = np.sin(2 * np.pi * freq * (np.arange(y.size) + 0.5) / RATE)
        err = y[100:] - want[100:]
        assert 20 * np.log10(np.std(err) / np.std(want)) < -70, freq     # level and phase both right


@pytest.mark.parametrize("ratio", [0.9995, 1.0003, 1.001])
@pytest.mark.parametrize("f", [1000.0, 15000.0, 19000.0])
def test_a_tone_is_shifted_by_the_ratio_without_distortion(ratio, f):
    t = np.arange(RATE) / RATE
    x = np.sin(2 * np.pi * f * t)
    r = StreamResampler(1)
    r.push(x[None, :])
    y = drain(r, 40000, 480, ratio)[0]
    want = np.sin(2 * np.pi * f * (np.arange(y.size) * ratio) / RATE)
    assert 20 * np.log10(np.std(y[100:] - want[100:]) / np.std(want)) < -85      # measured: about -94 dB


def test_chunking_of_input_and_output_does_not_matter():
    x = np.random.default_rng(1).standard_normal((2, 20000))
    a, b = StreamResampler(2), StreamResampler(2)
    a.push(x)
    ya = drain(a, 16000, 480, 1.0004)
    out = []
    pos = 0
    for step, n in zip([700, 111, 5000, 9000, 5189], [96, 960, 480, 96, 480]):
        b.push(x[:, pos:pos + step]); pos += step
        while (y := b.pull(n, 1.0004)) is not None:
            out.append(y)
    yb = np.concatenate(out, axis=1)
    m = min(ya.shape[1], yb.shape[1])
    assert m > 8000
    # the same positions are visited whatever the chunking, so every sample agrees
    assert np.allclose(ya[:, :m], yb[:, :m], atol=1e-9)


def test_the_read_position_advances_by_exactly_n_times_ratio():
    r = StreamResampler(1)
    r.push(np.zeros((1, 10000)))
    start = r._pos
    consumed = 0.0
    for n, ratio in ((480, 1.0002), (960, 0.9998), (96, 1.001)):
        before = r._buf.shape[1] - r._pos            # how far ahead of the read position the buffer reaches
        r.pull(n, ratio)
        consumed += n * ratio
        assert (r._buf.shape[1] - r._pos) == pytest.approx(before - n * ratio, abs=1e-9)
    assert consumed > 1500 and start == HISTORY


def test_ahead_reports_the_fill_level():
    r = StreamResampler(1)
    assert r.ahead == 0
    r.push(np.zeros((1, 1000)))
    assert r.ahead == 1000
    r.pull(480)
    assert r.ahead == pytest.approx(520)


def test_multichannel_channels_are_independent():
    t = np.arange(9600) / RATE
    x = np.vstack([np.sin(2 * np.pi * 500 * t), np.zeros_like(t), np.sin(2 * np.pi * 3000 * t)])
    r = StreamResampler(3)
    r.push(x)
    y = drain(r, 8000, 480, 1.0003)
    assert np.all(y[1] == 0.0)
    assert np.std(y[0]) > 0.5 and np.std(y[2]) > 0.5


def test_reset_forgets_everything():
    r = StreamResampler(1)
    r.push(np.ones((1, 2000)))
    r.pull(480, 1.001)
    r.reset()
    assert r.ahead == 0 and r._pos == HISTORY
    assert r.pull(1) is None


def test_lookahead_constants_match_the_filter():
    assert HISTORY + LOOKAHEAD + 1 == 32


def test_a_pull_needs_exactly_the_filters_lookahead():
    for n, ratio in ((480, 1.0003), (96, 0.9997), (960, 1.001)):
        last = HISTORY + (n - 1) * ratio
        required = int(np.floor(last)) + LOOKAHEAD + 1 - HISTORY       # samples to push in total
        r = StreamResampler(1)
        r.push(np.ones((1, required - 1)))
        assert r.pull(n, ratio) is None
        r.push(np.ones((1, 1)))
        assert r.pull(n, ratio) is not None


def test_history_is_kept_for_the_next_pull():
    x = np.random.default_rng(3).standard_normal((1, 6000))
    whole, parts = StreamResampler(1), StreamResampler(1)
    whole.push(x)
    parts.push(x[:, :700])
    out = [parts.pull(480, 1.0007)]
    parts.push(x[:, 700:])
    out += [parts.pull(480, 1.0007) for _ in range(6)]
    ref = [whole.pull(480, 1.0007) for _ in range(7)]
    assert np.allclose(np.concatenate(out, axis=1), np.concatenate(ref, axis=1), atol=1e-12)


# ── the compiled kernel and the silence shortcut ─────────────────────────────

from arctis_sound_manager.win.audio import fast  # noqa: E402
from arctis_sound_manager.win.audio import resample as rs  # noqa: E402


def _stream(channels=2, length=4000, seed=3):
    return np.random.default_rng(seed).standard_normal((channels, length)) * 0.2


@pytest.mark.skipif(not fast.HAVE_NUMBA, reason="numba is not installed")
@pytest.mark.parametrize("ratio", [1.0, 1.00003, 0.99991, 1.0004, 0.5, 1.37])
@pytest.mark.parametrize("channels,n", [(1, 480), (2, 480), (2, 960), (2, 37)])
def test_the_compiled_kernel_gives_what_the_numpy_code_gives(ratio, channels, n):
    x = _stream(channels, 6000)
    fast_r, slow_r = rs.StreamResampler(channels), rs.StreamResampler(channels)
    fast_r.push(x)
    slow_r.push(x)
    assert rs._READ_KERNEL is not None
    for _ in range(4):
        a = fast_r.pull(n, ratio)
        saved, rs._READ_KERNEL = rs._READ_KERNEL, None             # the same pull through the numpy path
        try:
            b = slow_r.pull(n, ratio)
        finally:
            rs._READ_KERNEL = saved
        assert a is not None and b is not None
        assert np.max(np.abs(a - b)) < 1e-12
    assert fast_r._pos == slow_r._pos


def test_a_kernel_that_fails_is_switched_off_and_the_numpy_path_takes_over(monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(rs, "_READ_KERNEL", broken)
    r = rs.StreamResampler(2)
    x = _stream(2, 3000)
    r.push(x)
    out = r.pull(480, 1.00003)
    reference = rs.StreamResampler(2)
    reference.push(x)
    monkeypatch.setattr(rs, "_READ_KERNEL", None)
    assert np.allclose(out, reference.pull(480, 1.00003), atol=1e-12)
    assert rs._READ_KERNEL is None


def test_silence_is_returned_as_silence_without_filtering(monkeypatch):
    calls = []
    monkeypatch.setattr(rs, "_READ_KERNEL", lambda *a: calls.append(1))
    monkeypatch.setattr(rs.StreamResampler, "_interpolate_numpy", lambda *a: calls.append(1))
    r = rs.StreamResampler(2)
    r.push(np.zeros((2, 3000)))
    for _ in range(5):
        out = r.pull(480, 1.00003)
        assert out.shape == (2, 480) and not out.any()
    assert calls == []
    assert r._pos < 600                                            # and the position still advanced and the buffer was trimmed


def test_audio_after_silence_is_filtered_again_and_the_filter_sees_the_silence_before_it():
    r = rs.StreamResampler(1)
    ref = rs.StreamResampler(1)
    x = np.concatenate([np.zeros((1, 1500)), _stream(1, 1500)], axis=1)
    for chunk in (x[:, :700], x[:, 700:1700], x[:, 1700:]):
        r.push(chunk)
    ref.push(x)
    got = np.concatenate([r.pull(240, 1.00005) for _ in range(10)], axis=1)
    want = np.concatenate([ref.pull(240, 1.00005) for _ in range(10)], axis=1)
    assert np.allclose(got, want, atol=1e-12)
    assert not got[:, :1200].any() and got[:, 1500:].any()


def test_one_nonzero_sample_inside_the_filter_window_defeats_the_shortcut():
    r = rs.StreamResampler(1)
    x = np.zeros((1, 2000))
    x[0, 520] = 1.0                                               # one click, in the second block's window only
    r.push(x)
    first = r.pull(480, 1.00003)
    second = r.pull(480, 1.00003)
    assert not first.any() or abs(first).max() < 1e-3             # at most the filter's very first tail
    assert second.any()
