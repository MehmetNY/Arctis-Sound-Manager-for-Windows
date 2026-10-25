# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Which sound devices the engine uses, and how snappy it should be.

``win_audio_engine.json`` (next to ASM's other state files) names the devices by a piece of their
name, because Windows device indices change from one boot to the next::

    {
      "latency": "balanced",
      "inputs":  {"game": "CABLE-A Output", "chat": "CABLE-B Output", "media": "CABLE-C Output", "aux": ""},
      "outputs": {"headset": "Arctis Nova Pro"},
      "routing": {"game": "headset", "chat": "headset", "media": "headset", "aux": "headset"},
      "mic":     {"input": "Microphone (Arctis", "output": "CABLE-D Input"},
      "master_limiter": true
    }

An empty name disables that input. Channels the user's programs are sent to (one virtual cable each)
are read from the *Output* side of a cable; the processed mix goes to the real headset; the processed
microphone goes to the *Input* side of one more cable, which other programs then record from.

A file that carries ``"auto": true`` was written by ASM itself the first time it ran (see
:meth:`~arctis_sound_manager.win.audio.host.EngineHost.autoconfigure`); it is redone whenever the devices it names
are missing, until the user changes something by hand (saving a configuration through the GUI drops the flag).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from arctis_sound_manager.win.audio.backend import DeviceInfo, find_device
from arctis_sound_manager.win.audio.graph import CHANNELS
from arctis_sound_manager.win.audio.tap import is_tap_device, tap_device_name

_log = logging.getLogger(__name__)

FILE_NAME = "win_audio_engine.json"
FRAME = 480


@dataclass(frozen=True)
class LatencyProfile:
    """Block sizes (frames at 48 kHz) and how many blocks each buffer holds."""

    block: int            # playback processing block
    mic_block: int
    input_blocks: float   # an input port keeps this many blocks of audio in hand
    output_blocks: float  # ... and so does the output ring in front of the device
    note: str = ""

    def approx_latency_ms(self, device_ms: float = 20.0) -> float:
        """Rough one-way latency: input + output buffers + the two device buffers."""
        return (self.input_blocks + self.output_blocks) * self.block / 48.0 + device_ms


PROFILES: dict[str, LatencyProfile] = {
    "low": LatencyProfile(480, 480, 2.0, 2.0, "lowest delay, highest CPU use"),
    "balanced": LatencyProfile(960, 480, 2.0, 2.0, "good for games and music"),
    "safe": LatencyProfile(1920, 960, 2.0, 2.0, "for slow or busy computers: fewest dropouts"),
}
DEFAULT_PROFILE = "balanced"

BUSES = ("headset", "stream")


@dataclass
class EngineConfig:
    latency: str = DEFAULT_PROFILE
    block: int | None = None                  # override of the profile's playback block
    mic_block: int | None = None
    device_latency: str | float = "low"       # PortAudio's latency hint for the devices themselves
    inputs: dict[str, str] = field(default_factory=lambda: {ch: "" for ch in CHANNELS})
    outputs: dict[str, str] = field(default_factory=lambda: {"headset": ""})
    routing: dict[str, str] = field(default_factory=lambda: {ch: "headset" for ch in CHANNELS})
    mic_input: str = ""
    mic_output: str = ""
    master_limiter: bool = True
    auto: bool = False                        # made by ASM itself (first run): it may be redone when devices appear or go

    # ── derived ─────────────────────────────────────────────────────────────

    @property
    def profile(self) -> LatencyProfile:
        return PROFILES.get(self.latency, PROFILES[DEFAULT_PROFILE])

    @property
    def playback_block(self) -> int:
        return self.block or self.profile.block

    @property
    def microphone_block(self) -> int:
        return self.mic_block or self.profile.mic_block

    @property
    def mic_enabled(self) -> bool:
        return bool(self.mic_input and self.mic_output)

    @property
    def playback_enabled(self) -> bool:
        return bool(self.outputs.get("headset")) and any(self.inputs.get(ch) for ch in CHANNELS)

    # ── (de)serialisation ───────────────────────────────────────────────────

    def to_dict(self) -> dict:
        out = {"latency": self.latency, "device_latency": self.device_latency,
               "inputs": dict(self.inputs), "outputs": dict(self.outputs), "routing": dict(self.routing),
               "mic": {"input": self.mic_input, "output": self.mic_output},
               "master_limiter": self.master_limiter}
        if self.auto:
            out["auto"] = True
        if self.block:
            out["block"] = self.block
        if self.mic_block:
            out["mic_block"] = self.mic_block
        return out

    @classmethod
    def from_dict(cls, raw: object) -> "EngineConfig":
        """Tolerant: anything missing or malformed falls back to the default; nothing raises."""
        cfg = cls()
        if not isinstance(raw, dict):
            return cfg
        latency = raw.get("latency")
        if latency in PROFILES:
            cfg.latency = latency
        for key, attr in (("block", "block"), ("mic_block", "mic_block")):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and FRAME <= value <= 9600 and value % FRAME == 0:
                setattr(cfg, attr, value)
        dl = raw.get("device_latency")
        if dl in ("low", "high") or (isinstance(dl, (int, float)) and not isinstance(dl, bool) and 0.001 <= dl <= 1.0):
            cfg.device_latency = dl
        inputs = raw.get("inputs")
        if isinstance(inputs, dict):
            for ch in CHANNELS:
                if isinstance(inputs.get(ch), str):
                    cfg.inputs[ch] = inputs[ch].strip()
        outputs = raw.get("outputs")
        if isinstance(outputs, dict):
            cfg.outputs = {bus: str(name).strip() for bus, name in outputs.items()
                           if bus in BUSES and isinstance(name, str)} or cfg.outputs
            cfg.outputs.setdefault("headset", "")
        routing = raw.get("routing")
        if isinstance(routing, dict):
            for ch in CHANNELS:
                if routing.get(ch) in BUSES:
                    cfg.routing[ch] = routing[ch]
        mic = raw.get("mic")
        if isinstance(mic, dict):
            cfg.mic_input = str(mic.get("input") or "").strip()
            cfg.mic_output = str(mic.get("output") or "").strip()
        if isinstance(raw.get("master_limiter"), bool):
            cfg.master_limiter = raw["master_limiter"]
        if raw.get("auto") is True:
            cfg.auto = True
        return cfg

    def problems(self) -> list[str]:
        """Things that would stop the engine from doing anything useful (shown to the user)."""
        issues = []
        # Only the microphone wanted (noise cancelling for the voice, the sound of the programs left alone): a
        # microphone and nothing else is a valid set-up, not a half-finished one.
        mic_only = self.mic_enabled and not self.outputs.get("headset") and not any(self.inputs.get(ch) for ch in CHANNELS)
        if not mic_only:
            if not self.outputs.get("headset"):
                issues.append("no headset output device is chosen")
            if not any(self.inputs.get(ch) for ch in CHANNELS):
                issues.append("no virtual cable is assigned to any channel")
        for ch, bus in self.routing.items():
            if bus != "headset" and not self.outputs.get(bus):
                issues.append(f"{ch} is routed to '{bus}' but that output has no device")
        used = [name for name in (*self.inputs.values(),) if name]
        if len(set(used)) != len(used):
            issues.append("the same device is assigned to two channels")
        if self.mic_input and not self.mic_output:
            issues.append("a microphone is chosen but there is no virtual cable to send it to")
        issues.extend(self._loops())
        return issues

    def _loops(self) -> list[str]:
        """A cable's two ends are one wire: playing into the Input of a cable the engine reads from (its Output end)
        would feed the engine its own sound, and two things played into one cable would be mixed together."""
        reads = {}
        for ch in CHANNELS:
            base = _cable_base(self.inputs.get(ch) or "")
            if base:
                reads.setdefault(base, ch)
        found, writes = [], {}
        for label, name in (("the headset", self.outputs.get("headset")), ("the stream mix", self.outputs.get("stream")),
                            ("the processed microphone", self.mic_output)):
            base = _cable_base(name or "")
            if not base:
                continue
            if base in reads:
                found.append(f"{label} plays into the cable that the {reads[base]} channel reads from, so the sound would loop")
            if base in writes:
                found.append(f"{label} and {writes[base]} use the same device")
            writes.setdefault(base, label)
        return found


def config_path(config_dir: Path | None = None) -> Path:
    from arctis_sound_manager.win.audio.params import default_config_dir
    return (config_dir or default_config_dir()) / FILE_NAME


def load_config(config_dir: Path | None = None) -> EngineConfig:
    path = config_path(config_dir)
    try:
        return EngineConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return EngineConfig()
    except (OSError, ValueError) as exc:
        _log.warning("cannot read %s: %s; using defaults", path, exc)
        return EngineConfig()


def save_config(cfg: EngineConfig, config_dir: Path | None = None) -> Path:
    path = config_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)                                     # atomic: a reader never sees half a file
    return path


