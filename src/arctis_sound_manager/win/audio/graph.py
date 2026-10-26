# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Windows audio graph: the same signal flow ASM builds out of PipeWire filter-chains on Linux.

    per channel   input (8ch / 2ch)
                    -> EQ rack (bands, Basses / Voix / Aigus, Boost)
                    -> Smart Volume (SC4 compressor)
                    -> volume x ChatMix x mute
                    -> HeSuVi virtual surround  (or the ITU stereo downmix)  -> stereo
    mix           sum of the channels sent to each output
                    -> master limiter  -> output device
    microphone    mic in -> MicChain -> mic out (a virtual cable other programs record from)

Everything here is plain numpy on ``(channels, frames)`` float64 blocks, with no I/O: the runtime
feeds it blocks and sends the result to the sound devices, and the tests feed it synthetic ones.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from arctis_sound_manager.win.audio import biquad
from arctis_sound_manager.win.audio.convolver import load_hrir
from arctis_sound_manager.win.audio.dynamics import Compressor, Limiter
from arctis_sound_manager.win.audio.eq import BiquadCascade
from arctis_sound_manager.win.audio.mic import MicChain
from arctis_sound_manager.win.audio.params import (MicSpec, SpatialSpec, StreamSpec, default_config_dir, eq_sections,
                                                   load_eq_state, load_micro_state, load_spatial, load_stream_spec,
                                                   mic_spec, smart_volume_spec)
from arctis_sound_manager.win.audio.spatial import VirtualSurround, downmix_to_stereo

_log = logging.getLogger(__name__)

RATE = 48000
BLOCK = 480
CHANNELS = ("game", "chat", "media", "aux")
MASTER = "master"                                    # the headset's overall level (the Home page's Master card)
MASTER_BUS = "headset"                               # ...applies to the mix sent to the headset only
STREAM_BUS = "stream"                                # Streamer mode's independent mix (the OBS cable)
CONTROLS = CHANNELS + (MASTER,)                      # everything volume / mute commands may address
CHANNEL_WIDTH = {"game": 8, "chat": 2, "media": 8, "aux": 8}
SPATIAL_CHANNELS = ("game", "media", "aux")          # chat is stereo and never virtualised
DEFAULT_HRIR = "atmos"

SILENCE = 1e-9                                       # a cable with nothing playing delivers exact zeros
IDLE_SECONDS = 6.0                                   # silent this long -> the effect tails have died away
MULTICHANNEL_FLOOR = 10 ** (-80 / 20)                # "auto" spatial: energy in FC..SR above -80 dBFS
MULTICHANNEL_HOLD_SECONDS = 1.0


def volume_gain(pct: float) -> float:
    """A channel's volume as ASM sets it on Linux: PulseAudio volumes are cubic."""
    return (max(0.0, min(100.0, float(pct))) / 100.0) ** 3


def chatmix_gain(pct: float) -> float:
    """Sonar's ChatMix balance as a linear gain (pactl.mix_to_volume, before the cube root)."""
    x = max(0.0, min(100.0, float(pct))) / 100.0
    return x / (2.0 - x)


@dataclass
class Controls:
    """The live knobs of one channel (volume / mute / ChatMix), changed while audio runs."""

    volume_pct: float = 100.0
    muted: bool = False
    chatmix_pct: float = 100.0

    @property
    def gain(self) -> float:
        return 0.0 if self.muted else volume_gain(self.volume_pct) * chatmix_gain(self.chatmix_pct)


