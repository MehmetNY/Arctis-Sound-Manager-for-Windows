"""The audio graph: channel strips, ChatMix/volume maths, spatial routing, buses and the mic, from state files."""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import numpy as np
import pytest

from arctis_sound_manager.win.audio import biquad
from arctis_sound_manager.win.audio.dynamics import db2lin
from arctis_sound_manager.win.audio.graph import (BLOCK, ChannelStrip, EngineGraph, chatmix_gain, volume_gain)
from arctis_sound_manager.win.audio.params import SpatialSpec, eq_sections
from arctis_sound_manager.win.audio.spatial import VirtualSurround, downmix_to_stereo
from arctis_sound_manager.win.audio.convolver import load_hrir

warnings.filterwarnings("ignore")
RATE = 48000
HRIR = Path(__file__).resolve().parents[1] / "src" / "arctis_sound_manager" / "hrir_assets" / "gsx.wav"


def write(cfg: Path, name: str, data) -> None:
    (cfg / name).write_text(json.dumps(data))


def eq_state(**kw):
    base = {"bands": [], "basses_db": 0.0, "voix_db": 0.0, "aigus_db": 0.0, "boost_db": 0.0, "smart_volume": None}
    base.update(kw)
    return base


def stereo_program(seconds=0.5, freq=1000.0, db=-20.0, width=8):
    t = np.arange(int(RATE * seconds)) / RATE
    x = np.zeros((width, t.size))
    x[0] = x[1] = np.sin(2 * np.pi * freq * t) * db2lin(db)
    return x


def run_strip(strip, x):
    n = x.shape[1] - x.shape[1] % BLOCK
    return np.concatenate([strip.process(x[:, i:i + BLOCK]) for i in range(0, n, BLOCK)], axis=1)


def rms_db(a):
    return 20 * np.log10(np.sqrt(np.mean(a ** 2)) + 1e-30)


def settle(y, skip=0.25):
    return y[:, int(skip * RATE):]


# ── the gain laws ────────────────────────────────────────────────────────────

def test_volume_is_cubic_like_pulseaudio():
    assert volume_gain(100) == 1.0 and volume_gain(0) == 0.0
    assert volume_gain(50) == pytest.approx(0.125)
    assert volume_gain(150) == 1.0 and volume_gain(-5) == 0.0


def test_chatmix_gain_is_sonars_balance_curve():
    from arctis_sound_manager.pactl import mix_to_volume

    for pct in range(0, 101, 5):
        assert chatmix_gain(pct) == pytest.approx(mix_to_volume(pct) ** 3, rel=1e-12)
    assert chatmix_gain(100) == 1.0 and chatmix_gain(0) == 0.0
    assert 20 * np.log10(chatmix_gain(50)) == pytest.approx(-9.54, abs=0.01)


# ── a strip ──────────────────────────────────────────────────────────────────

def test_a_flat_stereo_programme_passes_a_surround_strip_unchanged():
    s = ChannelStrip("game")
    x = stereo_program()
    y = run_strip(s, x)
    assert np.allclose(y, x[:2, :y.shape[1]], atol=1e-12)


def test_chat_is_stereo_and_never_virtualised():
    s = ChannelStrip("chat")
    s.configure_spatial(SpatialSpec(enabled=True), load_hrir(HRIR), "gsx")
    assert not s.spatial_active
    x = stereo_program(width=2)
    assert np.allclose(run_strip(s, x), x[:, :run_strip(s, x).shape[1]], atol=1e-12)


def test_eq_state_is_applied_with_the_playback_headroom():
    s = ChannelStrip("game")
    state = eq_state(voix_db=6.0, boost_db=0.0)
    s.configure_eq({**state, "bands": []})
    want = eq_sections([], 0, 6.0, 0, 0, RATE, headroom=True)
    assert np.array_equal(s._eq.sos, want)
    y = settle(run_strip(s, stereo_program(1.0, 2000.0)))
    assert rms_db(y[0]) - rms_db(stereo_program(1.0, 2000.0)[0, int(.25 * RATE):]) == pytest.approx(0.0, abs=0.1)  # +6 dB boost - 6 dB headroom
    low = settle(run_strip(s, stereo_program(1.0, 200.0)))
    assert rms_db(low[0]) - rms_db(stereo_program(1.0, 200.0)[0, int(.25 * RATE):]) == pytest.approx(-6.0, abs=0.3)