# ── resolving names to devices ───────────────────────────────────────────────

@dataclass
class ResolvedConfig:
    config: EngineConfig
    inputs: dict[str, DeviceInfo] = field(default_factory=dict)
    outputs: dict[str, DeviceInfo] = field(default_factory=dict)
    mic_input: DeviceInfo | None = None
    mic_output: DeviceInfo | None = None
    missing: list[str] = field(default_factory=list)          # human-readable "not found" lines


def resolve(cfg: EngineConfig, devices: list[DeviceInfo]) -> ResolvedConfig:
    out = ResolvedConfig(cfg)
    for ch in CHANNELS:
        name = cfg.inputs.get(ch, "")
        if not name:
            continue
        found = find_device(devices, name, "input")
        if found is None:
            out.missing.append(f"{ch}: no capture device matching '{name}'")
        else:
            out.inputs[ch] = found
    for bus, name in cfg.outputs.items():
        if not name:
            continue
        found = find_device(devices, name, "output")
        if found is None:
            out.missing.append(f"{bus}: no playback device matching '{name}'")
        else:
            out.outputs[bus] = found
    if cfg.mic_input and cfg.mic_output:
        out.mic_input = find_device(devices, cfg.mic_input, "input")
        out.mic_output = find_device(devices, cfg.mic_output, "output")
        if out.mic_input is None:
            out.missing.append(f"microphone: no capture device matching '{cfg.mic_input}'")
        if out.mic_output is None:
            out.missing.append(f"microphone: no playback device matching '{cfg.mic_output}'")
    return out


