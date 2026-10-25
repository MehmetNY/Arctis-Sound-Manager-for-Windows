# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The audio engine, dressed as the PulseAudio server the GUI and the daemon were written against.

ASM's Home page reads and sets channel volumes through ``pulsectl`` (``pulse.sink_list()``,
``pulse.volume_set_all_chans(sink, 0.5)``) and the daemon sets the ChatMix through
``PulseAudioManager.set_mix``. Windows has no PulseAudio, but the engine has the same four channels
with the same volume law, so two small adapters let that code run unchanged:

* :class:`WinPulse` — the slice of ``pulsectl.Pulse`` the GUI uses, answered from the engine's cached
  status and written through the :class:`~.bridge.EngineBridge`. A channel whose cable is not
  configured simply has no sink (as Aux has none on Linux while it is switched off), and with no engine
  running the sink list is empty — the Home page then shows its usual "not connected" state.
* :class:`WindowsAudioManager` — the daemon-side ``PulseAudioManager`` (ChatMix and per-sink volume).

Programs the per-application tap (:mod:`.tap`) is carrying are listed as sink inputs — the Home page then
shows them on their channel's card and lets the user drag them to another — and ``sink_input_move`` turns
that drag into a routing rule for the program's executable. Without the tap the lists stay empty.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from arctis_sound_manager.constants import (PULSE_AUX_NODE_NAME, PULSE_CHAT_NODE_NAME, PULSE_GAME_NODE_NAME,
                                            PULSE_MEDIA_NODE_NAME)
from arctis_sound_manager.pactl import NullPulseAudioManager
from arctis_sound_manager.win.audio.bridge import EngineBridge, shared_bridge
from arctis_sound_manager.win.audio.tap import BYPASS, TAP_CHANNELS, friendly_app_name

_log = logging.getLogger(__name__)

# channel <-> the node name its Linux sink has (the GUI and channel_volumes.json are keyed by it)
CHANNEL_NODE = {"game": PULSE_GAME_NODE_NAME, "chat": PULSE_CHAT_NODE_NAME,
                "media": PULSE_MEDIA_NODE_NAME, "aux": PULSE_AUX_NODE_NAME}
NODE_CHANNEL = {node: ch for ch, node in CHANNEL_NODE.items()}
_LABEL = {"game": "Game", "chat": "Chat", "media": "Media", "aux": "Aux"}

# The headset's overall level (the Home page's Master card). On Linux that card drives the headset's own
# ALSA sink; here the engine's master level answers for it, under a name and vendor the page recognises
# as "the SteelSeries output". MASTER_NODE is the key its level is saved under in channel_volumes.json.
MASTER = "master"
MASTER_NODE = "Arctis_Master"
HEADSET_SINK_NAME = "alsa_output.usb-SteelSeries_Arctis_ASM_Engine.analog-stereo"
_STEELSERIES_VENDOR = "0x1038"
_CONTROL_OF_NODE = {**NODE_CHANNEL, MASTER_NODE: MASTER}


class _Volume:
    def __init__(self, flat: float):
        self.value_flat = float(flat)
        self.values = [float(flat)]


class WinSink:
    """What ``pulsectl.PulseSinkInfo`` carries that the GUI reads."""

    def __init__(self, channel: str, index: int, volume_pct: float, muted: bool, running: bool):
        self.channel = channel
        self.index = index
        self.name = HEADSET_SINK_NAME if channel == MASTER else CHANNEL_NODE[channel]
        self.description = "ASM audio engine - headset" if channel == MASTER else f"ASM {_LABEL[channel]}"
        self.volume = _Volume(volume_pct / 100.0)
        self.mute = int(bool(muted))
        self.state = "running" if running else "idle"
        self.proplist = {"node.name": self.name, "node.nick": self.description,
                         "node.description": self.description, "media.class": "Audio/Sink"}
        if channel == MASTER:
            self.proplist["device.vendor.id"] = _STEELSERIES_VENDOR
        self.owner_module = None