def test_smart_volume_is_the_sc4_compressor_with_the_presets_parameters():
    from arctis_sound_manager.win.audio.dynamics import Compressor
    from arctis_sound_manager.win.audio.params import smart_volume_spec

    sv = {"enabled": True, "loudness": "loud", "level": 100}
    s = ChannelStrip("game")
    s.configure_eq(eq_state(smart_volume=sv))
    spec = smart_volume_spec(sv)
    ref = Compressor(RATE, 8)
    ref.set_params(spec.rms_peak, spec.attack_ms, spec.release_ms, spec.threshold_db, spec.ratio, spec.knee_db, spec.makeup_db)
    ref.reset()                                                     # state follows the parameters
    x = np.concatenate([stereo_program(0.5, 800.0, -40.0), stereo_program(0.5, 800.0, -3.0)], axis=1)
    got = run_strip(s, x)
    want = np.concatenate([ref.process(x[:, i:i + BLOCK]) for i in range(0, x.shape[1] - x.shape[1] % BLOCK, BLOCK)], axis=1)
    assert np.allclose(got, want[:2], atol=1e-12)
    # and it really changes the level, more for the loud half than for the quiet one
    gain = lambda lo, hi: rms_db(got[0, lo:hi]) - rms_db(x[0, lo:hi])
    assert gain(12000, 23000) > gain(36000, 47000) + 1.0 and gain(12000, 23000) > 5.0


def test_volume_mute_and_chatmix_scale_the_channel():
    s = ChannelStrip("game")
    x = stereo_program(1.0)
    s.set_controls(volume_pct=50)
    y = settle(run_strip(s, x))
    assert rms_db(y[0]) - rms_db(x[0, int(.25 * RATE):]) == pytest.approx(-18.06, abs=0.05)
    s.set_controls(volume_pct=100, chatmix_pct=50)
    y = settle(run_strip(s, x), 0.5)
    assert rms_db(y[0]) - rms_db(x[0, int(.5 * RATE):]) == pytest.approx(-9.54, abs=0.05)
    s.set_controls(muted=True)
    assert np.max(np.abs(settle(run_strip(s, x), 0.5))) == 0.0
    s.set_controls(muted=False, chatmix_pct=100)
    assert rms_db(settle(run_strip(s, x), 0.5)[0]) - rms_db(x[0, int(.5 * RATE):]) == pytest.approx(0.0, abs=0.05)


def test_level_changes_ramp_across_the_block_instead_of_stepping():
    s = ChannelStrip("chat")
    x = np.ones((2, BLOCK * 3)) * 0.5
    s.process(x[:, :BLOCK])
    s.set_controls(muted=True)
    y = s.process(x[:, BLOCK:2 * BLOCK])
    assert y[0, 0] > 0.49 and y[0, -1] == 0.0
    assert np.all(np.diff(y[0]) <= 0) and np.max(np.abs(np.diff(y[0]))) < 0.01
    assert np.all(s.process(x[:, 2 * BLOCK:]) == 0.0)


# ── spatial ──────────────────────────────────────────────────────────────────

def test_spatial_strip_equals_the_virtual_surround_it_wraps():
    hrir = load_hrir(HRIR)
    s = ChannelStrip("media")
    spec = SpatialSpec(enabled=True, immersion_pct=50, distance_pct=0)
    s.configure_spatial(spec, hrir, "gsx")
    assert s.spatial_active and s.latency_frames == 240
    ref = VirtualSurround(hrir, RATE, BLOCK, spec.immersion_db, spec.distance_wet, 0.0)
    x = np.random.default_rng(0).standard_normal((8, BLOCK * 20)) * 0.05
    got = run_strip(s, x)
    want = np.concatenate([ref.process(x[:, i:i + BLOCK]) for i in range(0, x.shape[1], BLOCK)], axis=1)
    assert np.allclose(got, want, atol=1e-12)


