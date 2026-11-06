# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Which sound devices the engine uses, and how snappy it should be.

``win_audio_engine.json`` (next to ASM's other state files) names the devices by a piece of their
name, because Windows device indices change from one boot to the next::

    {
      "schema": 3,
      "latency": "balanced",
      "inputs":  {"game": "ASM Tap game", "chat": "ASM Tap chat", "media": "ASM Tap media", "aux": "ASM Tap aux"},
      "outputs": {"headset": "Arctis Nova Pro"},
      "routing": {"game": "headset", "chat": "headset", "media": "headset", "aux": "headset"},
      "mic":     {"fx": true},
      "master_limiter": true
    }

An empty name disables that input. The channels are the per-application tap's (each program's own sound, taken by
``asm-audiotap.exe``); the processed mix goes to the real headset. The processed microphone needs no device at all:
the microphone effect inside Windows' audio engine hands the headset's own microphone to the engine and takes the
processed one back (:mod:`arctis_sound_manager.win.fx`), so programs keep recording from the one microphone they
already know. No virtual device of any kind is used.

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
    mic_fx: bool = True                       # the processed microphone, through the microphone effect in Windows' audio engine
    master_limiter: bool = True
    schema: int = 3                           # 1: a build before the per-application tap; 2: the tap, a virtual cable for the microphone; 3: the microphone effect
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
        return self.mic_fx

    @property
    def playback_enabled(self) -> bool:
        return bool(self.outputs.get("headset")) and any(self.inputs.get(ch) for ch in CHANNELS)

    # ── (de)serialisation ───────────────────────────────────────────────────

    def to_dict(self) -> dict:
        out = {"schema": self.schema, "latency": self.latency, "device_latency": self.device_latency,
               "inputs": dict(self.inputs), "outputs": dict(self.outputs), "routing": dict(self.routing),
               "mic": {"fx": self.mic_fx},
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
            cfg.schema = 1                                            # junk: not worth keeping, the next auto-setup replaces it
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
        if isinstance(mic, dict) and isinstance(mic.get("fx"), bool):
            cfg.mic_fx = mic["fx"]                                # the "input"/"output" of older builds named devices: gone
        if isinstance(raw.get("master_limiter"), bool):
            cfg.master_limiter = raw["master_limiter"]
        if raw.get("auto") is True:
            cfg.auto = True
        schema = raw.get("schema")
        cfg.schema = schema if isinstance(schema, int) and not isinstance(schema, bool) else 1     # no mark: an older build wrote it
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
                issues.append("no source is assigned to any channel (the audio helper asm-audiotap.exe is missing)")
        for ch, bus in self.routing.items():
            if bus != "headset" and not self.outputs.get(bus):
                issues.append(f"{ch} is routed to '{bus}' but that output has no device")
        used = [name for name in (*self.inputs.values(),) if name]
        if len(set(used)) != len(used):
            issues.append("the same device is assigned to two channels")
        return issues


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
        return EngineConfig(schema=1)


NO_CONFIGURATION = "no configuration yet: waiting for the headset to be found"


def config_problems(config_dir: Path | None = None, cfg: EngineConfig | None = None) -> list[str]:
    """What keeps the engine from starting with the saved configuration. A computer that was never set up has no file
    yet and waits for the headset to be found (the microphone alone is a valid set-up, but only one somebody chose)."""
    problems = (cfg or load_config(config_dir)).problems()
    if not problems and not config_path(config_dir).exists():
        problems = [NO_CONFIGURATION]
    return problems


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
    return out


# ── a first guess for a machine that has never been configured ───────────────

# devices that are not a headset: virtual ones other programs bring (a voice mixer, a virtual cable somebody installed
# for their own use, SteelSeries GG's Sonar). ASM never uses them and never lists them.
_VIRTUAL = re.compile(r"cable|vb-audio|voicemeeter|virtual|sonar", re.IGNORECASE)
_HEADSET = re.compile(r"arctis|steelseries", re.IGNORECASE)
_NOT_A_HEADSET = re.compile(r"sonar|\bgg\b", re.IGNORECASE)


def is_virtual(name: str) -> bool:
    """A device that belongs to somebody else's virtual audio software, not a sound card or a headset."""
    return bool(_VIRTUAL.search(name))


def suggest_config(devices: list[DeviceInfo]) -> EngineConfig:
    """Find the headset and the per-application tap's channels: the first guess the setup assistant shows, and what
    the engine applies by itself on a first run (``"auto": true``).

    The channels are the tap's (one per Game / Chat / Media / Aux, offered by the audio helper); the headset is a
    real device with 'Arctis' or 'SteelSeries' in its name, else the system default output. The microphone has no
    device to choose: it is the headset's own, through the microphone effect.
    """
    cfg = EngineConfig()
    wasapi = [d for d in devices if d.wasapi] or devices
    taps = {d.name for d in devices if is_tap_device(d)}
    if all(tap_device_name(ch) in taps for ch in CHANNELS):
        for ch in CHANNELS:
            cfg.inputs[ch] = tap_device_name(ch)
    outputs = [d for d in wasapi if d.max_outputs > 0 and not is_virtual(d.name) and not is_tap_device(d)]
    headset = next((d for d in outputs if _HEADSET.search(d.name) and not _NOT_A_HEADSET.search(d.name)), None) \
        or next((d for d in outputs if d.default_output), None)
    if headset is not None:
        cfg.outputs["headset"] = headset.name
    return cfg
