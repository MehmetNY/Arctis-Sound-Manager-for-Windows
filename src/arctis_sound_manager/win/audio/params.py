# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""From the state files the GUI already writes to the parameters the DSP needs.

On Linux ``sonar_to_pipewire`` turns that state into filter-chain conf text. This module turns the
same state into numbers for :mod:`.eq`, :mod:`.dynamics` and :mod:`.spatial`, following the same
rules node for node:

* the macro sliders (Basses / Voix / Aigus) are peaking filters at 80 / 2000 / 9000 Hz;
* the curve is lowered by its largest positive gain (headroom), then the user's Boost is added by a
  broadband shelf — ``boost = Boost - headroom``;
* every band literal is clamped to the domain the GUI itself allows;
* a channel with no bands, no macros and no boost is a plain bypass.

The constants are copies of the ones in ``sonar_to_pipewire`` (kept here so the engine process
imports nothing heavy); ``tests/test_win_audio_params.py`` asserts they agree.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from arctis_sound_manager.eq_types import EqBand
from arctis_sound_manager.win.audio import biquad

_log = logging.getLogger(__name__)

MACRO_PARAMS = {
    "basses": {"freq": 80.0, "q": 0.50},
    "voix": {"freq": 2000.0, "q": 0.60},
    "aigus": {"freq": 9000.0, "q": 0.80},
}

SMART_PRESETS: dict[str, dict] = {
    "quiet": {"threshold": -27.0, "ratio": 6.0, "makeup": 4.0, "attack": 5.0, "release": 200.0, "knee": 8.0},
    "balanced": {"threshold": -18.0, "ratio": 4.0, "makeup": 8.0, "attack": 10.0, "release": 200.0, "knee": 6.0},
    "loud": {"threshold": -9.0, "ratio": 3.0, "makeup": 12.0, "attack": 15.0, "release": 300.0, "knee": 4.0},
}

BAND_FREQ_RANGE = (20.0, 20000.0)
BAND_Q_RANGE = (0.1, 10.0)
BAND_GAIN_RANGE = (-12.0, 12.0)

# Boost shelf: "bq_highshelf Freq = 10 Q = 0.7071" — a broadband gain with its knee below hearing.
BOOST_FREQ_HZ = 10.0
BOOST_Q = 0.7071

# The Sonar-style types the rack lays out first (see sonar_to_pipewire._band_slot_rack); every other
# type follows in curve order. Order does not change the sound (cascaded biquads commute) but it
# keeps the section list recognisable next to the Linux conf.
_RACK_TYPES = ("peakingEQ", "lowShelving", "highShelving")

# A gain below this is a passthrough: the section is dropped instead of run.
_UNITY_EPS_DB = 1e-6


def clamp_finite(value: float, lo: float, hi: float, default: float) -> float:
    """Clamp into ``[lo, hi]``; a non-finite value (inf, nan from a shared preset) becomes *default*."""
    if not math.isfinite(value):
        return default
    return max(lo, min(hi, value))


def headroom_db(bands, macro_values: dict[str, float] | None = None) -> float:
    """How far the curve is lowered so no band raises the level (``eq_headroom_db``)."""
    gains = [float(b.gain) for b in bands if getattr(b, "enabled", True)]
    if macro_values:
        gains += [float(v) for v in macro_values.values()]
    peak = max([0.0] + [g for g in gains if math.isfinite(g)])
    return round(peak, 2)


def _is_active(band: EqBand) -> bool:
    return band.enabled and band.type != "byPass"


@dataclass(frozen=True)
class CompressorSpec:
    """Parameters of the swh SC4 mono compressor, in the plugin's own units."""

    rms_peak: float
    attack_ms: float
    release_ms: float
    threshold_db: float
    ratio: float
    knee_db: float
    makeup_db: float


def smart_volume_spec(smart_volume: dict | None) -> CompressorSpec | None:
    """The channel's Smart Volume compressor, or ``None`` when it is off."""
    if not smart_volume or not smart_volume.get("enabled"):
        return None
    preset = SMART_PRESETS.get(smart_volume.get("loudness", "balanced"), SMART_PRESETS["balanced"])
    t = max(0.0, min(100.0, float(smart_volume.get("level", 50)))) / 100.0
    return CompressorSpec(
        rms_peak=0.0,
        attack_ms=preset["attack"],
        release_ms=preset["release"],
        threshold_db=preset["threshold"],
        ratio=round(1.0 + (preset["ratio"] - 1.0) * t, 1),  # the conf writes "{ratio:.1f}"
        knee_db=preset["knee"],
        makeup_db=round(preset["makeup"] * t, 1),
    )


