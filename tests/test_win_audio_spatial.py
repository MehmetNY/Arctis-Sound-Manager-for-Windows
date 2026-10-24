"""The virtual-surround chain: wiring, Immersion, Distance, Boost compensation, HRIR swap, downmix."""
from __future__ import annotations

import time
import warnings
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile
from scipy.signal import fftconvolve, sosfilt

from arctis_sound_manager.win.audio import biquad
from arctis_sound_manager.win.audio.convolver import hrir_matrix, load_hrir
from arctis_sound_manager.win.audio.dynamics import db2lin
from arctis_sound_manager.win.audio.reverb import PlateReverb
from arctis_sound_manager.win.audio.spatial import VirtualSurround, downmix_to_stereo

warnings.filterwarnings("ignore", category=wavfile.WavFileWarning)
HRIR_DIR = Path(__file__).resolve().parents[1] / "src" / "arctis_sound_manager" / "hrir_assets"
RATE, B = 48000, 480


def run(vs, x):
    n = x.shape[1] - x.shape[1] % B
    return np.concatenate([vs.process(x[:, i:i + B]) for i in range(0, n, B)], axis=1)


def delta_hrir(**taps):
    """A 14-channel HRIR that is silent except for the named taps, e.g. FL_L=(0, 1.0)."""
    from arctis_sound_manager.win.audio.convolver import HESUVI_14

    h = np.zeros((14, 64))
    for name, (pos, value) in taps.items():
        h[HESUVI_14.index(name), pos] = value
    return h


def test_no_reverb_no_boost_equals_shelf_then_convolution_then_the_limiter_delay():
    h = load_hrir(HRIR_DIR / "gsx.wav")
    vs = VirtualSurround(h, RATE, B, immersion_db=6.0, distance_wet=0.0, boost_db=0.0)
    x = np.random.default_rng(1).standard_normal((8, B * 30)) * 0.05
    got = run(vs, x)

    m = hrir_matrix(h)
    ears = sum(np.stack([fftconvolve(x[i], m[i, o]) for o in range(2)])[:, :x.shape[1]] for i in range(8))
    shelf = biquad.to_sos_row(biquad.design("highshelf", 10.0, 0.7071, 6.0, RATE))[None, :]
    want = sosfilt(shelf, ears, axis=-1)
    d = vs.latency_frames
    assert d == 240
    assert np.max(np.abs(want)) < 0.89                       # the limiter never engages in this test
    assert np.allclose(got[:, d:], want[:, :got.shape[1] - d], atol=1e-8)
    assert np.all(got[:, :d] == 0)


def test_immersion_is_a_broadband_gain():
    h = delta_hrir(FL_L=(0, 1.0), FL_R=(0, 0.5))
    x = np.zeros((8, B * 10)); x[0] = np.random.default_rng(2).standard_normal(B * 10) * 0.02
    base = run(VirtualSurround(h, RATE, B, 0.0, 0.0), x)
    for db in (3.0, 6.0, 12.0):
        got = run(VirtualSurround(h, RATE, B, db, 0.0), x)
        gain = np.sqrt(np.mean(got[:, 2000:] ** 2)) / np.sqrt(np.mean(base[:, 2000:] ** 2))
        assert 20 * np.log10(gain) == pytest.approx(db, abs=0.1), db


def test_wiring_each_speaker_reaches_the_right_ears():
    # FR is the speaker on the right: its near ear is the right one (FR_R), the far one the left (FR_L)
    h = delta_hrir(FR_R=(0, 1.0), FR_L=(5, 0.4))
    vs = VirtualSurround(h, RATE, B, 0.0, 0.0)
    x = np.zeros((8, B * 3)); x[1, 100] = 0.1                 # channel 1 = FR
    y = run(vs, x)
    d = vs.latency_frames
    assert y[1, 100 + d] == pytest.approx(0.1, abs=1e-9)      # right ear, no delay
    assert y[0, 105 + d] == pytest.approx(0.04, abs=1e-9)     # left ear, 5 samples later, 0.4x
    # LFE is rendered through the centre's responses
    h = delta_hrir(FC_L=(0, 1.0), FC_R=(0, 0.25))
    y = run(VirtualSurround(h, RATE, B, 0.0, 0.0), np.where(np.arange(8)[:, None] == 3, 1.0, 0.0) * np.ones((8, B * 4)) * 0.05)
    assert np.mean(np.abs(y[0, 1000:])) == pytest.approx(0.05, rel=0.01) and np.mean(np.abs(y[1, 1000:])) == pytest.approx(0.0125, rel=0.02)


