# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later
"""``ArctisSoundManager.exe --fx <command>``: the microphone effect from the command line.

    attach [--match WORD ...] [--force] [--no-protect]   register the effect and attach it to the headset's microphone (administrator)
    detach [--reason TEXT]                 take it away again and put Windows back as it was (administrator)
    guard                                  detach for good if the audio engine crashed in the effect (administrator)
    status [--json]                        what is in place (anyone)
    tasks-install | tasks-remove           the three scheduled tasks that let the program ask for the above (administrator)
    run attach|detach|guard                start one of those tasks (anyone)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from arctis_sound_manager.win.fx import attach as fx
from arctis_sound_manager.win.fx import tasks

_log = logging.getLogger(__name__)


def dll_path() -> str:
    """Where ``asm_micfx.dll`` is: ``fx\\`` next to the program, or the path in ``ASM_MICFX_DLL`` (CI, a source checkout)."""
    override = os.environ.get("ASM_MICFX_DLL")
    if override:
        return override
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent / "fx" / fx.DLL_NAME)
    return str(Path(__file__).resolve().parents[4] / "build" / fx.DLL_NAME)


def program_path() -> str:
    return str(Path(sys.executable).resolve()) if getattr(sys, "frozen", False) else sys.executable


def _words(args: list[str]) -> list[str]:
    out, it = [], iter(args)
    for a in it:
        if a == "--match":
            out.append(next(it, ""))
    return [w for w in out if w]


def _print(result: fx.Result) -> int:
    print(json.dumps({"ok": result.ok, "changed": result.changed, "restarted": result.restarted,
                      "attached": result.attached, "errors": result.errors, "notes": result.notes}))
    return 0 if result.ok else 1


def main(args: list[str]) -> int:
    from arctis_sound_manager.win.fx.registry import system_registry
    command = args[0] if args else "status"
    rest = args[1:]
    if command == "status":
        s = fx.status(system_registry(), dll_path(), fx.selector(_words(rest)))
        info = {"dll": s.dll, "dll_present": s.dll_present, "com_registered": s.com_registered,
                "protection_off": s.protection_off, "attached": s.attached, "wanted": s.wanted, "missing": s.missing,
                "disabled": s.disabled, "ready": s.ready, "needs_attach": s.needs_attach, "tasks": tasks.installed()}
        print(json.dumps(info) if "--json" in rest else "\n".join(f"{k}: {v}" for k, v in info.items()))
        return 0
    if command == "attach":
        if "--force" in rest:
            fx.clear_marker()
        return _print(fx.attach(system_registry(), dll_path(), fx.selector(_words(rest)), force="--force" in rest,
                                protect="--no-protect" not in rest))
    if command == "detach":
        reason = rest[rest.index("--reason") + 1] if "--reason" in rest and rest.index("--reason") + 1 < len(rest) else ""
        return _print(fx.detach(system_registry(), reason=reason))
    if command == "guard":
        return _print(fx.guard(system_registry()))
    if command == "tasks-install":
        ok = tasks.install(program_path())
        print("ok" if ok else "failed")
        return 0 if ok else 1
    if command == "tasks-remove":
        tasks.remove()
        return 0
    if command == "run":
        ok = bool(rest) and tasks.start(rest[0])
        print("started" if ok else "could not start")
        return 0 if ok else 1
    print(__doc__)
    return 2