def test_the_channels_boost_reaches_the_spatial_limiter():
    s = ChannelStrip("game")
    s.configure_eq(eq_state(boost_db=6.0))
    s.configure_spatial(SpatialSpec(enabled=True, distance_pct=0), load_hrir(HRIR), "gsx")
    assert s._surround.boost_db == 6.0
    s.configure_eq(eq_state(boost_db=3.0))
    assert s._surround.boost_db == 3.0


def test_stereo_input_to_a_surround_strip_uses_the_front_pair_only():
    hrir = load_hrir(HRIR)
    s = ChannelStrip("game")
    s.configure_spatial(SpatialSpec(enabled=True, distance_pct=0), hrir, "gsx")
    y = run_strip(s, stereo_program(0.5, 500.0, -20.0, width=8))
    assert rms_db(y[0]) > -40 and rms_db(y[1]) > -40
    s2 = ChannelStrip("game")
    s2.configure_spatial(SpatialSpec(enabled=True, distance_pct=0), hrir, "gsx")
    y2 = run_strip(s2, stereo_program(0.5, 500.0, -20.0, width=8))
    assert np.array_equal(y, y2)


def test_auto_spatial_only_engages_for_multichannel_sources():
    hrir = load_hrir(HRIR)
    spec = SpatialSpec(enabled=True, distance_pct=0, auto=True)
    s = ChannelStrip("game")
    s.configure_spatial(spec, hrir, "gsx")
    rng = np.random.default_rng(5)
    stereo = np.zeros((8, BLOCK * 20))
    stereo[0] = stereo[1] = rng.standard_normal(BLOCK * 20) * 0.05              # not periodic in 240 samples
    y = run_strip(s, stereo)
    d = s.latency_frames
    assert np.allclose(y[:, d:], stereo[:2, :y.shape[1] - d], atol=1e-12)          # plain, but delayed like the chain
    assert not np.allclose(y[:, d:], stereo[:2, d:y.shape[1]], atol=1e-3)           # (the delay is really there)
    surround = stereo.copy()
    surround[2] = rng.standard_normal(BLOCK * 20) * 0.05                            # a centre channel appears
    z = run_strip(s, surround)
    assert not np.allclose(z[:, d:], surround[:2, :z.shape[1] - d], atol=1e-3)     # virtualised


def test_auto_spatial_keeps_virtualising_through_short_gaps_then_lets_go():
    hrir = load_hrir(HRIR)
    s = ChannelStrip("game")
    s.configure_spatial(SpatialSpec(enabled=True, distance_pct=0, auto=True), hrir, "gsx")
    rng = np.random.default_rng(6)
    loud = np.zeros((8, BLOCK))
    loud[:3] = rng.standard_normal((3, BLOCK)) * 0.05
    s.process(loud)
    d = s.latency_frames

    def plain_after(blocks):
        gap = np.zeros((8, BLOCK * blocks))
        gap[:2] = rng.standard_normal((2, BLOCK * blocks)) * 0.05
        y = run_strip(s, gap)
        return np.allclose(y[:, -BLOCK:], gap[:2, -BLOCK - d:-d], atol=1e-12)

    assert not plain_after(40)       # 0.4 s after the surround source stopped: still virtualised (hold is 1 s)
    assert plain_after(100)          # 1.4 s later: stereo is left alone again


def test_non_auto_spatial_virtualises_stereo_too():
    s = ChannelStrip("game")
    s.configure_spatial(SpatialSpec(enabled=True, distance_pct=0, auto=False), load_hrir(HRIR), "gsx")
    x = stereo_program(0.3)
    y = run_strip(s, x)
    assert not np.allclose(y[:, 300:], x[:2, 300 - 240:y.shape[1] - 240], atol=1e-3)