def eq_sections(
    bands: list[EqBand],
    basses_db: float,
    voix_db: float,
    aigus_db: float,
    boost_db: float,
    rate: float,
    headroom: bool = True,
) -> np.ndarray:
    """Every filter section of one EQ channel, as an ``(n, 6)`` scipy SOS array.

    An empty array means bypass. Sections that would be a unity passthrough (gain 0 dB) are omitted;
    Linux keeps them as fixed slots only so a running graph can be edited without a restart.

    ``headroom`` lowers the curve by its largest boost, as the playback channels do. The microphone
    EQ does not (its conf writes the user's Boost as it is), so the mic chain passes ``False``.
    """
    boost_db = max(-12.0, min(12.0, boost_db))
    active = [b for b in bands if _is_active(b)]
    macro_values = {"basses": basses_db, "voix": voix_db, "aigus": aigus_db}
    flat = (not active and all(abs(v) < 0.01 for v in macro_values.values()) and abs(boost_db) < 0.01)
    if flat:
        return np.zeros((0, 6))

    rows: list[np.ndarray] = []

    def add(kind: str, freq: float, q: float, gain: float) -> None:
        freq = clamp_finite(freq, *BAND_FREQ_RANGE, 1000.0)
        q = clamp_finite(q, *BAND_Q_RANGE, 0.7071)
        gain = clamp_finite(gain, *BAND_GAIN_RANGE, 0.0)
        if kind in ("peaking", "lowshelf", "highshelf") and abs(gain) < _UNITY_EPS_DB:
            return
        rows.append(biquad.to_sos_row(biquad.design(kind, freq, q, gain, rate)))

    ordered = [b for t in _RACK_TYPES for b in active if b.type == t]
    ordered += [b for b in active if b.type not in _RACK_TYPES]
    for band in ordered:
        kind = biquad.KIND_FOR_BAND_TYPE.get(band.type, "peaking")
        add(kind, band.freq, band.q, band.gain)
        if band.type == "bandPassPeakQ":
            # PipeWire's bandpass peaks at 0 dB; Sonar's bandPassPeakQ peaks at Q. Same filter x Q.
            rows.append(biquad.gain_stage(clamp_finite(band.q, *BAND_Q_RANGE, 1.0)))

    for name, db in macro_values.items():
        p = MACRO_PARAMS[name]
        add("peaking", p["freq"], p["q"], db)

    net_boost = boost_db - (headroom_db(active, macro_values) if headroom else 0.0)
    if abs(net_boost) >= _UNITY_EPS_DB:
        # unclamped on purpose: PipeWire's boost node takes the raw value (range -24..+12 here)
        rows.append(biquad.to_sos_row(biquad.design("highshelf", BOOST_FREQ_HZ, BOOST_Q, net_boost, rate)))

    return np.array(rows) if rows else np.zeros((0, 6))


# ── microphone processing parameters ─────────────────────────────────────────

@dataclass(frozen=True)
class MicSpec:
    """Everything the microphone chain needs, already validated."""

    eq_bands: list
    basses_db: float
    voix_db: float
    aigus_db: float
    boost_db: float
    background_highpass_hz: float | None   # bgReduction: cutoff 30..350 Hz
    impact_gain_db: float | None           # impactReduction: high shelf 0..-12 dB at 4 kHz
    gate_threshold_db: float | None        # noiseGate: -60..-10 dB
    noise_canceling: bool
    noise_canceling_engine: str            # "rnnoise" | "deepfilternet"
    noise_canceling_strength: float        # 0..1 (the GUI's slider)
    compressor: CompressorSpec | None


