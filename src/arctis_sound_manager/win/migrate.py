# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""One-time repairs for what an older build of the Windows port left behind.

Each repair runs once per user (a mark is kept in ``%LOCALAPPDATA%\\arctis_manager\\migrations.json``), at
the start of the daemon, before the audio engine is started.

* ``engine_on`` (1): the "process sound with the ASM audio engine" switch was saved *off* by some early builds
  (and by people trying them out). Nothing in the program asks for it to be off any more, and with it off
  the headset page, the equaliser and the microphone settings change nothing, so it is switched on once.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

VERSION = {"engine_on": 1}


def marks_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "arctis_manager" / "migrations.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def run(logger: logging.Logger | None = None, path: Path | None = None, load=None, save=None) -> list[str]:
    """Run what has not run yet. *load* returns the general settings object, *save* writes it back.
    Returns the names of the repairs that changed something."""
    log = logger or logging.getLogger(__name__)
    path = path or marks_path()
    marks = _read(path)
    changed: list[str] = []
    if marks.get("engine_on", 0) < VERSION["engine_on"]:
        try:
            if load is None:
                from arctis_sound_manager.settings import GeneralSettings
                load = GeneralSettings.read_from_file
                save = lambda gs: gs.write_to_file()                        # noqa: E731
            gs = load()
            if not getattr(gs, "windows_audio_engine", True):
                gs.windows_audio_engine = True
                save(gs)
                changed.append("engine_on")
                log.info("Windows: the audio engine switch was off from an older build; switched on once.")
            marks["engine_on"] = VERSION["engine_on"]
        except Exception as exc:                                              # noqa: BLE001 — never stop the daemon over this
            log.warning("Windows: migration engine_on failed: %r", exc)
    if marks:
        _write(path, marks)
    return changed