def test_disabling_spatial_returns_to_the_plain_downmix():
    s = ChannelStrip("game")
    s.configure_spatial(SpatialSpec(enabled=True), load_hrir(HRIR), "gsx")
    s.configure_spatial(SpatialSpec(enabled=False), load_hrir(HRIR), "gsx")
    assert not s.spatial_active
    x = np.random.default_rng(1).standard_normal((8, BLOCK * 3)) * 0.1
    assert np.allclose(run_strip(s, x), downmix_to_stereo(x), atol=1e-12)


def test_changing_the_hrir_swaps_it_in_place():
    s = ChannelStrip("game")
    s.configure_spatial(SpatialSpec(enabled=True), load_hrir(HRIR), "gsx")
    first = s._surround
    other = load_hrir(HRIR.with_name("atmos.wav"))
    s.configure_spatial(SpatialSpec(enabled=True), other, "atmos")
    assert s._surround is first and s._hrir_id == "atmos"


# ── idle channels cost nothing ───────────────────────────────────────────────

def test_a_channel_silent_for_seconds_is_skipped_and_wakes_up_cleanly():
    s = ChannelStrip("aux")
    s.configure_spatial(SpatialSpec(enabled=True), load_hrir(HRIR), "gsx")
    calls = []
    original = s._eq.process
    s._eq.process = lambda a: (calls.append(1), original(a))[1]
    zeros = np.zeros((8, BLOCK))
    for _ in range(int(6.2 * RATE / BLOCK) + 5):
        out = s.process(zeros)
    assert np.all(out == 0.0)
    skipped_at = len(calls)
    for _ in range(100):
        s.process(zeros)
    assert len(calls) == skipped_at                                 # nothing ran
    sound = stereo_program(0.2)
    y = run_strip(s, sound)
    assert len(calls) > skipped_at and np.max(np.abs(y)) > 0.01


# ── the graph ────────────────────────────────────────────────────────────────

def run_graph(g, inputs, blocks):
    outs = {}
    for i in range(blocks):
        feed = {k: v[:, i * BLOCK:(i + 1) * BLOCK] for k, v in inputs.items()}
        for bus, y in g.process(feed).items():
            outs.setdefault(bus, []).append(y)
    return {k: np.concatenate(v, axis=1) for k, v in outs.items()}


def test_channels_are_summed_into_the_headset_bus_and_the_limiter_holds_the_ceiling():
    g = EngineGraph(master_limiter=True)
    x = stereo_program(1.0, 440.0, -3.0)
    out = run_graph(g, {"game": x, "media": x, "chat": x[:2]}, 90)["headset"]
    assert np.max(np.abs(out)) <= db2lin(-0.5) + 1e-9
    assert np.max(np.abs(out)) > 0.9 * db2lin(-0.5)                 # three channels at -3 dB want ~ +6.5 dB


def test_without_the_master_limiter_the_sum_is_exact_and_undelayed():
    g = EngineGraph(master_limiter=False)
    x = stereo_program(0.5, 440.0, -20.0)
    out = run_graph(g, {"game": x, "chat": x[:2] * 0.5}, 40)["headset"]
    assert np.allclose(out, x[:2, :out.shape[1]] * 1.5, atol=1e-12)


def test_routing_sends_channels_to_separate_buses():
    g = EngineGraph(master_limiter=False, routing={"chat": "speakers"})
    x = stereo_program(0.2, 440.0, -20.0)
    out = run_graph(g, {"game": x, "chat": x[:2] * 0.5}, 20)
    assert set(out) == {"headset", "speakers"}
    assert np.allclose(out["speakers"], x[:2, :out["speakers"].shape[1]] * 0.5, atol=1e-12)
    assert np.allclose(out["headset"], x[:2, :out["headset"].shape[1]], atol=1e-12)
    g.set_routing({"chat": "headset"})
    assert set(g.buses) == {"headset"}


def test_missing_inputs_are_silence_and_the_bus_limiter_idles_cheaply():
    g = EngineGraph(master_limiter=True)
    out = g.process({})
    assert np.all(out["headset"] == 0.0)
    bus = g.buses["headset"]
    calls = []
    original = bus.limiter.process
    bus.limiter.process = lambda a: (calls.append(1), original(a))[1]
    for _ in range(20):
        g.process({})
    assert len(calls) <= 1                                         # drained once, then skipped


