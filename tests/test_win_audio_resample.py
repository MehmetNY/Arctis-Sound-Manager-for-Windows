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
