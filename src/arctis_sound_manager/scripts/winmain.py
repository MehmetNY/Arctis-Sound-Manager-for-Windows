# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The one program the Windows installer puts on disk (``ArctisSoundManager.exe``) and what its first argument means.

ASM is several cooperating processes (the tray/window, the daemon that talks to the headset, the audio engine,
the per-application audio helper). A frozen build has one executable, so every process is this program started
with a different first argument:

    (none) / --background   the tray icon; starts the daemon first when none runs  (Start menu, Start with Windows)
    --show                  the same, but with the window open
    --gui [...]             the window, as ``asm-gui``
    --daemon                the headset daemon, which starts the audio engine
    --engine [...]          the audio engine (``--list-devices``, ``--selftest``, ``--bench`` ... as in ``engine_main``)
    --enable-autostart      register *Start with Windows* for the current user, then exit (the installer runs this)
    --disable-autostart     remove it again (the uninstaller runs this)
    --restore-volumes       give every program the volume the audio helper turned down back (the uninstaller, crashes)
    --quit-all              stop the engine politely, end every other ASM process, restore volumes (installer / uninstaller)
    --version

The same names work from a source checkout (``py -m arctis_sound_manager.scripts.winmain --daemon``).
"""
from __future__ import annotations

import os
import sys


def _prepare_environment() -> None:
    """Things that must be right before numpy/numba/Qt are imported."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # numba compiles the reverb/limiter kernels once and keeps them; the program folder is read-only
        os.environ.setdefault("NUMBA_CACHE_DIR", os.path.join(local, "arctis_manager", "numba-cache"))


def _run_engine(args: list[str]) -> int:
    from arctis_sound_manager.win.audio import engine_main
    return engine_main.main(args)


def _run_daemon(args: list[str]) -> int:
    sys.argv = [sys.argv[0]] + args
    from arctis_sound_manager.scripts import daemon
    daemon.main()
    return 0


def _run_gui(args: list[str]) -> int:
    sys.argv = [sys.argv[0]] + args
    from arctis_sound_manager.scripts import gui
    try:
        gui.main()
    except SystemExit as exit_:
        code = exit_.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


def _autostart(on: bool) -> int:
    from arctis_sound_manager.win import startup
    try:
        if on:
            startup.enable()
        else:
            startup.disable()
    except OSError as exc:
        print(f"could not change Start with Windows: {exc}", file=sys.stderr)
        return 1
    return 0


def _restore_volumes() -> int:
    from arctis_sound_manager.win.audio.tap import restore_volumes
    return 0 if restore_volumes() else 1


def _quit_all() -> int:
    import subprocess
    import time
    try:
        from arctis_sound_manager.win.audio.launcher import stop_engine
        stop_engine()                                   # politely: the audio helper puts every program's volume back
        time.sleep(1.5)
    except Exception:                                   # noqa: BLE001 — whatever is running is ended below
        pass
    if os.name == "nt":
        name = os.path.basename(sys.executable) if getattr(sys, "frozen", False) else "ArctisSoundManager.exe"
        for image in (name, "asm-audiotap.exe"):
            try:
                subprocess.run(["taskkill", "/F", "/T", "/FI", f"PID ne {os.getpid()}", "/IM", image],
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
            except (OSError, subprocess.SubprocessError):
                pass
    _restore_volumes()                                  # a process that was ended hard cannot have done it
    return 0


def _version() -> int:
    from arctis_sound_manager.utils import project_version
    print(project_version())
    return 0


def main(argv: list[str] | None = None) -> int:
    import multiprocessing
    multiprocessing.freeze_support()
    _prepare_environment()
    args = list(sys.argv[1:] if argv is None else argv)
    from arctis_sound_manager.scripts.winstart import attach_output
    attach_output()                                   # a windowed program has no stdout/stderr: give them a file
    mode = args[0] if args else "--background"
    rest = args[1:]
    if mode == "--engine":
        return _run_engine(rest)
    if mode == "--daemon":
        return _run_daemon(rest)
    if mode == "--gui":
        return _run_gui(rest)
    if mode == "--enable-autostart":
        return _autostart(True)
    if mode == "--disable-autostart":
        return _autostart(False)
    if mode == "--restore-volumes":
        return _restore_volumes()
    if mode == "--quit-all":
        return _quit_all()
    if mode == "--version":
        return _version()
    from arctis_sound_manager.scripts import winstart      # --background, --show, and anything the tray understands
    return winstart.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
