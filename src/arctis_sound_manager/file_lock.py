# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Portable advisory file lock and per-user runtime directory.

POSIX keeps the exact behaviour the code base always had (``fcntl.flock`` on a
file descriptor, ``XDG_RUNTIME_DIR``). Windows gets the nearest equivalent:
``msvcrt.locking`` on the first byte of the file, and a runtime directory under
``%LOCALAPPDATA%``.

Both lock flavours are *non-blocking* and are released by the OS when the
owning process dies, however it dies, so there is never a stale lock to reap.
That property is the whole reason the callers use a lock instead of a pid file.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

IS_WINDOWS: bool = sys.platform == "win32"

if IS_WINDOWS:
    import msvcrt

    def lock_nb(fd: int) -> None:
        """Take an exclusive lock on ``fd`` or raise ``OSError`` at once."""
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def lock_nb(fd: int) -> None:
        """Take an exclusive lock on ``fd`` or raise ``OSError`` at once."""
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def runtime_dir() -> Path:
    """Directory for lock files and short-lived stamps.

    POSIX: ``$XDG_RUNTIME_DIR`` when set, else ``/tmp/arctis-<uid>`` (the
    location the daemon has always used). Windows: ``%LOCALAPPDATA%\\arctis_manager\\run``,
    falling back to the temp directory.
    """
    env = os.environ.get("XDG_RUNTIME_DIR")
    if env:
        return Path(env)
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return Path(base) / "arctis_manager" / "run"
    return Path(f"/tmp/arctis-{os.getuid()}")