def test_distance_is_the_plate_reverb_on_each_ear():
    h = load_hrir(HRIR_DIR / "gsx.wav")
    wet = 0.4
    vs = VirtualSurround(h, RATE, B, immersion_db=0.0, distance_wet=wet)
    dry = VirtualSurround(h, RATE, B, immersion_db=0.0, distance_wet=0.0)
    x = np.random.default_rng(3).standard_normal((8, B * 20)) * 0.04
    got = run(vs, x)
    # reproduce: ears -> plate_L (left output) / plate_R (right output) -> limiter delay
    ears = np.concatenate([dry._conv.process(x[:, i:i + B]) for i in range(0, B * 20, B)], axis=1)
    pl, pr = PlateReverb(2.5, 0.5, wet), PlateReverb(2.5, 0.5, wet)
    left = np.concatenate([pl.process(ears[0, i:i + B])[0] for i in range(0, B * 20, B)])
    right = np.concatenate([pr.process(ears[1, i:i + B])[1] for i in range(0, B * 20, B)])
    d = vs.latency_frames
    want = np.stack([left, right])
    assert np.max(np.abs(want)) < 0.89
    assert np.allclose(got[:, d:], want[:, :-d], atol=1e-8)
    assert not np.allclose(got, run(dry, x))


def test_zero_distance_skips_the_reverb_and_coming_back_starts_clean():
    h = delta_hrir(FL_L=(0, 1.0), FL_R=(0, 1.0))
    vs = VirtualSurround(h, RATE, B, 0.0, distance_wet=0.5)
    burst = np.zeros((8, B * 4)); burst[0, 10] = 0.3
    run(vs, burst)
    vs.set_params(0.0, 0.0)
    vs.set_params(0.0, 0.5)
    silent = run(vs, np.zeros((8, B * 4)))
    d = vs.latency_frames
    assert np.all(silent[:, d:] == 0.0)                       # the old tail is gone (the first d samples are the limiter's own delay line)
    # control: without leaving and re-entering bypass the tail would still be audible
    keep = VirtualSurround(h, RATE, B, 0.0, distance_wet=0.5)
    run(keep, burst)
    assert np.max(np.abs(run(keep, np.zeros((8, B * 4)))[:, d:])) > 1e-5


def test_boost_cancels_in_the_limiter_and_returns_after_it():
    h = delta_hrir(FL_L=(0, 1.0), FL_R=(0, 1.0))
    tone = np.sin(2 * np.pi * 1000 * np.arange(B * 40) / RATE) * 0.05
    x = np.zeros((8, B * 40)); x[0] = tone
    plain = run(VirtualSurround(h, RATE, B, 0.0, 0.0, boost_db=0.0), x)
    boosted = run(VirtualSurround(h, RATE, B, 0.0, 0.0, boost_db=6.0), x)
    # quiet programme: the limiter trim (-6 dB) and the make-up shelf (+6 dB at 1 kHz) cancel in level
    # (the shelf has a few degrees of phase lead at 1 kHz, so compare levels, not samples)
    rms = lambda a: np.sqrt(np.mean(a[:, 3000:] ** 2))
    assert 20 * np.log10(rms(boosted) / rms(plain)) == pytest.approx(0.0, abs=0.01)
    assert rms(plain) > 0.03                                  # and the signal is actually there

    # loud programme: the ceiling is lifted by the boost, the limiter works at -1 dB on the trimmed signal
    loud = np.zeros((8, B * 40)); loud[0] = np.sin(2 * np.pi * 300 * np.arange(B * 40) / RATE) * 3.0
    out = run(VirtualSurround(h, RATE, B, 0.0, 0.0, boost_db=6.0), loud)
    assert 0.9 * db2lin(-1 + 6) < np.max(np.abs(out[:, 6000:])) < 1.12 * db2lin(-1 + 6)
    ceiling = run(VirtualSurround(h, RATE, B, 0.0, 0.0, boost_db=0.0), loud)
    assert np.max(np.abs(ceiling)) <= db2lin(-1.0) + 1e-9