class ChannelStrip:
    """EQ -> Smart Volume -> level -> spatial/downmix for one playback channel."""

    def __init__(self, name: str, rate: int = RATE, block: int = BLOCK):
        self.name = name
        self.width = CHANNEL_WIDTH[name]
        self.rate = rate
        self.block = block
        self.controls = Controls()
        self._eq = BiquadCascade(self.width)
        self._compressor = Compressor(rate, self.width)
        self._compressor_on = False
        self._boost_db = 0.0
        self._spatial_spec = SpatialSpec(enabled=False)
        self._hrir: np.ndarray | None = None
        self._hrir_id: str | None = None
        self._surround: VirtualSurround | None = None
        self._gain = 0.0 if self.controls.muted else self.controls.gain
        self._gain_started = False
        self._silent_blocks = 0
        self._multichannel_left = 0
        self._bypass_delay: np.ndarray | None = None
        self._was_spatial = False
        self._eq_state: dict | None = None

    # ── configuration ───────────────────────────────────────────────────────

    def configure_eq(self, state: dict | None) -> None:
        self._eq_state = state
        if state is None:
            self._eq.set_sos(np.zeros((0, 6)))
            self._compressor_on = False
            self._boost_db = 0.0
            return
        sos = eq_sections(state["bands"], state["basses_db"], state["voix_db"], state["aigus_db"],
                          state["boost_db"], self.rate, headroom=True)
        self._eq.set_sos(sos)
        self._boost_db = float(state["boost_db"])
        sv = smart_volume_spec(state.get("smart_volume"))
        if sv is None:
            self._compressor_on = False
        else:
            self._compressor.set_params(sv.rms_peak, sv.attack_ms, sv.release_ms, sv.threshold_db,
                                        sv.ratio, sv.knee_db, sv.makeup_db)
            if not self._compressor_on:
                self._compressor.reset()
            self._compressor_on = True
        if self._surround is not None:
            self._surround.set_params(self._spatial_spec.immersion_db, self._spatial_spec.distance_wet, self._boost_db)

    def configure_spatial(self, spec: SpatialSpec, hrir14: np.ndarray | None = None, hrir_id: str | None = None) -> None:
        if self.name not in SPATIAL_CHANNELS:
            return
        self._spatial_spec = spec
        want = spec.enabled and hrir14 is not None
        if not want:
            self._surround = None
            self._hrir, self._hrir_id = None, None
            return
        if self._surround is None:
            self._surround = VirtualSurround(hrir14, self.rate, self.block, spec.immersion_db,
                                             spec.distance_wet, self._boost_db)
            self._hrir, self._hrir_id = hrir14, hrir_id
            self._bypass_delay = np.zeros((2, self._surround.latency_frames))
        else:
            if hrir_id != self._hrir_id:
                self._surround.set_hrir(hrir14)
                self._hrir, self._hrir_id = hrir14, hrir_id
            self._surround.set_params(spec.immersion_db, spec.distance_wet, self._boost_db)

    def set_controls(self, volume_pct: float | None = None, muted: bool | None = None,
                     chatmix_pct: float | None = None) -> None:
        if volume_pct is not None:
            self.controls.volume_pct = volume_pct
        if muted is not None:
            self.controls.muted = muted
        if chatmix_pct is not None:
            self.controls.chatmix_pct = chatmix_pct

    # ── processing ──────────────────────────────────────────────────────────

    @property
    def spatial_active(self) -> bool:
        return self._surround is not None

    @property
    def latency_frames(self) -> int:
        return self._surround.latency_frames if self._surround is not None else 0

    def _is_multichannel(self, x: np.ndarray) -> bool:
        if x.shape[0] <= 2:
            return False
        if np.max(np.abs(x[2:])) > MULTICHANNEL_FLOOR:
            self._multichannel_left = int(MULTICHANNEL_HOLD_SECONDS * self.rate / self.block)
            return True
        if self._multichannel_left > 0:
            self._multichannel_left -= 1
            return True
        return False

    def _fit(self, x: np.ndarray) -> np.ndarray:
        """Make the block fit what a device really delivers: mono -> both ears, 3..7 channels (a 5.1 cable)
        -> padded to the 8-channel layout (FL FR FC LFE RL RR SL SR), more than 8 -> the first 8."""
        c = x.shape[0]
        if c == 1:
            x, c = np.vstack([x, x]), 2
        elif 2 < c < 8:
            x, c = np.vstack([x, np.zeros((8 - c, x.shape[1]))]), 8
        elif c > 8:
            x, c = x[:8], 8
        if c != self.width:                                  # first block of a narrower (or wider) device
            self.width = c
            self._eq = BiquadCascade(c)
            self._compressor = Compressor(self.rate, c)
            self._compressor_on = False
            self.configure_eq(self._eq_state)
        return x

    @property
    def idle(self) -> bool:
        """Silent for so long that the effect tails are gone: blocks of this strip cost next to nothing."""
        return self._silent_blocks > IDLE_SECONDS * self.rate / self.block

    def process(self, x: np.ndarray) -> np.ndarray:
        """``(channels, block)`` -> ``(2, block)``."""
        x = self._fit(x)
        n = x.shape[1]
        if not np.any(np.abs(x) > SILENCE):
            self._silent_blocks += 1
            if self._silent_blocks > IDLE_SECONDS * self.rate / self.block:
                self._gain = self._target_gain()
                return np.zeros((2, n))
        else:
            self._silent_blocks = 0

        y = self._eq.process(x)
        if self._compressor_on:
            y = self._compressor.process(y)

        target = self._target_gain()
        ramp = np.linspace(self._gain, target, n + 1)[1:] if target != self._gain else None
        y = y * ramp if ramp is not None else (y * target if target != 1.0 else y)
        self._gain = target

        use_spatial = self._surround is not None
        if use_spatial and self._spatial_spec.auto and not self._is_multichannel(x):
            use_spatial = False
        if use_spatial:
            padded = y if y.shape[0] == 8 else np.vstack([y, np.zeros((8 - y.shape[0], n))])
            out = self._surround.process(padded)
            self._was_spatial = True
            return out
        stereo = downmix_to_stereo(y) if y.shape[0] == 8 else y
        if self._surround is not None:                       # "auto" bypass: keep the same latency as the chain
            joined = np.concatenate([self._bypass_delay, stereo], axis=1)
            stereo, self._bypass_delay = joined[:, :n], joined[:, n:]
        return stereo

    def _target_gain(self) -> float:
        return self.controls.gain