MASTER_INDEX = 1000                                              # the sink index of the headset's own (master) level
CHANNEL_INDEX = {channel: 1001 + i for i, channel in enumerate(CHANNEL_NODE)}
AUDIBLE_LEVEL = 5e-4                                             # a program quieter than this (rms) is not "playing"


class WinSinkInput:
    """What ``pulsectl.PulseSinkInputInfo`` carries that the Home page reads."""

    def __init__(self, exe: str, pid: int, sink: int, active: bool):
        stem = exe.rsplit(".", 1)[0] if exe.lower().endswith(".exe") else exe
        self.index = int(pid)
        self.sink = sink
        self.exe = exe
        self.corked = 0 if active else 1
        self.mute = 0
        self.volume = _Volume(1.0)
        self.proplist = {"application.name": friendly_app_name(exe), "application.process.binary": stem,
                         "application.process.id": str(pid), "application.icon_name": stem.lower(),
                         "media.name": "", "node.name": f"asm_tap_{stem.lower()}"}


class WinPulse:
    """The part of ``pulsectl.Pulse`` the GUI calls, backed by the engine."""

    def __init__(self, bridge: EngineBridge | None = None):
        self._bridge = bridge or shared_bridge()

    # ── queries ─────────────────────────────────────────────────────────────

    def _status(self) -> dict | None:
        status = self._bridge.status()
        return status if status and status.get("running") else None

    def sink_list(self) -> list[WinSink]:
        status = self._status()
        if status is None:
            return []
        endpoints = status.get("endpoints", {})
        volumes, muted = status.get("volumes", {}), status.get("muted", {})
        sinks = []
        for i, channel in enumerate(CHANNEL_NODE):
            ep = endpoints.get(f"in:{channel}")
            if ep is None:                                       # no cable configured for this channel
                continue
            sinks.append(WinSink(channel, 1001 + i, float(volumes.get(channel, 100.0)), bool(muted.get(channel, False)),
                                 ep.get("state") == "running"))
        headset = endpoints.get("out:headset")
        if headset is not None:                                  # the engine's master level stands in for the headset's
            sinks.append(WinSink(MASTER, 1000, float(volumes.get(MASTER, 100.0)), bool(muted.get(MASTER, False)),
                                 headset.get("state") == "running"))
        return sinks

    def source_list(self) -> list:
        return []

    def sink_input_list(self) -> list:
        """Programs playing to the headset, each on the sink of the channel it is sent to (or the master sink when
        it goes straight to the headset)."""
        status = self._status()
        tap = (status or {}).get("tap") or {}
        out: list[WinSinkInput] = []
        for app in tap.get("apps", []) or []:
            exe, pid = str(app.get("exe") or ""), int(app.get("pid") or 0)
            if not exe or not pid:
                continue
            playing = bool(app.get("active")) or float(app.get("level") or 0.0) > AUDIBLE_LEVEL
            if not playing:
                continue
            channel = str(app.get("channel") or BYPASS)
            sink = CHANNEL_INDEX.get(channel, MASTER_INDEX) if channel in TAP_CHANNELS else MASTER_INDEX
            if sink != MASTER_INDEX and f"in:{channel}" not in status.get("endpoints", {}):
                sink = MASTER_INDEX
            out.append(WinSinkInput(exe, pid, sink, True))
        return out

    def source_output_list(self) -> list:
        return []

    def card_list(self) -> list:
        return []

    def server_info(self):
        return SimpleNamespace(default_sink_name="", default_source_name="", server_name="ASM audio engine")

    # ── commands ────────────────────────────────────────────────────────────

    def volume_set_all_chans(self, obj, value: float) -> None:
        channel = getattr(obj, "channel", None) or _CONTROL_OF_NODE.get(getattr(obj, "name", ""))
        if channel is None:
            return
        self._bridge.volume(channel, max(0.0, min(1.0, float(value))) * 100.0)

    def mute(self, obj, mute: bool = True) -> None:
        channel = getattr(obj, "channel", None) or _CONTROL_OF_NODE.get(getattr(obj, "name", ""))
        if channel is not None:
            self._bridge.mute(channel, bool(mute))

    def sink_input_move(self, index, sink_index) -> None:
        """Dragging a program onto a channel's card: remember "this executable goes to that channel"."""
        app = next((a for a in ((self._status() or {}).get("tap") or {}).get("apps", []) or []
                    if int(a.get("pid") or 0) == int(index)), None)
        if app is None:
            return None
        if int(sink_index) == MASTER_INDEX:
            channel = BYPASS
        else:
            channel = next((ch for ch, i in CHANNEL_INDEX.items() if i == int(sink_index)), None)
        if channel is not None:
            self._bridge.route(str(app.get("exe") or ""), channel)
        return None

    # These exist so callers that offer the choice find something to call.
    def source_output_move(self, *_args) -> None: return None
    def source_default_set(self, *_args) -> None: return None
    def default_set(self, *_args) -> None: return None
    def card_profile_set(self, *_args) -> None: return None
    def event_mask_set(self, *_args) -> None: return None
    def event_callback_set(self, *_args) -> None: return None
    def event_listen(self, *_args, **_kwargs) -> None: return None
    def event_listen_stop(self) -> None: return None
    def close(self) -> None: return None
    def disconnect(self) -> None: return None

    # ── ChatMix (what pactl.apply_mix_to_streams / mix_stream_levels defer to) ─

    def apply_mix(self, channels_pct: int, chat_pct: int, channels: list[str]) -> None:
        self._bridge.chatmix(channels_pct, chat_pct, [c for c in channels if c in CHANNEL_NODE and c != "chat"])

    def mix_levels(self) -> dict[str, int | None]:
        """Each channel's current ChatMix level (0-100); ``None`` where the channel has no cable."""
        status = self._status()
        levels: dict[str, int | None] = {ch: None for ch in CHANNEL_NODE}
        if status is None:
            return levels
        mix = status.get("chatmix", {})
        on_dial = set(mix.get("channels") or ["game"])
        for channel in CHANNEL_NODE:
            if f"in:{channel}" not in status.get("endpoints", {}):
                continue
            if channel == "chat":
                levels[channel] = round(float(mix.get("chat_pct", 100)))
            elif channel in on_dial:
                levels[channel] = round(float(mix.get("channels_pct", 100)))
            else:
                levels[channel] = 100
        return levels


class WindowsAudioManager(NullPulseAudioManager):
    """The daemon's ``PulseAudioManager`` on Windows: ChatMix and channel volumes go to the engine; everything
    PulseAudio-specific (the sinks of a USB card, redirecting streams, default devices) answers "nothing
    there", as the null manager it extends does."""

    def __init__(self, bridge: EngineBridge | None = None):
        super().__init__()
        self._bridge = bridge or shared_bridge()
        self.pulse = WinPulse(self._bridge)

    def set_mix(self, media_mix: int, chat_mix: int) -> None:
        self._bridge.chatmix(min(100, int(media_mix)), min(100, int(chat_mix)), self._chatmix_channels())

    def set_sink_volume_by_node(self, node_name: str, pct: int) -> bool:
        channel = _CONTROL_OF_NODE.get(node_name)
        if channel is None or not self._bridge.running():
            return False
        self._bridge.volume(channel, max(0, min(100, int(pct))))
        return True

    def _chatmix_channels(self) -> list[str]:
        try:
            from arctis_sound_manager.settings import GeneralSettings
            return GeneralSettings.read_from_file().chatmix_channels_or_default()
        except Exception:                                        # noqa: BLE001 — a broken settings file: Game alone
            return ["game"]