def test_output_never_exceeds_the_ceiling_without_boost():
    h = load_hrir(HRIR_DIR / "nahimic.wav")
    vs = VirtualSurround(h, RATE, B, immersion_db=12.0, distance_wet=1.0)
    x = np.random.default_rng(5).standard_normal((8, B * 30)) * 0.8
    assert np.max(np.abs(run(vs, x))) <= db2lin(-1.0) + 1e-9


def test_hrir_swap_crossfades_then_matches_a_fresh_instance():
    a, b = delta_hrir(FL_L=(0, 1.0), FL_R=(0, 1.0)), delta_hrir(FL_L=(0, 0.5), FL_R=(0, 0.25))
    x = np.zeros((8, B * 30)); x[0] = np.random.default_rng(6).standard_normal(B * 30) * 0.05
    vs = VirtualSurround(a, RATE, B, 0.0, 0.0)
    out = [vs.process(x[:, i * B:(i + 1) * B]) for i in range(10)]
    vs.set_hrir(b, fade_blocks=4)
    out += [vs.process(x[:, i * B:(i + 1) * B]) for i in range(10, 30)]
    y = np.concatenate(out, axis=1)
    fresh = run(VirtualSurround(b, RATE, B, 0.0, 0.0), x)
    assert np.allclose(y[:, -B * 10:], fresh[:, -B * 10:], atol=1e-9)
    # during the fade the gain moves monotonically between the two (never a jump, never a dip below the new one)
    seg = np.abs(y[0, 10 * B + 240: 14 * B + 240]) / np.maximum(np.abs(x[0, 10 * B: 14 * B]), 1e-6)
    blocks = seg.reshape(4, B).mean(axis=1)
    assert np.all(np.diff(blocks) < 0) and 0.5 <= blocks.min() and blocks.max() <= 1.0


def test_downmix_follows_itu_and_leaves_stereo_alone():
    x = np.zeros((8, 4))
    x[0], x[1] = 1.0, -1.0                                    # a plain stereo programme
    assert np.array_equal(downmix_to_stereo(x), np.array([[1.0] * 4, [-1.0] * 4]))
    y = np.zeros((8, 1)); y[2] = 1.0                          # centre -> both sides at -3 dB
    assert np.allclose(downmix_to_stereo(y)[:, 0], [0.7071, 0.7071])
    z = np.zeros((8, 1)); z[3] = 1.0                          # LFE is left out
    assert np.all(downmix_to_stereo(z) == 0)
    r = np.zeros((8, 1)); r[4], r[5], r[6], r[7] = 1, 2, 3, 4  # RL RR SL SR
    assert np.allclose(downmix_to_stereo(r)[:, 0], [0.7071 * (1 + 3), 0.7071 * (2 + 4)])


def test_downmix_matches_the_linux_gain_table():
    from arctis_sound_manager import sonar_to_pipewire as s2p
    order = ("FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR")
    for side, ear in (("L", 0), ("R", 1)):
        for ch, gain in s2p._DOWNMIX_GAINS[side]:
            e = np.zeros((8, 1)); e[order.index(ch)] = 1.0
            assert downmix_to_stereo(e)[ear, 0] == pytest.approx(gain)