def to_stereo(x: np.ndarray) -> np.ndarray:
    """Whatever a device delivers -> ``(2, n)``: mono to both sides, 3..8 channels folded with the ITU matrix
    (a 5.1 cable is padded to the 8-channel layout first), more than 8 -> the first 8. LFE is left out."""
    c, n = x.shape
    if c == 1:
        return np.vstack([x, x])
    if c == 2:
        return x
    if c < 8:
        x = np.vstack([x, np.zeros((8 - c, n))])
    elif c > 8:
        x = x[:8]
    return downmix_to_stereo(x)


class StreamMix:
    """Streamer mode's mix: Game + Chat + Media + Aux + the processed Mic, each with its own level and mute.

    Like Sonar's Stream mix (and ASM's ``stream_mix.py`` on Linux) it is independent of the personal mix: it
    takes each channel's audio *as the programs play it* - before the channel's EQ, spatial audio, volume,
    ChatMix and the headset's master level - and the microphone *after* its chain. Levels are cubic, like
    every other slider, and change with a ramp so a drag never clicks.
    """

    def __init__(self) -> None:
        self.spec = StreamSpec()
        self._gain: dict[str, float] = {ch: 1.0 for ch, _, _ in self.spec.levels}

    @property
    def enabled(self) -> bool:
        return self.spec.enabled

    def configure(self, spec: StreamSpec) -> None:
        self.spec = spec

    def mix(self, sources: dict[str, np.ndarray | None], n: int) -> np.ndarray:
        """``{source: (2, n) or None}`` -> the stereo stream mix ``(2, n)``."""
        out = np.zeros((2, n))
        for ch, volume, muted in self.spec.levels:
            target = 0.0 if muted else volume_gain(volume)
            start = self._gain.get(ch, 1.0)
            self._gain[ch] = target
            x = sources.get(ch)
            if x is None or (target == 0.0 and start == 0.0):
                continue
            if target == start:
                out += x if target == 1.0 else x * target
            else:
                out += x * np.linspace(start, target, n + 1)[1:]                      # no click when the knob moves
        return out


@dataclass
class OutputBus:
    """A destination and the channels summed into it."""

    name: str
    channels: tuple[str, ...]
    limiter: Limiter | None = None
    quiet_blocks: int = 0          # consecutive silent mixes (the limiter's 5 ms memory is gone after one)