def mic_spec(state: dict | None) -> MicSpec:
    """Build the :class:`MicSpec` from ``sonar_micro_state.json`` (as ``load_micro_state`` returns it)."""
    state = state or {}
    nc = state.get("noise_canceling") or {}
    nr = state.get("noise_reduction") or {}
    bg, impact = nr.get("bgReduction") or {}, nr.get("impactReduction") or {}
    gate, comp = nr.get("noiseGate") or {}, nr.get("compressor") or {}

    bg_hz = None
    if bg.get("enabled"):
        bg_hz = 30.0 + max(0.0, min(1.0, float(bg.get("value", 0.0)))) * 320.0

    impact_db = None
    if impact.get("enabled"):
        g = -max(0.0, min(1.0, float(impact.get("value", 0.0)))) * 12.0
        impact_db = g if abs(g) >= 0.01 else None

    gate_db = None
    if gate.get("enabled"):
        gate_db = max(-60.0, min(-10.0, float(gate.get("value", -40.0))))

    compressor = None
    if comp.get("enabled"):
        v = max(0.0, min(1.0, float(comp.get("value", 0.0))))
        compressor = CompressorSpec(rms_peak=0.5, attack_ms=10.0, release_ms=150.0,
                                    threshold_db=-10.0 - v * 20.0, ratio=round(2.0 + v * 6.0, 1),
                                    knee_db=6.0, makeup_db=round(v * 10.0, 1))

    return MicSpec(
        eq_bands=list(state.get("bands") or []),
        basses_db=float(state.get("basses_db", 0.0)),
        voix_db=float(state.get("voix_db", 0.0)),
        aigus_db=float(state.get("aigus_db", 0.0)),
        boost_db=float(state.get("boost_db", 0.0)),
        background_highpass_hz=bg_hz,
        impact_gain_db=impact_db,
        gate_threshold_db=gate_db,
        noise_canceling=bool(nc.get("enabled", False)),
        noise_canceling_engine=str(nc.get("engine", "rnnoise")),
        noise_canceling_strength=max(0.0, min(1.0, float(nc.get("value", 0.9)))),
        compressor=compressor,
    )


# ── state files ──────────────────────────────────────────────────────────────

def default_config_dir() -> Path:
    return Path.home() / ".config" / "arctis_manager"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _bands_from(raw_bands) -> list[EqBand] | None:
    if not isinstance(raw_bands, list):
        return None
    out = []
    try:
        for b in raw_bands:
            if not isinstance(b, dict):
                return None
            out.append(EqBand(freq=float(b.get("freq", 1000.0)), gain=float(b.get("gain", 0.0)),
                              q=float(b.get("q", 0.7071)), type=str(b.get("type", "peakingEQ")),
                              enabled=bool(b.get("enabled", True))))
    except (TypeError, ValueError):
        return None
    return out


def load_eq_state(channel: str, config_dir: Path | None = None) -> dict | None:
    """``sonar_eq_state_<channel>.json`` -> bands / macros / boost / smart volume, or ``None``.

    Fails closed on anything malformed, like ``sonar_to_pipewire._load_eq_state``.
    """
    raw = _read_json((config_dir or default_config_dir()) / f"sonar_eq_state_{channel}.json")
    if not isinstance(raw, dict):
        return None
    bands = _bands_from(raw.get("bands"))
    if bands is None:
        return None
    try:
        state = {"bands": bands, "basses_db": float(raw.get("basses_db", 0.0)),
                 "voix_db": float(raw.get("voix_db", 0.0)), "aigus_db": float(raw.get("aigus_db", 0.0)),
                 "boost_db": float(raw.get("boost_db", 0.0))}
    except (TypeError, ValueError):
        return None
    smart = raw.get("smart_volume")
    state["smart_volume"] = smart if isinstance(smart, dict) else None
    return state


