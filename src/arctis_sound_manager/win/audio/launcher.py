# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Starting (and stopping) the audio engine process.

The engine is its own process (see :mod:`.control`): the daemon starts it when ASM starts, the setup
assistant starts it to talk to it, and it keeps running if the GUI is closed. It is started *detached*
and without a console window, so closing the terminal or IDLE the user launched ASM from does not stop
the sound.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from arctis_sound_manager.win.audio.control import EngineClient

_log = logging.getLogger(__name__)

MODULE = "arctis_sound_manager.win.audio.engine_main"


def engine_command(executable: str | None = None) -> list[str]:
    """The command line that runs the engine; ``pythonw`` when there is one (no console window)."""
    exe = executable or sys.executable
    if getattr(sys, "frozen", False):                            # the installed program: one exe, the first argument says which process
        return [exe, "--engine"]
    path = Path(exe)
    if path.name.lower() == "python.exe":
        windowless = path.with_name("pythonw.exe")
        if windowless.exists():
            exe = str(windowless)
    return [exe, "-m", MODULE]


def _creation_flags() -> int:
    flags = 0
    for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    return flags


def start_engine(config_dir: Path | None = None, wait: float = 8.0, client: EngineClient | None = None,
                 popen: Callable[..., object] = subprocess.Popen, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, extra_args: list[str] | None = None) -> bool:
    """Make sure an engine answers. Returns ``True`` when one does (already running, or started and up
    within *wait* seconds); ``False`` otherwise (it may still be coming up: the first start compiles)."""
    client = client or EngineClient(config_dir, timeout=0.5)
    if client.available():
        return True
    cmd = engine_command() + (["--config-dir", str(config_dir)] if config_dir else []) + list(extra_args or [])
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = _creation_flags()
    else:
        kwargs["start_new_session"] = True
    try:
        popen(cmd, **kwargs)
    except OSError as exc:
        _log.error("could not start the audio engine: %s", exc)
        return False
    deadline = clock() + wait
    while clock() < deadline:
        if client.available():
            return True
        sleep(0.2)
    return client.available()


def stop_engine(config_dir: Path | None = None, client: EngineClient | None = None) -> bool:
    """Ask a running engine to shut down. ``True`` if there was one."""
    client = client or EngineClient(config_dir, timeout=1.0)
    try:
        client.shutdown()
        return True
    except Exception:                                            # noqa: BLE001 — nothing to stop
        return False
