"""The Windows engine derives its DSP parameters from the same state the Linux conf generators use.

Rather than trusting a copy of the rules, these tests generate the real Linux filter-chain conf for
the same input, parse every node out of it, and require the Windows engine's filter to have the same
frequency response (and the same compressor numbers).
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from arctis_sound_manager import sonar_to_pipewire as s2p
from arctis_sound_manager.eq_types import EqBand, PW_LABEL
from arctis_sound_manager.win.audio import biquad, params

RATE = 48000.0
PROBE = np.geomspace(20, 20000, 80)

NODE_RE = re.compile(
    r"type = builtin\s+name = (?P<name>\w+)\s+label = (?P<label>bq_\w+)\s+control = \{ "
    r"Freq = (?P<f>[-\d.e+]+)\s+Q = (?P<q>[-\d.e+]+)\s+Gain = (?P<g>[-\d.e+]+) \}")
LINEAR_RE = re.compile(r"label = linear\s+control = \{ Mult = (?P<m>[-\d.e+]+)\s+Add = 0.0 \}")


def conf_response_db(text: str) -> np.ndarray:
    """Total response of every builtin biquad / linear node in a generated conf (per channel)."""
    total = np.zeros(len(PROBE))
    label_to_kind = {"bq_peaking": "peaking", "bq_lowshelf": "lowshelf", "bq_highshelf": "highshelf",
                     "bq_lowpass": "lowpass", "bq_highpass": "highpass", "bq_notch": "notch",
                     "bq_allpass": "allpass", "bq_bandpass": "bandpass"}
    for m in NODE_RE.finditer(text):
        if m["name"].endswith("_R"):
            continue  # 2-channel confs duplicate every node per channel; count one channel
        coeffs = biquad.design(label_to_kind[m["label"]], float(m["f"]), float(m["q"]), float(m["g"]), RATE)
        total += biquad.response_db(biquad.to_sos_row(coeffs)[None, :], PROBE, RATE)
    for m in LINEAR_RE.finditer(text):
        total += 20 * np.log10(float(m["m"]))
    return np.maximum(total, FLOOR_DB)


FLOOR_DB = -100.0  # far below hearing; beyond it float rounding differs between two equal filters


def make_band(rng: random.Random, kinds=("peakingEQ", "lowShelving", "highShelving")) -> EqBand:
    return EqBand(freq=rng.choice([40, 120, 400, 1000, 3000, 8000, 14000]) * rng.uniform(0.9, 1.1),
                  gain=rng.uniform(-9, 9), q=rng.uniform(0.3, 3.0), type=rng.choice(kinds), enabled=True)


@pytest.fixture(autouse=True)
def _no_ladspa(monkeypatch):
    # the generators only emit LADSPA nodes when the plugin file is found; pretend it is
    monkeypatch.setattr(s2p, "_ladspa_plugin_ref", lambda name, resolved=None: name.removesuffix(".so"))
    monkeypatch.setattr(s2p, "_deepfilter_plugin_ref", lambda: "deep_filter")


def test_constants_match_the_linux_module():
    assert params.MACRO_PARAMS == s2p._MACRO_PARAMS
    assert params.SMART_PRESETS == s2p._SMART_PRESETS
    assert params.BAND_FREQ_RANGE == s2p._BAND_FREQ_RANGE
    assert params.BAND_Q_RANGE == s2p._BAND_Q_RANGE
    assert params.BAND_GAIN_RANGE == s2p._BAND_GAIN_RANGE
    assert params._RACK_TYPES == s2p._RACK_TYPES


def test_headroom_matches_linux():
    rng = random.Random(7)
    for _ in range(200):
        bands = [make_band(rng) for _ in range(rng.randint(0, 12))]
        for b in bands:
            b.enabled = rng.random() > 0.2
        macros = {k: rng.uniform(-12, 12) for k in ("basses", "voix", "aigus")} if rng.random() > 0.3 else None
        assert params.headroom_db(bands, macros) == s2p.eq_headroom_db(bands, macros)
    assert params.headroom_db([EqBand(gain=float("inf"))]) == s2p.eq_headroom_db([EqBand(gain=float("inf"))])


@pytest.mark.parametrize("channel", ["game", "chat", "media", "output"])
def test_eq_response_equals_the_generated_conf(channel):
    rng = random.Random(channel)
    for trial in range(25):
        bands = [make_band(rng) for _ in range(rng.randint(0, 14))]
        macros = [rng.choice([0.0, rng.uniform(-12, 12)]) for _ in range(3)]
        boost = rng.choice([0.0, rng.uniform(-12, 12)])
        with patch.object(s2p, "_resolve_external_output", return_value=("sink", 8, "FL FR FC LFE RL RR SL SR")), \
                patch.object(s2p, "_device_attached", return_value=True), \
                patch.object(s2p, "_get_physical_out_chat", return_value="alsa_output.arctis-chat"):
            text = s2p.generate_sonar_eq_conf(channel, bands, *macros, output_path=Path("/dev/null"), boost_db=boost)
        sos = params.eq_sections(bands, *macros, boost, RATE)
        mine = np.maximum(biquad.response_db(sos, PROBE, RATE), FLOOR_DB)
        assert np.allclose(mine, conf_response_db(text), atol=1e-3), (channel, trial)


def test_every_filter_type_matches_the_conf():
    rng = random.Random(11)
    types = list(PW_LABEL)
    for trial in range(40):
        bands = [make_band(rng, types) for _ in range(rng.randint(1, 6))]
        with patch.object(s2p, "_device_attached", return_value=True):
            text = s2p.generate_sonar_eq_conf("game", bands, 0, 0, 0, output_path=Path("/dev/null"))
        sos = params.eq_sections(bands, 0, 0, 0, 0, RATE)
        mine = np.maximum(biquad.response_db(sos, PROBE, RATE), FLOOR_DB)
        assert np.allclose(mine, conf_response_db(text), atol=1e-3), (trial, [b.type for b in bands])


def test_flat_channel_is_a_bypass_like_linux():
    assert params.eq_sections([], 0, 0, 0, 0, RATE).shape == (0, 6)
    disabled = [EqBand(gain=6.0, enabled=False), EqBand(gain=6.0, type="byPass")]
    assert params.eq_sections(disabled, 0, 0, 0, 0, RATE).shape == (0, 6)
    # boost alone is not flat
    assert params.eq_sections([], 0, 0, 0, 3.0, RATE).shape[0] == 1


def test_a_boosting_curve_never_raises_the_level():
    bands = [EqBand(freq=1000, gain=8.0, q=1.0, type="peakingEQ")]
    sos = params.eq_sections(bands, 4.0, 0, 0, 0, RATE)
    peak = biquad.response_db(sos, PROBE, RATE).max()
    assert peak <= 0.1, peak  # headroom lowers the curve by its largest boost (8 dB)


def test_non_finite_and_out_of_range_bands_are_clamped_not_propagated():
    bands = [EqBand(freq=float("nan"), gain=float("inf"), q=float("-inf"), type="peakingEQ"),
             EqBand(freq=1e9, gain=99.0, q=1e9, type="peakingEQ")]
    sos = params.eq_sections(bands, 0, 0, 0, 0, RATE)
    assert np.all(np.isfinite(sos))
    assert np.all(np.isfinite(biquad.response_db(sos, PROBE, RATE)))


@pytest.mark.parametrize("loudness", ["quiet", "balanced", "loud", "nonsense"])
@pytest.mark.parametrize("level", [0, 1, 37, 50, 100, 250])
def test_smart_volume_matches_the_conf(loudness, level):
    sv = {"enabled": True, "loudness": loudness, "level": level}
    with patch.object(s2p, "_device_attached", return_value=True):
        text = s2p.generate_sonar_eq_conf("game", [EqBand(freq=1000, gain=1.0)], 0, 0, 0,
                                          output_path=Path("/dev/null"), smart_volume=sv)
    values = {k: float(v) for k, v in re.findall(r'"([^"]+)" = (-?[\d.]+)', text.split("name = compressor")[1])}
    spec = params.smart_volume_spec(sv)
    assert values["RMS/peak"] == spec.rms_peak
    assert values["Attack time (ms)"] == spec.attack_ms
    assert values["Release time (ms)"] == spec.release_ms
    assert values["Threshold level (dB)"] == spec.threshold_db
    assert values["Ratio (1:n)"] == spec.ratio
    assert values["Knee radius (dB)"] == spec.knee_db
    assert values["Makeup gain (dB)"] == spec.makeup_db


def test_smart_volume_off():
    assert params.smart_volume_spec(None) is None
    assert params.smart_volume_spec({"enabled": False, "level": 80}) is None


def _micro_conf(state):
    with patch.object(s2p, "_device_attached", return_value=True), \
            patch.object(s2p, "_get_physical_in", return_value="alsa_input.arctis"):
        return s2p.generate_sonar_micro_conf(state["bands"], state["basses_db"], state["voix_db"], state["aigus_db"],
                                             output_path=Path("/dev/null"), boost_db=state["boost_db"],
                                             noise_canceling=state["noise_canceling"], noise_reduction=state["noise_reduction"])


def micro_state(**over):
    state = {"bands": [EqBand(freq=250, gain=-3.0, q=1.0), EqBand(freq=6000, gain=2.0, q=0.9, type="highShelving")],
             "basses_db": 2.0, "voix_db": 0.0, "aigus_db": -1.5, "boost_db": 3.0,
             "noise_canceling": {"enabled": True, "value": 0.6, "engine": "rnnoise"},
             "noise_reduction": {"bgReduction": {"enabled": True, "value": 0.4},
                                 "impactReduction": {"enabled": True, "value": 0.5},
                                 "noiseGate": {"enabled": True, "value": -35.0},
                                 "compressor": {"enabled": True, "value": 0.7}}}
    state.update(over)
    return state


def test_mic_spec_matches_the_micro_conf():
    state = micro_state()
    text = _micro_conf(state)
    spec = params.mic_spec(state)

    bg = float(re.search(r"name = nr_bg\s+label = bq_highpass\s+control = \{ Freq = ([\d.]+)", text)[1])
    assert spec.background_highpass_hz == pytest.approx(bg, abs=0.05)
    imp = float(re.search(r"name = nr_impact\s+label = bq_highshelf\s+control = \{ Freq = 4000.0\s+Q = 0.7071\s+Gain = (-?[\d.]+)", text)[1])
    assert spec.impact_gain_db == pytest.approx(imp, abs=0.05)
    gate = float(re.search(r'name = ngate.*?"Threshold \(dB\)" = (-?[\d.]+)', text, re.S)[1])
    assert spec.gate_threshold_db == pytest.approx(gate, abs=0.05)
    nc = float(re.search(r'"VAD Threshold \(%\)" = ([\d.]+)', text)[1])
    assert spec.noise_canceling and spec.noise_canceling_strength * 100 == pytest.approx(nc, abs=0.05)
    comp = text.split("name = comp ")[1]
    vals = {k: float(v) for k, v in re.findall(r'"([^"]+)" = (-?[\d.]+)', comp)}
    c = spec.compressor
    assert (vals["RMS/peak"], vals["Attack time (ms)"], vals["Release time (ms)"]) == (c.rms_peak, c.attack_ms, c.release_ms)
    assert vals["Threshold level (dB)"] == pytest.approx(c.threshold_db, abs=0.05)
    assert vals["Ratio (1:n)"] == c.ratio and vals["Knee radius (dB)"] == c.knee_db
    assert vals["Makeup gain (dB)"] == c.makeup_db

    # the EQ part of the chain (bands + macros + boost) has the same response as the conf's
    pre = [m for m in text.split("name = nr_bg")[0:1]][0]
    # (the mic EQ writes Boost as it is: no headroom, unlike the playback channels)
    sos = params.eq_sections(spec.eq_bands, spec.basses_db, spec.voix_db, spec.aigus_db, spec.boost_db, RATE,
                             headroom=False)
    assert np.allclose(np.maximum(biquad.response_db(sos, PROBE, RATE), FLOOR_DB), conf_response_db(pre), atol=1e-3)


def test_mic_spec_with_everything_off():
    off = micro_state(noise_canceling={"enabled": False, "value": 0.9, "engine": "rnnoise"},
                      noise_reduction={k: {"enabled": False, "value": 0.0}
                                       for k in ("bgReduction", "impactReduction", "noiseGate", "compressor")})
    spec = params.mic_spec(off)
    assert (spec.background_highpass_hz, spec.impact_gain_db, spec.gate_threshold_db, spec.compressor) == (None,) * 4
    assert not spec.noise_canceling
    empty = params.mic_spec(None)
    assert empty.eq_bands == [] and not empty.noise_canceling and empty.compressor is None


# ── state files agree with the Linux loaders ─────────────────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = tmp_path / ".config" / "arctis_manager"
    cfg.mkdir(parents=True)
    return cfg


def test_eq_state_loader_agrees_with_linux(home):
    bands = [EqBand(freq=500, gain=2.5, q=1.2), EqBand(freq=9000, gain=-4, q=0.7, type="highShelving", enabled=False)]
    s2p._save_eq_state("game", bands, 1.0, -2.0, 0.5, 4.0, {"enabled": True, "loudness": "loud", "level": 40})
    mine, theirs = params.load_eq_state("game", home), s2p._load_eq_state("game")
    assert mine == theirs and mine is not None
    assert params.load_eq_state("chat", home) is None and s2p._load_eq_state("chat") is None


@pytest.mark.parametrize("content", ["not json", "[]", '{"bands": "x"}', '{"bands": [1]}',
                                     '{"bands": [], "boost_db": "loud"}'])
def test_eq_state_loader_fails_closed_like_linux(home, content):
    (home / "sonar_eq_state_game.json").write_text(content)
    assert params.load_eq_state("game", home) == s2p._load_eq_state("game") is None


def test_micro_state_loader_agrees_with_linux(home):
    st = micro_state()
    s2p._save_micro_state(st["bands"], st["basses_db"], st["voix_db"], st["aigus_db"], st["boost_db"],
                          st["noise_canceling"], st["noise_reduction"])
    assert params.load_micro_state(home) == s2p._load_micro_state()
    (home / "sonar_micro_state.json").write_text('{"bands": [], "noise_reduction": {"noiseGate": 5}}')
    assert params.load_micro_state(home) is None and s2p._load_micro_state() is None


def test_spatial_loader_agrees_with_linux(home):
    assert params.load_spatial("game", home) == params.SpatialSpec()
    assert (params.load_spatial("game", home).immersion_pct, params.load_spatial("game", home).distance_pct) == s2p._load_spatial_pct("game")
    (home / "sonar_spatial_audio_media.json").write_text(json.dumps({"enabled": False, "immersion": 80, "distance": 10, "auto": True}))
    sp = params.load_spatial("media", home)
    assert (sp.enabled, sp.immersion_pct, sp.distance_pct, sp.auto) == (False, 80, 10, True)
    assert (sp.immersion_pct, sp.distance_pct) == s2p._load_spatial_pct("media")
    assert sp.immersion_db == pytest.approx(9.6) and sp.distance_wet == pytest.approx(0.1)
    (home / "sonar_spatial_audio.json").write_text("garbage")
    assert params.load_spatial("game", home) == params.SpatialSpec()