# ── state files ──────────────────────────────────────────────────────────────

def test_load_state_reads_eq_spatial_and_mic_files(tmp_path):
    write(tmp_path, "sonar_eq_state_game.json", eq_state(voix_db=4.0, boost_db=2.0))
    write(tmp_path, "sonar_spatial_audio.json", {"enabled": True, "immersion": 100, "distance": 0, "auto": False})
    write(tmp_path, "sonar_spatial_audio_media.json", {"enabled": False})
    write(tmp_path, "sonar_spatial_audio_aux.json", {"enabled": True, "immersion": 20, "distance": 40})
    write(tmp_path, "sonar_micro_state.json", {
        "bands": [], "basses_db": 0, "voix_db": 0, "aigus_db": 0, "boost_db": 0,
        "noise_reduction": {"noiseGate": {"enabled": True, "value": -35.0}}})
    g = EngineGraph(master_limiter=False)
    g.load_state(tmp_path, hrir_id="gsx")
    game, media, chat = g.strips["game"], g.strips["media"], g.strips["chat"]
    assert len(game._eq.sos) > 0 and game._surround is not None
    assert game._surround.immersion_db == 12.0 and game._surround.distance_wet == 0.0
    assert game._surround.boost_db == 2.0
    assert media._surround is None and chat._surround is None and len(chat._eq.sos) == 0
    aux = g.strips["aux"]
    assert aux._surround is not None and aux._surround.immersion_db == pytest.approx(2.4) and aux._surround.distance_wet == 0.4
    assert g.mic.spec.gate_threshold_db == -35.0 and g.mic.latency_frames == 240


def test_read_state_is_a_pure_snapshot_that_applies_in_two_independent_halves(tmp_path):
    from arctis_sound_manager.win.audio.graph import read_state
    write(tmp_path, "sonar_eq_state_game.json", eq_state(voix_db=4.0))
    write(tmp_path, "sonar_micro_state.json", {
        "bands": [], "basses_db": 0, "voix_db": 0, "aigus_db": 0, "boost_db": 0,
        "noise_reduction": {"noiseGate": {"enabled": True, "value": -35.0}}})
    snap = read_state(tmp_path, "gsx", RATE)
    assert snap.hrir_id == "gsx" and snap.hrir14.shape[0] == 14
    assert snap.eq["game"] is not None and snap.eq["chat"] is None and set(snap.spatial) == {"game", "media", "aux"}
    assert snap.mic.gate_threshold_db == -35.0
    g = EngineGraph(master_limiter=False)
    g.apply_playback(snap)                                       # the audio thread's half ...
    assert len(g.strips["game"]._eq.sos) > 0 and g.mic.spec.gate_threshold_db is None
    g.apply_mic(snap)                                            # ... and the microphone thread's
    assert g.mic.spec.gate_threshold_db == -35.0
    (tmp_path / "sonar_eq_state_game.json").unlink()             # the snapshot no longer depends on the files
    g2 = EngineGraph(master_limiter=False)
    g2.apply_playback(snap)
    assert len(g2.strips["game"]._eq.sos) > 0


def test_applying_the_same_snapshot_twice_keeps_the_running_effects(tmp_path):
    from arctis_sound_manager.win.audio.graph import read_state
    snap = read_state(tmp_path, "gsx", RATE)
    g = EngineGraph(master_limiter=False)
    g.apply_playback(snap)
    surround = g.strips["game"]._surround
    g.apply_playback(snap)
    assert g.strips["game"]._surround is surround                # same object: no rebuild, no glitch


def test_empty_config_dir_means_flat_eq_and_default_spatial(tmp_path):
    g = EngineGraph(master_limiter=False)
    g.load_state(tmp_path, hrir_id="gsx")
    assert all(len(s._eq.sos) == 0 for s in g.strips.values())
    assert g.strips["game"].spatial_active                          # Sonar's default: spatial on, 50/50
    assert g.strips["game"]._surround.immersion_db == 6.0
    assert g.mic.spec.gate_threshold_db is None