class EngineGraph:
    """All strips, the mixing buses and the microphone chain."""

    def __init__(self, rate: int = RATE, block: int = BLOCK, master_limiter: bool = True,
                 routing: dict[str, str] | None = None, native=None):
        self.rate = rate
        self.block = block
        self.strips = {name: ChannelStrip(name, rate, block) for name in CHANNELS}
        self.mic = MicChain(rate, native=native)
        self.master_limiter = master_limiter
        self.routing = {name: "headset" for name in CHANNELS}
        self.buses: dict[str, OutputBus] = {}
        self.set_routing(routing or {})
        self.status: dict[str, object] = {}
        self.master = Controls()
        self._master_gain = self.master.gain
        self.stream = StreamMix()
        self._stream_limiter = Limiter(rate, 2, limit_db=-0.5, release_s=0.05) if master_limiter else None
        self._stream_quiet = 0
        self._stream_hot = False           # the stream limiter holds audio from the last mix it processed

    def set_routing(self, routing: dict[str, str]) -> None:
        self.routing.update({k: v for k, v in routing.items() if k in CHANNELS})
        old = self.buses
        self.buses = {}
        for bus in sorted(set(self.routing.values())):
            members = tuple(ch for ch in CHANNELS if self.routing[ch] == bus)
            limiter = old[bus].limiter if bus in old else None
            if self.master_limiter and limiter is None:
                limiter = Limiter(self.rate, 2, limit_db=-0.5, release_s=0.05)
            self.buses[bus] = OutputBus(bus, members, limiter if self.master_limiter else None)

    def set_master(self, volume_pct: float | None = None, muted: bool | None = None) -> None:
        """The headset's overall level, applied to its mix just before the limiter."""
        if volume_pct is not None:
            self.master.volume_pct = volume_pct
        if muted is not None:
            self.master.muted = muted

    def _apply_master(self, mix: np.ndarray) -> np.ndarray:
        target = self.master.gain
        if target == self._master_gain:
            return mix if target == 1.0 else mix * target
        ramp = np.linspace(self._master_gain, target, mix.shape[1] + 1)[1:]          # no click when the knob moves
        self._master_gain = target
        return mix * ramp

    @property
    def latency_frames(self) -> dict[str, int]:
        bus = 240 if self.master_limiter else 0
        return {ch: self.strips[ch].latency_frames + bus for ch in CHANNELS}

    def process(self, inputs: dict[str, np.ndarray | None], mic: np.ndarray | None = None,
                stream: bool | None = None) -> dict[str, np.ndarray]:
        """One block: ``{channel: (width, block) or None}`` -> ``{bus: (2, block)}``.

        ``mic`` is the processed microphone block (mono, ``(block,)``) for Streamer mode's mix; ``stream``
        says whether that mix is wanted right now (default: whatever the settings say).
        """
        stream_on = self.stream.enabled if stream is None else stream
        outs: dict[str, np.ndarray] = {}
        rendered = {}
        raw = {}
        for name, strip in self.strips.items():
            x = inputs.get(name)
            if x is None:
                x = np.zeros((strip.width, self.block))
            rendered[name] = strip.process(x)
            if stream_on:
                raw[name] = to_stereo(x)
        stream_mix = None
        if stream_on:
            if mic is not None:
                raw["mic"] = np.vstack([mic, mic])
            stream_mix = self.stream.mix(raw, self.block)
        for bus_name, bus in self.buses.items():
            mix = np.zeros((2, self.block))
            for ch in bus.channels:
                mix += rendered[ch]
            if bus_name == MASTER_BUS:
                mix = self._apply_master(mix)
            if bus_name == STREAM_BUS and stream_mix is not None:       # channels routed here AND the stream mix
                mix = mix + stream_mix
                stream_mix = None
            if bus.limiter is None:
                outs[bus_name] = mix
                continue
            bus.quiet_blocks = bus.quiet_blocks + 1 if not np.any(mix) else 0
            outs[bus_name] = mix if bus.quiet_blocks > 1 else bus.limiter.process(mix)
        if stream_mix is not None:
            outs[STREAM_BUS] = self._limit_stream(stream_mix)
        elif self._stream_hot and self._stream_limiter is not None and not stream_on:
            # switched off: the limiter still holds its last 5 ms of audio, which a later start must not play
            self._stream_limiter.process(np.zeros((2, self.block)))
            self._stream_hot = False
        return outs

    def _limit_stream(self, mix: np.ndarray) -> np.ndarray:
        if self._stream_limiter is None:
            return mix
        self._stream_quiet = self._stream_quiet + 1 if not np.any(mix) else 0
        if self._stream_quiet > 1:
            return mix
        self._stream_hot = True
        return self._stream_limiter.process(mix)

    def process_mic(self, x: np.ndarray) -> np.ndarray:
        return self.mic.process(x)

    # ── loading state from ASM's files ───────────────────────────────────────

    def apply_playback(self, snapshot: "StateSnapshot") -> None:
        """EQ, Smart Volume and spatial audio of every strip. Quick (a few ms): runs on the audio thread."""
        self.stream.configure(snapshot.stream)
        for name, strip in self.strips.items():
            strip.configure_eq(snapshot.eq.get(name))
            if name in SPATIAL_CHANNELS:
                strip.configure_spatial(snapshot.spatial.get(name, SpatialSpec(enabled=False)),
                                        snapshot.hrir14, snapshot.hrir_id)

    def apply_mic(self, snapshot: "StateSnapshot") -> None:
        self.mic.set_spec(snapshot.mic)

    def load_state(self, config_dir: Path | None = None, hrir_id: str | None = None) -> None:
        """(Re)read every state file ASM keeps and apply it. Missing files mean "flat / defaults"."""
        snapshot = read_state(config_dir, hrir_id, self.rate)
        self.apply_playback(snapshot)
        self.apply_mic(snapshot)


