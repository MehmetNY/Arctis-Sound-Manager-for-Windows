"""Partitioned convolution against direct convolution, and the HeSuVi HRIR plumbing."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile
import warnings

warnings.filterwarnings("ignore", category=wavfile.WavFileWarning)
from scipy.signal import fftconvolve

from arctis_sound_manager.win.audio.convolver import (
    PartitionedConvolver, expand_hrir, hrir_matrix, load_hrir, resample_ir)

HRIR_DIR = Path(__file__).resolve().parents[1] / "src" / "arctis_sound_manager" / "hrir_assets"
B = 480


def stream(conv, x):
    n = x.shape[1] - x.shape[1] % conv.block
    return np.concatenate([conv.process(x[:, i:i + conv.block]) for i in range(0, n, conv.block)], axis=1)


def reference(irs, x):
    """out[o] = sum_i conv(x[i], irs[i, o]) by FFT convolution, cut to the input length."""
    n_in, n_out, _ = irs.shape
    out = np.zeros((n_out, x.shape[1]))
    for o in range(n_out):
        for i in range(n_in):
            out[o] += fftconvolve(x[i], irs[i, o])[:x.shape[1]]
    return out


@pytest.mark.parametrize("length", [1, 4, 100, 479, 480, 481, 960, 5147, 16802])
def test_matches_direct_convolution(length):
    rng = np.random.default_rng(length)
    irs = rng.standard_normal((3, 2, length)) * 0.05
    x = rng.standard_normal((3, B * 40)) * 0.3
    conv = PartitionedConvolver(irs, B)
    got = stream(conv, x)
    assert np.allclose(got, reference(irs, x)[:, :got.shape[1]], atol=1e-9)


def test_unit_impulse_is_zero_latency():
    ir = np.zeros((1, 1, 600)); ir[0, 0, 0] = 1.0
    conv = PartitionedConvolver(ir, B)
    x = np.random.default_rng(0).standard_normal((1, B * 4))
    assert np.allclose(stream(conv, x), x, atol=1e-12)


def test_delayed_impulse_delays_by_exactly_that_many_samples():
    ir = np.zeros((1, 1, 2000)); ir[0, 0, 1234] = 1.0     # lands in the third partition
    conv = PartitionedConvolver(ir, B)
    x = np.random.default_rng(1).standard_normal((1, B * 10))
    y = stream(conv, x)
    assert np.allclose(y[0, 1234:], x[0, :-1234], atol=1e-12) and np.all(np.abs(y[0, :1234]) < 1e-12)


def test_channels_do_not_leak_into_each_other():
    irs = np.zeros((2, 2, 50)); irs[0, 0, 0] = 1.0; irs[1, 1, 3] = 0.5
    conv = PartitionedConvolver(irs, B)
    x = np.zeros((2, B * 3)); x[0, 10] = 1.0
    y = stream(conv, x)
    assert y[0, 10] == pytest.approx(1.0)
    assert np.all(np.abs(y[1]) < 1e-12)                     # nothing reaches the other output
    assert np.count_nonzero(np.abs(y[0]) > 1e-12) == 1       # and the impulse is not smeared


def test_reset_clears_the_tail():
    rng = np.random.default_rng(2)
    conv = PartitionedConvolver(rng.standard_normal((1, 1, 3000)) * 0.1, B)
    conv.process(rng.standard_normal((1, B)))
    conv.reset()
    assert np.all(conv.process(np.zeros((1, B))) == 0.0)


def test_wrong_block_shape_is_rejected():
    conv = PartitionedConvolver(np.ones((2, 1, 10)), B)
    with pytest.raises(ValueError):
        conv.process(np.zeros((2, B - 1)))
    with pytest.raises(ValueError):
        PartitionedConvolver(np.ones((2, 10)), B)


# ── HRIR files ───────────────────────────────────────────────────────────────

def test_seven_channel_files_expand_by_mirroring_exactly_like_the_14_channel_ones():
    # atmos.wav (14 ch) is the mirror image of its own first seven channels; expanding the 7-channel
    # variant with the same rule must give the same left/right structure.
    _, d14 = wavfile.read(HRIR_DIR / "atmos.wav")
    full = expand_hrir(d14)
    assert np.allclose(full, d14[:, :14].T)
    for k in range(7):                                           # the recorded right-hand side is the mirror image
        assert np.corrcoef(full[k], full[k + 7])[0, 1] > 0.95, k
    _, d7 = wavfile.read(HRIR_DIR / "atmos-.wav")
    exp = expand_hrir(d7)
    assert exp.shape == (14, d7.shape[0])
    assert np.allclose(exp[7:13], exp[:6]) and np.allclose(exp[13], exp[6])


def test_ipsilateral_arrives_before_contralateral_in_the_bundled_files():
    # FL_L (0) is the near ear of the front-left speaker, FL_R (1) the far one; FR mirrors that
    for name in ("atmos", "gsx", "dh-", "hear", "oal_dflt"):
        h = load_hrir(HRIR_DIR / f"{name}.wav")
        def onset(x):
            a = np.abs(x)
            return int(np.argmax(a > 0.2 * a.max()))
        assert onset(h[0]) <= onset(h[1]), name       # FL: left ear first
        assert onset(h[7]) <= onset(h[8]), name       # FR: right ear (FR_R) first


def test_every_bundled_hrir_loads_as_14_channels_at_48k():
    files = sorted(HRIR_DIR.glob("*.wav"))
    assert len(files) > 40
    for f in files:
        h = load_hrir(f, 48000)
        assert h.shape[0] == 14 and h.shape[1] >= 1 and np.all(np.isfinite(h)), f.name
        assert np.max(np.abs(h)) > 0, f.name


def test_resampling_scales_the_length():
    ir = np.random.default_rng(3).standard_normal((2, 4410))
    up = resample_ir(ir, 44100, 48000)
    assert up.shape == (2, 4800)
    assert resample_ir(ir, 48000, 48000) is ir
    down = resample_ir(ir, 48000, 44100)
    assert down.shape[1] == 4052                       # ceil(4410 * 147 / 160)


def test_loading_converts_other_rates_and_integer_formats(tmp_path):
    t = np.arange(1000) / 44100.0
    pcm = (np.sin(2 * np.pi * 1000 * t)[:, None] * np.ones((1, 7)) * 20000).astype(np.int16)
    path = tmp_path / "x.wav"
    wavfile.write(path, 44100, pcm)
    h = load_hrir(path, 48000)
    assert h.shape == (14, 1089)                       # ceil(1000 * 160 / 147)
    assert 0.5 < np.max(np.abs(h)) < 0.7           # 20000 / 32768 ~ 0.61


def test_matrix_wiring_matches_the_linux_hesuvi_graph():
    from arctis_sound_manager import sonar_to_pipewire as s2p

    h = np.arange(14)[:, None] * np.ones((1, 4)) + np.arange(4)[None, :] * 0.01     # channel k carries value k
    m = hrir_matrix(h)
    assert m.shape == (8, 2, 4)
    order = list(s2p._HESUVI_CHANNELS)                       # FL FR FC LFE RL RR SL SR
    link_for = {}
    for conv, mixer, _ in s2p._HESUVI_CONV_MIX_LINKS:        # which convolver feeds which ear
        speaker = conv[4:].split("_")[0]
        ear = 0 if mixer == "mixL" else 1
        link_for[(speaker, ear)] = dict(s2p._HESUVI_CONVOLVERS)[conv]
    for speaker in order:
        for ear in (0, 1):
            assert m[order.index(speaker), ear, 0] == link_for[(speaker, ear)], (speaker, ear)


def test_virtual_surround_end_to_end_is_the_sum_of_eight_convolutions():
    rng = np.random.default_rng(4)
    h = load_hrir(HRIR_DIR / "gsx.wav")
    m = hrir_matrix(h)
    x = rng.standard_normal((8, B * 12)) * 0.2
    conv = PartitionedConvolver(m, B)
    got = stream(conv, x)
    want = reference(m, x)[:, :got.shape[1]]
    assert np.allclose(got, want, atol=1e-8)


def _gappy_input(rng, blocks):
    x = rng.standard_normal((4, B * blocks)) * 0.3
    x[0, B * 6:B * 60] = 0.0                    # long gap, then it comes back
    x[1, B * 20:B * 21] = 0.0                   # one silent block in the middle of a signal
    x[2] = 0.0                                  # never active
    x[3, :B * 30] = 0.0                         # starts late
    return x


@pytest.mark.parametrize("length", [1, 479, 480, 1500, 5147])
def test_silent_inputs_are_skipped_without_changing_the_result(length):
    rng = np.random.default_rng(length + 7)
    irs = rng.standard_normal((4, 2, length)) * 0.05
    x = _gappy_input(rng, 90)
    conv = PartitionedConvolver(irs, B)
    got = stream(conv, x)
    assert np.allclose(got, reference(irs, x)[:, :got.shape[1]], atol=1e-9)
    assert conv._subsets, "the sparse path was never taken"


def test_a_burst_ringing_out_is_not_cut_by_the_skip():
    irs = np.zeros((2, 1, 3000)); irs[:, 0, 2900] = 1.0          # a very late arrival: 7 partitions away
    conv = PartitionedConvolver(irs, B)
    x = np.zeros((2, B * 20)); x[0, 5] = 1.0
    y = stream(conv, x)
    assert y[0, 2905] == pytest.approx(1.0, abs=1e-9) and np.sum(np.abs(y)) == pytest.approx(1.0, abs=1e-6)


def test_all_silent_input_returns_zeros_and_costs_no_transform():
    conv = PartitionedConvolver(np.ones((3, 2, 100)) * 0.01, B)
    for _ in range(20):
        y = conv.process(np.zeros((3, B)))
    assert y.shape == (2, B) and not y.any()
    assert conv._quiet.tolist() == [conv.partitions + 2] * 3