def test_an_unknown_hrir_falls_back_to_the_default_profile(tmp_path):
    g = EngineGraph(master_limiter=False)
    g.load_state(tmp_path, hrir_id="../../etc/passwd")
    assert g.strips["game"]._hrir_id == "atmos"


def test_reloading_unchanged_state_does_not_disturb_the_audio(tmp_path):
    write(tmp_path, "sonar_eq_state_game.json", eq_state(voix_db=4.0))
    g = EngineGraph(master_limiter=False)
    g.load_state(tmp_path, hrir_id="gsx")
    x = stereo_program(0.4)
    a = run_graph(g, {"game": x}, 20)["headset"]
    g.load_state(tmp_path, hrir_id="gsx")
    b = run_graph(g, {"game": np.concatenate([x, x], axis=1)}, 20)["headset"]
    assert np.max(np.abs(np.diff(np.concatenate([a, b], axis=1)[0]))) < 0.2    # no glitch at the reload seam


def test_the_mic_runs_through_the_mic_chain(tmp_path):
    write(tmp_path, "sonar_micro_state.json", {
        "bands": [], "basses_db": 0, "voix_db": 6.0, "aigus_db": 0, "boost_db": 0})
    g = EngineGraph()
    g.load_state(tmp_path, hrir_id="gsx")
    t = np.arange(RATE) / RATE
    x = np.sin(2 * np.pi * 2000 * t) * 0.05
    y = np.concatenate([g.process_mic(x[i:i + BLOCK]) for i in range(0, RATE - RATE % BLOCK, BLOCK)])
    assert 20 * np.log10(np.std(y[RATE // 2:]) / np.std(x[RATE // 2:y.size])) == pytest.approx(6.0, abs=0.1)


def test_reverb_tails_are_not_cut_by_the_idle_skip():
    s = ChannelStrip("game")
    s.configure_spatial(SpatialSpec(enabled=True, immersion_pct=0, distance_pct=100), load_hrir(HRIR), "gsx")
    burst = np.zeros((8, BLOCK))
    burst[0, 0] = burst[1, 0] = 0.5
    s.process(burst)
    tails = [s.process(np.zeros((8, BLOCK))) for _ in range(int(3.0 * RATE / BLOCK))]
    late = np.concatenate(tails[int(1.5 * RATE / BLOCK):], axis=1)                  # 1.5 .. 3 s after the click
    assert np.max(np.abs(late)) > 1e-4


# ── devices that do not deliver the channel layout the strip was built for ───

def test_a_stereo_game_cable_runs_the_strip_at_two_channels():
    s = ChannelStrip("game")
    s.configure_eq(eq_state(voix_db=4.0))
    x = stereo_program(0.3, width=2)
    y = run_strip(s, x)
    assert s.width == 2 and y.shape[0] == 2
    wide = ChannelStrip("game")
    wide.configure_eq(eq_state(voix_db=4.0))
    z = run_strip(wide, stereo_program(0.3, width=8))
    assert np.allclose(y, z, atol=1e-12)                                   # the same sound, a quarter of the work


def test_a_mono_source_is_heard_in_both_ears():
    s = ChannelStrip("game")
    mono = np.random.default_rng(1).standard_normal((1, BLOCK)) * 0.1
    y = s.process(mono)
    assert y.shape == (2, BLOCK) and np.array_equal(y[0], y[1]) and np.allclose(y[0], mono[0])


def test_a_5_1_cable_is_padded_to_the_eight_channel_layout():
    s = ChannelStrip("game")
    six = np.zeros((6, BLOCK))
    six[4] = 0.5                                                           # rear left
    y8 = ChannelStrip("game").process(np.vstack([six, np.zeros((2, BLOCK))]))
    y6 = s.process(six)
    assert s.width == 8 and np.array_equal(y6, y8)


def test_more_than_eight_channels_keep_the_first_eight():
    s = ChannelStrip("game")
    x = np.zeros((10, BLOCK))
    x[0] = 0.4
    x[9] = 0.9                                                             # discarded
    y = s.process(x)
    assert s.width == 8 and np.allclose(y[0], 0.4 * downmix_to_stereo(np.vstack([np.ones((1, BLOCK)), np.zeros((7, BLOCK))]))[0])


def test_the_eq_survives_a_change_of_device_width():
    s = ChannelStrip("game")
    s.configure_eq(eq_state(voix_db=6.0))
    s.process(np.zeros((8, BLOCK)))
    sos_before = s._eq.sos.copy()
    s.process(np.zeros((2, BLOCK)))                                        # the device was swapped for a stereo one
    assert s.width == 2 and np.array_equal(s._eq.sos, sos_before) and s._eq.channels == 2


# ── the headset's master level ───────────────────────────────────────────────

def bus_blocks(graph, level, blocks, channel="game"):
    """Feed a constant stereo level into one channel; return the headset bus output block by block."""
    x = np.zeros((8, BLOCK))
    x[0] = x[1] = level
    return [graph.process({channel: x})["headset"] for _ in range(blocks)]


def test_the_master_scales_the_headset_mix_like_a_channel_volume():
    g = EngineGraph(master_limiter=False)
    full = bus_blocks(g, 0.1, 10)[-1]
    g.set_master(volume_pct=50)
    half = bus_blocks(g, 0.1, 10)[-1]
    assert half[0, -1] / full[0, -1] == pytest.approx(volume_gain(50), rel=1e-6) and volume_gain(50) == pytest.approx(0.125)


def test_the_master_mute_silences_the_headset_and_unmute_restores_it():
    g = EngineGraph(master_limiter=False)
    full = bus_blocks(g, 0.1, 10)[-1][0, -1]
    g.set_master(muted=True)
    assert np.all(bus_blocks(g, 0.1, 10)[-1] == 0.0)
    g.set_master(muted=False)
    assert bus_blocks(g, 0.1, 10)[-1][0, -1] == pytest.approx(full)


def test_the_master_ramps_across_the_block_instead_of_stepping():
    g = EngineGraph(master_limiter=False)
    bus_blocks(g, 0.5, 5)
    g.set_master(muted=True)
    y = bus_blocks(g, 0.5, 1)[0][0]
    assert y[0] == pytest.approx(0.5 * (1 - 1 / BLOCK)) and y[-1] == 0.0
    assert np.all(np.diff(y) < 0) and np.max(np.abs(np.diff(y))) < 0.01
    assert np.all(bus_blocks(g, 0.5, 1)[0] == 0.0)


def test_the_master_only_touches_the_headset_bus():
    g = EngineGraph(master_limiter=False, routing={"media": "stream"})
    g.set_master(volume_pct=0)
    x = np.zeros((8, BLOCK))
    x[0] = x[1] = 0.1
    out = g.process({"game": x, "media": x})
    for _ in range(5):
        out = g.process({"game": x, "media": x})
    assert np.all(out["headset"] == 0.0) and np.max(np.abs(out["stream"])) > 0.05


def test_the_master_acts_before_the_limiter():
    """A 1.2 mix is clamped to the limit; turned down to half (0.6) it passes untouched instead of being squashed."""
    clamped = max(np.max(np.abs(b)) for b in bus_blocks(EngineGraph(), 1.2, 30))
    assert clamped == pytest.approx(db2lin(-0.5), abs=0.02)
    g = EngineGraph()
    g.set_master(volume_pct=100 * 0.5 ** (1 / 3))                      # a gain of exactly 0.5
    turned_down = max(np.max(np.abs(b)) for b in bus_blocks(g, 1.2, 30)[10:])
    assert turned_down == pytest.approx(0.6, abs=0.01)


def test_the_master_is_full_by_default():
    g = EngineGraph(master_limiter=False)
    assert g.master.gain == 1.0
    assert bus_blocks(g, 0.1, 3)[-1][0, -1] == pytest.approx(0.1)