# ── a first guess for a machine that has never been configured ───────────────

_CABLE = re.compile(r"cable|vb-audio|voicemeeter|virtual", re.IGNORECASE)
_SERIAL = re.compile(r"cable(?:[ -]?([a-z]))?\b", re.IGNORECASE)
_HEADSET = re.compile(r"arctis|steelseries", re.IGNORECASE)
_NOT_A_HEADSET = re.compile(r"sonar|\bgg\b", re.IGNORECASE)


def _cable_key(name: str) -> tuple:
    """Order cables: plain 'CABLE' first, then 'CABLE-A', 'CABLE-B', ..."""
    m = _SERIAL.search(name)
    letter = (m.group(1) or "").lower() if m else ""
    return (letter != "", letter, name.lower())


def _cable_base(name: str) -> str:
    """'CABLE-A Output (VB-Audio Cable A)' and 'CABLE-A Input (VB-Audio Cable A)' -> 'cable-a'."""
    bare = re.sub(r"\(.*?\)", "", name)
    return re.sub(r"\b(output|input)\b", "", bare, flags=re.IGNORECASE).strip().lower()


def suggest_config(devices: list[DeviceInfo]) -> EngineConfig:
    """Pair up virtual cables with channels, find the headset and its microphone.

    A virtual cable has two ends: programs play into its *Input* (a render device) and the engine
    records from its *Output* (a capture device). Cables, in name order, become game / chat / media /
    aux; with five or more the fifth, with fewer (but at least two) the last one carries the processed
    microphone instead, through its *Input* end. A sixth cable, if there is one, takes Streamer mode's
    mix (what OBS records). A headset is a non-virtual device with 'Arctis' or
    'SteelSeries' in its name, else the system default output. This is a starting point for the setup
    assistant, and what the engine applies by itself on a first run (``"auto": true``).
    """
    cfg = EngineConfig()
    wasapi = [d for d in devices if d.wasapi] or devices
    captures = sorted((d for d in wasapi if d.max_inputs > 0 and _CABLE.search(d.name)), key=lambda d: _cable_key(d.name))
    renders = [d for d in wasapi if d.max_outputs > 0 and _CABLE.search(d.name)]
    cables = []                                               # (capture end, render end) of every complete cable
    for cap in captures:
        end = next((r for r in renders if _cable_base(r.name) == _cable_base(cap.name)), None)
        if end is not None:
            cables.append((cap, end))

    stream_cable = None
    taps = {d.name for d in devices if is_tap_device(d)}
    if all(tap_device_name(ch) in taps for ch in CHANNELS):
        # the per-application tap needs no cable per channel: a cable is only for the processed microphone
        # (and, with a second one, Streamer mode's mix)
        channel_cables = []
        for ch in CHANNELS:
            cfg.inputs[ch] = tap_device_name(ch)
        mic_cable = cables[0] if cables else None
        stream_cable = cables[1] if len(cables) > 1 else None
    elif len(cables) > len(CHANNELS) + 1:
        channel_cables, mic_cable = cables[:len(CHANNELS)], cables[len(CHANNELS)]
        stream_cable = cables[len(CHANNELS) + 1]
    elif len(cables) > len(CHANNELS):
        channel_cables, mic_cable = cables[:len(CHANNELS)], cables[len(CHANNELS)]
    elif len(cables) >= 2:
        channel_cables, mic_cable = cables[:-1], cables[-1]
    else:
        channel_cables, mic_cable = cables, None
    for ch, (cap, _end) in zip(CHANNELS, channel_cables):
        cfg.inputs[ch] = cap.name
    if mic_cable is not None:
        cfg.mic_output = mic_cable[1].name
    if stream_cable is not None:
        cfg.outputs["stream"] = stream_cable[1].name

    outputs = [d for d in wasapi if d.max_outputs > 0 and not _CABLE.search(d.name)]
    headset = next((d for d in outputs if _HEADSET.search(d.name) and not _NOT_A_HEADSET.search(d.name)), None) \
        or next((d for d in outputs if d.default_output), None)
    if headset is not None:
        cfg.outputs["headset"] = headset.name
    mics = [d for d in wasapi if d.max_inputs > 0 and not _CABLE.search(d.name)]
    mic = next((d for d in mics if _HEADSET.search(d.name) and not _NOT_A_HEADSET.search(d.name)), None) \
        or next((d for d in mics if d.default_input), None)
    if mic is not None and cfg.mic_output:
        cfg.mic_input = mic.name
    return cfg
