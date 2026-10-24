# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The daemon's side of the audio engine: start it with ASM, stop it with ASM.

The engine runs as its own detached process (see :mod:`.launcher`), so a daemon crash does not cut the
sound off. The daemon starts one only when the engine is switched on in the settings *and* the cables are
configured (an engine with nothing to do would just sit in memory), and stops only an engine it started
itself — one the user launched by hand (``run-audio-engine.cmd``) is left alone.
"""
from __future__ import annotations

import logging
from pathlib import Path

from arctis_sound_manager.win.audio.control import EngineClient
from arctis_sound_manager.win.audio.engine_config import load_config
from arctis_sound_manager.win.audio.launcher import start_engine, stop_engine

_started_by_us = False


def engine_enabled() -> bool:
    try:
        from arctis_sound_manager.settings import GeneralSettings
        return bool(GeneralSettings.read_from_file().windows_audio_engine)
    except Exception:                                            # noqa: BLE001 — a broken settings file: default on
        return True


def start_engine_for_daemon(logger: logging.Logger, config_dir: Path | None = None, **kw) -> bool:
    """Start the engine if it is wanted and ready to work. Returns whether one is now answering."""
    global _started_by_us
    if not engine_enabled():
        logger.info("Windows: the audio engine is switched off in the settings.")
        return False
    client = EngineClient(config_dir, timeout=0.5)
    if client.available():
        logger.info("Windows: an audio engine is already running; leaving it as it is.")
        return True
    problems = load_config(config_dir).problems()
    if problems:
        logger.info("Windows: the audio engine is not set up yet (%s) - open ASM > Settings > Audio engine.",
                    "; ".join(problems))
        return False
    up = start_engine(config_dir, wait=0.0, client=client, **kw)       # do not hold the daemon's start-up
    _started_by_us = True
    logger.info("Windows: audio engine launched%s.", "" if up else " (it is still starting)")
    return up


def stop_engine_for_daemon(logger: logging.Logger, config_dir: Path | None = None, **kw) -> bool:
    """Stop the engine the daemon started (if it did)."""
    global _started_by_us
    if not _started_by_us:
        return False
    _started_by_us = False
    stopped = stop_engine(config_dir, **kw)
    logger.info("Windows: audio engine %s.", "stopped" if stopped else "was already gone")
    return stopped


def apply_engine_setting(logger: logging.Logger, enabled: bool, config_dir: Path | None = None, **kw) -> bool:
    """The user flipped "Process sound with the ASM audio engine" (the daemon has already saved the setting).

    On: start the engine when it is ready to work. Off: stop whichever engine answers - this one is an
    explicit request, so an engine somebody launched by hand goes too. Returns whether an engine is
    answering afterwards.
    """
    global _started_by_us
    if enabled:
        return start_engine_for_daemon(logger, config_dir, **kw)
    _started_by_us = False
    stopped = stop_engine(config_dir, **kw)
    logger.info("Windows: audio engine switched off in the settings (%s).",
                "stopped" if stopped else "none was running")
    return False