def test_parameters_are_clamped_and_rounded_like_the_conf():
    vs = VirtualSurround(delta_hrir(FL_L=(0, 1.0)), RATE, B)
    vs.set_params(99.0, 7.0, 99.0)
    assert (vs.immersion_db, vs.distance_wet, vs.boost_db) == (12.0, 1.0, 12.0)
    vs.set_params(-5.0, -1.0, -99.0)
    assert (vs.immersion_db, vs.distance_wet, vs.boost_db) == (0.0, 0.0, -12.0)
    vs.set_params(4.96, 0.333, 2.04)
    assert (vs.immersion_db, vs.distance_wet, vs.boost_db) == (5.0, 0.33, 2.0)


def test_the_longest_bundled_hrir_runs_comfortably_in_real_time():
    h = load_hrir(HRIR_DIR / "ds3d+++.wav")
    assert h.shape[1] > 16000
    vs = VirtualSurround(h, RATE, B, 6.0, 0.5, 3.0)
    x = np.random.default_rng(7).standard_normal((8, B)) * 0.1
    for _ in range(5):
        vs.process(x)
    t0 = time.process_time()                                  # CPU time, so other programs on the machine cannot fail it
    for _ in range(100):
        vs.process(x)
    per_block_ms = (time.process_time() - t0) * 10
    assert per_block_ms < 10.0 * 0.5, per_block_ms            # under half of the 10 ms the block lasts


def test_limiter_stage_is_the_conf_limiter_minus_one_db_with_a_tenth_of_a_second_release():
    from arctis_sound_manager.win.audio.dynamics import Limiter

    h = delta_hrir(FL_L=(0, 1.0), FL_R=(0, 1.0))
    n = np.arange(B * 60)
    prog = np.where(n < B * 20, 3.0, 0.05) * np.sin(2 * np.pi * 300 * n / RATE)   # loud, then quiet
    x = np.zeros((8, n.size)); x[0] = prog
    got = run(VirtualSurround(h, RATE, B, 0.0, 0.0), x)
    ref = Limiter(RATE, 2, limit_db=-1.0, release_s=0.1)
    want = np.concatenate([ref.process(np.stack([prog[i:i + B]] * 2)) for i in range(0, n.size, B)], axis=1)
    assert np.allclose(got, want, atol=1e-9)
    recovering = got[0, B * 20 + 300:B * 30]                         # the release is audible in the quiet part
    assert np.max(np.abs(recovering)) < 0.05                     # still attenuated shortly after the burst


def test_centre_and_lfe_are_folded_into_one_convolution_without_changing_the_sound():
    from arctis_sound_manager.win.audio.spatial import _Bank

    h = load_hrir(HRIR_DIR / "gsx.wav")
    bank = _Bank(h, B)
    assert bank.keep == [0, 1, 2, 4, 5, 6, 7] and bank.fold == [(3, 2)]
    assert bank.conv.n_in == 7
    x = np.random.default_rng(9).standard_normal((8, B * 12)) * 0.1
    got = np.concatenate([bank.process(x[:, i:i + B]) for i in range(0, B * 12, B)], axis=1)
    m = hrir_matrix(h)
    want = sum(np.stack([fftconvolve(x[i], m[i, o]) for o in range(2)])[:, :x.shape[1]] for i in range(8))
    assert np.allclose(got, want, atol=1e-9)
    # the LFE alone is heard through the centre's responses
    lfe = np.zeros((8, B * 6)); lfe[3] = np.random.default_rng(10).standard_normal(B * 6) * 0.1
    bank = _Bank(h, B)                                           # fresh state
    only = np.concatenate([bank.process(lfe[:, i:i + B]) for i in range(0, B * 6, B)], axis=1)
    ref = np.stack([fftconvolve(lfe[3], m[3, o]) for o in range(2)])[:, :B * 6]
    assert np.allclose(only, ref, atol=1e-9) and np.max(np.abs(only)) > 1e-3


def test_the_input_block_is_not_modified_by_the_folding():
    from arctis_sound_manager.win.audio.spatial import _Bank

    bank = _Bank(load_hrir(HRIR_DIR / "gsx.wav"), B)
    x = np.random.default_rng(11).standard_normal((8, B))
    before = x.copy()
    bank.process(x)
    assert np.array_equal(x, before)