def load_micro_state(config_dir: Path | None = None) -> dict | None:
    """``sonar_micro_state.json``, validated like ``sonar_to_pipewire._load_micro_state``."""
    raw = _read_json((config_dir or default_config_dir()) / "sonar_micro_state.json")
    if not isinstance(raw, dict):
        return None
    bands = _bands_from(raw.get("bands"))
    if bands is None:
        return None
    try:
        out = {"bands": bands, "basses_db": float(raw.get("basses_db", 0.0)),
               "voix_db": float(raw.get("voix_db", 0.0)), "aigus_db": float(raw.get("aigus_db", 0.0)),
               "boost_db": float(raw.get("boost_db", 0.0))}
        nc = raw.get("noise_canceling")
        if nc is None:
            out["noise_canceling"] = {}
        elif isinstance(nc, dict):
            out["noise_canceling"] = {"enabled": bool(nc.get("enabled", False)),
                                      "value": float(nc.get("value", 0.9)),
                                      "engine": str(nc.get("engine", "rnnoise"))}
        else:
            return None
        nr_raw = raw.get("noise_reduction")
        if nr_raw is None:
            nr_raw = {}
        elif not isinstance(nr_raw, dict):
            return None
        nr = {}
        for key, default in (("bgReduction", 0.0), ("impactReduction", 0.0), ("noiseGate", -40.0), ("compressor", 0.0)):
            sub = nr_raw.get(key)
            if sub is None:
                nr[key] = {"enabled": False, "value": default}
            elif isinstance(sub, dict):
                nr[key] = {"enabled": bool(sub.get("enabled", False)), "value": float(sub.get("value", default))}
            else:
                return None
        out["noise_reduction"] = nr
    except (TypeError, ValueError):
        return None
    return out


@dataclass(frozen=True)
class SpatialSpec:
    enabled: bool = True
    immersion_pct: int = 50
    distance_pct: int = 50
    auto: bool = False

    @property
    def immersion_db(self) -> float:
        return max(0, min(100, self.immersion_pct)) / 100.0 * 12.0

    @property
    def distance_wet(self) -> float:
        return max(0, min(100, self.distance_pct)) / 100.0


def load_spatial(channel: str, config_dir: Path | None = None) -> SpatialSpec:
    """``sonar_spatial_audio[_<channel>].json``; missing/unreadable -> Sonar's defaults (on, 50/50)."""
    suffix = "" if channel == "game" else f"_{channel}"
    data = _read_json((config_dir or default_config_dir()) / f"sonar_spatial_audio{suffix}.json")
    if not isinstance(data, dict):
        return SpatialSpec()
    try:
        return SpatialSpec(enabled=bool(data.get("enabled", True)), immersion_pct=int(data.get("immersion", 50)),
                           distance_pct=int(data.get("distance", 50)), auto=bool(data.get("auto", False)))
    except (TypeError, ValueError):
        return SpatialSpec()


# ── streamer mode ────────────────────────────────────────────────────────────

STREAM_SOURCES = ("game", "chat", "media", "aux", "mic")     # the order of stream_mix.STREAM_CHANNELS


@dataclass(frozen=True)
class StreamSpec:
    """Streamer mode: is it on, and each source's level in the stream mix (percent, muted)."""

    enabled: bool = False
    levels: tuple[tuple[str, float, bool], ...] = tuple((ch, 100.0, False) for ch in STREAM_SOURCES)


def load_stream_spec(config_dir: Path | None = None) -> StreamSpec:
    """``stream_mix.json`` (per-source volume / mute) and ``streamer_mode`` of ``general_settings.yaml``.

    Anything missing or malformed reads as "off, everything at 100 %" (a source with a bad entry keeps its
    default), like ``stream_mix.load_stream_mix``: a torn file must never stop the audio.
    """
    config_dir = config_dir or default_config_dir()
    enabled = False
    try:
        from ruamel.yaml import YAML                             # ASM's own YAML library (settings.py)
        data = YAML(typ="safe").load((config_dir / "general_settings.yaml").read_text(encoding="utf-8"))
        enabled = isinstance(data, dict) and data.get("streamer_mode") is True
    except FileNotFoundError:
        pass
    except Exception:                                            # noqa: BLE001 — bad YAML, unreadable: off
        _log.debug("could not read streamer_mode from general_settings.yaml", exc_info=True)
    raw = _read_json(config_dir / "stream_mix.json")
    raw = raw if isinstance(raw, dict) else {}
    levels = []
    for ch in STREAM_SOURCES:
        entry = raw.get(ch)
        volume, muted = 100.0, False
        if isinstance(entry, dict):
            value = entry.get("volume", 100)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
                volume = float(max(0, min(100, value)))
            if isinstance(entry.get("muted"), bool):
                muted = entry["muted"]
        levels.append((ch, volume, muted))
    return StreamSpec(enabled=enabled, levels=tuple(levels))