@dataclass
class StateSnapshot:
    """Everything the graph takes from ASM's state files, read once (slow) and applied later (fast)."""

    eq: dict[str, dict | None] = field(default_factory=dict)
    spatial: dict[str, SpatialSpec] = field(default_factory=dict)
    hrir14: np.ndarray | None = None
    hrir_id: str | None = None
    mic: MicSpec = field(default_factory=lambda: mic_spec(None))
    stream: StreamSpec = field(default_factory=StreamSpec)


def read_state(config_dir: Path | None = None, hrir_id: str | None = None, rate: int = RATE) -> StateSnapshot:
    """Read the EQ / spatial / microphone state files and the active HRIR. Touches the disk: not for the audio thread."""
    config_dir = config_dir or default_config_dir()
    hrir14, resolved = _load_active_hrir(hrir_id, rate)
    return StateSnapshot(
        eq={name: load_eq_state(name, config_dir) for name in CHANNELS},
        spatial={name: load_spatial(name, config_dir) for name in SPATIAL_CHANNELS},
        hrir14=hrir14, hrir_id=resolved,
        mic=mic_spec(load_micro_state(config_dir)),
        stream=load_stream_spec(config_dir),
    )


_hrir_cache: dict[tuple[str, int], np.ndarray] = {}


def _load_active_hrir(hrir_id: str | None, rate: int) -> tuple[np.ndarray | None, str | None]:
    """The HRIR of the chosen profile (``GeneralSettings.hrir_id``), or the default; ``None`` if unreadable."""
    try:
        from arctis_sound_manager.hrir_catalog import package_hrir_path
        if hrir_id is None:
            try:
                from arctis_sound_manager.settings import GeneralSettings
                hrir_id = GeneralSettings.read_from_file().hrir_id
            except Exception:                                # noqa: BLE001 — settings must never stop the audio
                hrir_id = None
        for candidate in (hrir_id, DEFAULT_HRIR):
            if not candidate:
                continue
            path = package_hrir_path(candidate)
            if path is None:
                continue
            key = (candidate, rate)
            if key not in _hrir_cache:
                _hrir_cache[key] = load_hrir(path, rate)
            return _hrir_cache[key], candidate
    except Exception as exc:                                 # noqa: BLE001
        _log.warning("could not load an HRIR: %s", exc)
    return None, None
