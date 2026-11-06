# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later
"""Versions up to 1.6 installed VB-CABLE (a third-party virtual audio cable) for the processed microphone. This version does
not need it: the installer removes the one the earlier installer put there, so that Windows' sound panel shows the
headset and nothing else.

"The one the earlier installer put there" is recognised by the folder it left next to the program (``vbcable\\`` with the
driver pack): a VB-CABLE the person installed themselves is never touched. ``/KEEPCABLE=1`` on the installer's command line
keeps even that one.

The driver package is found by looking into Windows' own driver store (``C:\\Windows\\INF\\oem*.inf``) instead of parsing
``pnputil``'s output, which is translated into every language. ``pnputil /delete-driver ... /uninstall /force`` takes the
devices (and so the "CABLE Input", "CABLE Output" and "CABLE In 16ch" endpoints) and the package away.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)

PACK_DIR = "vbcable"
# What marks a driver package as VB-CABLE's: its hardware id / service name, not the (changeable) display names.
_SIGNS = ("vbaudiovacwdm", "vbaudio_cable")
_OEM = re.compile(r"^oem\d+\.inf$", re.IGNORECASE)


def _text(path: Path) -> str:
    """The INF as text: Windows' driver store keeps UTF-16 files with a byte-order mark, vendors ship either."""
    try:
        raw = path.read_bytes()[:200_000]
    except OSError:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="ignore").lower()
    return raw.decode("latin-1", errors="ignore").lower()


def find_packages(inf_dir: Path | None = None) -> list[str]:
    """Names (``oem12.inf``) of the installed driver packages that are VB-CABLE's."""
    folder = inf_dir or Path(os.environ.get("SystemRoot", r"C:\Windows")) / "INF"
    try:
        names = sorted(p for p in folder.iterdir() if _OEM.match(p.name))
    except OSError:
        return []
    return [p.name for p in names if any(sign in _text(p) for sign in _SIGNS)]


def has_pack(app_dir: Path) -> bool:
    return (app_dir / PACK_DIR).is_dir()


def remove(app_dir: Path, inf_dir: Path | None = None, run: Callable = subprocess.run, keep: bool = False) -> dict:
    """Take the earlier installer's VB-CABLE away. Returns what happened: ``{"skipped": why}`` or ``{"removed": [...]}``."""
    if keep:
        return {"skipped": "kept on request"}
    if not has_pack(app_dir):
        return {"skipped": "this machine's VB-CABLE (if any) was not installed by ASM"}
    out: dict = {"removed": [], "failed": []}
    for name in find_packages(inf_dir):
        try:
            done = run(["pnputil", "/delete-driver", name, "/uninstall", "/force"], capture_output=True, text=True,
                       timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            (out["removed"] if done.returncode in (0, 3010) else out["failed"]).append(name)      # 3010: done, restart wanted
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning("could not remove %s: %s", name, exc)
            out["failed"].append(name)
    if not out["failed"]:
        shutil.rmtree(app_dir / PACK_DIR, ignore_errors=True)         # the pack has done its job; keep it if something is left
    return out
