# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""What "Apply" means on Windows: leave the settings where the engine reads them, and tell it to look.

On Linux the Equalizer / Channels pages turn a channel's EQ into a PipeWire filter-chain ``.conf`` and
restart the chain; as a by-product ``generate_sonar_eq_conf`` leaves a JSON snapshot of the state it was
built from (``sonar_eq_state_<channel>.json``, ``sonar_micro_state.json``). That snapshot is exactly what
the Windows engine's graph is built from, so here we write the same files with the same writers and skip
the ``.conf``. The engine also notices changed files by itself within a second; the ``reload`` just makes
it immediate.
"""
from __future__ import annotations

import logging

from arctis_sound_manager.win.audio.bridge import shared_bridge

_log = logging.getLogger(__name__)

#: Channels the engine has an EQ for. ("output" — the EQ on an external output device — is a PipeWire
#: filter-chain on the Linux desktop's own sinks and has no counterpart here.)
ENGINE_CHANNELS = ("game", "chat", "media", "aux")


def save_channel_state(channel: str, bands, basses_db: float, voix_db: float, aigus_db: float,
                       boost_db: float = 0.0, smart_volume: dict | None = None,
                       micro_processing: dict | None = None) -> bool:
    """Write *channel*'s EQ state for the engine. ``micro_processing`` is the microphone's noise-processing
    dict (``noiseCanceling``, ``noiseGate``, ...) as ``_load_micro_proc`` returns it. ``False`` for a channel
    the engine does not process."""
    from arctis_sound_manager import sonar_to_pipewire as s2p                     # noqa: PLC0415 — heavy module
    if channel == "micro":
        proc = micro_processing or {}
        s2p._save_micro_state(list(bands), basses_db, voix_db, aigus_db, boost_db,
                              proc.get("noiseCanceling") or {}, proc)
        return True
    if channel in ENGINE_CHANNELS:
        s2p._save_eq_state(channel, list(bands), basses_db, voix_db, aigus_db, boost_db, smart_volume)
        return True
    _log.info("Windows: the %r EQ has no counterpart in the audio engine; saved only", channel)
    return False


def reload_engine() -> None:
    """Ask the engine to re-read the state files now (best effort, never blocks)."""
    try:
        shared_bridge().reload()
    except Exception:                                                              # noqa: BLE001
        _log.debug("could not queue an engine reload", exc_info=True)
