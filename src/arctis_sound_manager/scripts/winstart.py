# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Start Arctis Sound Manager the way a Windows tray program starts: quietly, in the background.

This is what the *Start with Windows* switch registers (see :mod:`arctis_sound_manager.win.startup`) and
what the Start-menu entry runs. It

1. starts the daemon — detached, without a console window, logging to a file — unless one is running,
2. then becomes the tray icon (``asm-gui --systray``) in this very process.

No terminal, no taskbar button, no flash: after logging in the headset simply works. The daemon keeps
running if the tray is quit, exactly as it keeps running when the GUI of the Linux build is closed.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from arctis_sound_manager.file_lock import lock_nb, runtime_dir, unlock

logger = logging.getLogger("WinStart")

DAEMON_MODULE = "arctis_sound_manager.scripts.daemon"
LOCK_NAME = "arctis-manager.lock"
LOG_LIMIT = 2 * 1024 * 1024            # a log that grew past this starts over at the next launch


def log_dir() -> Path:
    """``%LOCALAPPDATA%\\arctis_manager\\logs`` (next to the run directory the daemon already uses)."""
    return runtime_dir().parent / "logs"


def daemon_running() -> bool:
    """True when a daemon holds the single-instance lock. Taking and dropping the lock is harmless."""
    directory = runtime_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = open(directory / LOCK_NAME, "a+")
    except OSError:
        return False
    try:
        try:
            lock_nb(handle.fileno())
        except OSError:
            return True
        try:
            unlock(handle.fileno())
        except OSError:
            pass
        return False
    finally:
        handle.close()


def daemon_command() -> list[str]:
    from arctis_sound_manager.win.startup import frozen, windowless_python
    if frozen():
        return [sys.executable, "--daemon"]
    return [windowless_python(), "-m", DAEMON_MODULE]


def _creation_flags() -> int:
    flags = 0
    for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    return flags


def _child_env() -> dict[str, str]:
    """The daemon must import the same copy of the package this process runs, installed or not."""
    env = dict(os.environ)
    env.setdefault("ARCTIS_LOG_LEVEL", "info")
    try:
        import arctis_sound_manager
        parent = str(Path(arctis_sound_manager.__file__).resolve().parent.parent)
    except Exception:                                    # noqa: BLE001 — nothing to add
        return env
    existing = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if parent not in existing:
        env["PYTHONPATH"] = os.pathsep.join([parent] + existing)
    return env


def open_log(name: str, directory: Path | None = None):
    """An append-mode, line-buffered log file; started over once it is larger than ``LOG_LIMIT``."""
    directory = directory or log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    try:
        mode = "w" if path.exists() and path.stat().st_size > LOG_LIMIT else "a"
    except OSError:
        mode = "a"
    return open(path, mode, encoding="utf-8", errors="replace", buffering=1)


def start_daemon(popen: Callable[..., object] = subprocess.Popen, running: Callable[[], bool] = daemon_running) -> bool:
    """Start the daemon unless one is already running. ``True`` when one is (or was just started)."""
    if running():
        logger.info("the daemon is already running")
        return True
    try:
        log = open_log("daemon.log")
    except OSError as exc:
        logger.warning("could not open the daemon log (%r); starting it without one", exc)
        log = subprocess.DEVNULL
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
                    "close_fds": True, "env": _child_env()}
    if os.name == "nt":
        kwargs["creationflags"] = _creation_flags()
    else:
        kwargs["start_new_session"] = True
    try:
        popen(daemon_command(), **kwargs)
    except OSError as exc:
        logger.error("could not start the daemon: %s", exc)
        return False
    finally:
        if hasattr(log, "close"):
            log.close()                                  # the child has its own copy of the handle
    logger.info("daemon started")
    return True


def attach_output() -> None:
    """Under ``pythonw`` there is no stdout/stderr at all: give print() and logging a file to write to."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        stream = open_log("tray.log")
    except OSError:
        stream = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asm-start", description="Start Arctis Sound Manager in the background.")
    parser.add_argument("--background", action="store_true", help="what the Start-with-Windows entry passes (the default)")
    parser.add_argument("--no-daemon", action="store_true", help="only start the tray icon")
    parser.add_argument("--no-tray", action="store_true", help="only start the daemon")
    parser.add_argument("--show", action="store_true", help="open the window instead of staying in the tray")
    args = parser.parse_args(argv)

    attach_output()
    logging.basicConfig(level=logging.INFO, format="%(name)20s %(levelname)8s | %(message)s")

    if os.name == "nt":
        from arctis_sound_manager.win import selfrestart
        selfrestart.wait_for_predecessor()               # a tray that replaces another one waits until that one has left

    if not args.no_daemon:
        start_daemon()
    if args.no_tray:
        return 0

    from arctis_sound_manager.scripts import gui
    sys.argv = [sys.argv[0] if sys.argv else "asm-gui"] + ([] if args.show else ["--systray"])
    try:
        gui.main()
    except SystemExit as exit_:
        code = exit_.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
