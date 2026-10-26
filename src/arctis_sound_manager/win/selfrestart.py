# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Replace the running tray with a fresh one, without stopping the daemon or the audio engine.

``os.execv`` cannot do this on Windows for the installed program (it re-runs it with ``--systray``, which the one
``ArctisSoundManager.exe`` does not take) and the new process would find the old tray still listening on the
single-instance pipe, hand it a message and leave. So:

1. the old tray starts a replacement (``--background``) that carries ``ASM_RESTART_AFTER_PID=<old pid>``,
2. the old tray leaves (its pipe and its icon go with it),
3. the replacement's first act is :func:`wait_for_predecessor`: it waits until that process has ended, then starts
   as a normal first instance. The daemon and the engine are not touched; ``winstart`` finds the daemon running.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Callable

log = logging.getLogger("SelfRestart")

WAIT_ENV = "ASM_RESTART_AFTER_PID"
WAIT_TIMEOUT_S = 20.0

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def replacement_command() -> list[str]:
    from arctis_sound_manager.win.startup import frozen, windowless_python
    if frozen():
        return [sys.executable, "--background"]
    return [windowless_python(), "-m", "arctis_sound_manager.scripts.winmain", "--background"]


def respawn(popen: Callable[..., object] = subprocess.Popen, command: list[str] | None = None,
            log_name: str | None = "tray.log") -> bool:
    """Start the replacement (the tray by default; any *command* — the engine uses this too). ``True`` when it was
    started (the caller then leaves). Its output goes to *log_name* in the logs folder, or nowhere."""
    from arctis_sound_manager.scripts.winstart import _child_env, _creation_flags, open_log
    env = _child_env()
    env[WAIT_ENV] = str(os.getpid())
    out = subprocess.DEVNULL
    if log_name:
        try:
            out = open_log(log_name)
        except OSError:
            out = subprocess.DEVNULL
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT, "close_fds": True,
                    "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = _creation_flags()
    else:
        kwargs["start_new_session"] = True
    try:
        popen(command or replacement_command(), **kwargs)
    except OSError as exc:
        log.error("could not start the replacement process: %s", exc)
        return False
    finally:
        if hasattr(out, "close"):
            out.close()
    return True


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False                                         # gone (or never ours to look at)
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def wait_for_predecessor(timeout: float = WAIT_TIMEOUT_S, alive: Callable[[int], bool] = pid_alive,
                         sleep: Callable[[float], None] = time.sleep,
                         clock: Callable[[], float] = time.monotonic) -> bool:
    """Wait for the tray that started this process to end. ``True`` when there was none or it has ended."""
    value = os.environ.pop(WAIT_ENV, "")
    if not value.isdigit() or int(value) == os.getpid():
        return True
    pid = int(value)
    end = clock() + timeout
    while alive(pid):
        if clock() >= end:
            log.warning("the previous tray (pid %d) is still running after %.0f s; starting anyway", pid, timeout)
            return False
        sleep(0.1)
    return True
