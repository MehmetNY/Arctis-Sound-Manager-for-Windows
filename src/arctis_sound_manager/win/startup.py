# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Start with Windows: the way every other tray program does it.

One value under ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``. Nothing needs administrator
rights, nothing is left behind when the value is removed, and Windows lists it under *Settings → Apps →
Startup* where the user can also switch it off by hand.

What the value runs is :mod:`arctis_sound_manager.scripts.winstart`, which starts the daemon (and through
it the audio engine) without a console window and then becomes the tray icon — so after logging in the
program is simply *there*, in the background, with no window and no terminal.

Two details that are easy to get wrong:

* A ``Run`` value is limited to 260 characters on some Windows versions. Running from a source checkout
  needs a ``sys.path`` tweak, which does not fit on a command line that short, so it lives in a tiny
  ``start.pyw`` next to the other per-user files and the value only names that file.
* Since Windows 8, *Task Manager → Startup* keeps its own on/off flag under ``StartupApproved\\Run``. A
  program that is switched off there stays off no matter what is in ``Run``, so ``is_enabled`` honours the
  flag and ``enable`` clears it (the user just asked, in our own settings, for the program to start).
"""
from __future__ import annotations

import logging
import os
import sys
import sysconfig
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "ArctisSoundManager"
ARGUMENT = "--background"
#: What the registry value is told to run when the program is not a frozen executable.
SCRIPT_NAME = "start.pyw"

_APPROVED_ENABLED = bytes([0x02]) + bytes(11)       # what Task Manager writes when it switches an item on


def _winreg():
    import winreg
    return winreg


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "arctis_manager"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def windowless_python(executable: str | None = None) -> str:
    """``pythonw.exe`` next to *executable* when there is one: no console window flashes at login."""
    exe = executable or sys.executable
    path = Path(exe)
    if path.name.lower() == "python.exe":
        candidate = path.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return exe


def _package_parent() -> Path:
    import arctis_sound_manager
    return Path(arctis_sound_manager.__file__).resolve().parent.parent


def _installed_package(parent: Path) -> bool:
    """True when the package sits in a site-packages directory (so ``-m`` finds it by itself)."""
    paths = {sysconfig.get_paths().get(key) for key in ("purelib", "platlib")}
    paths.discard(None)
    try:
        return any(Path(p).resolve() == parent for p in paths)
    except OSError:
        return False


def launcher_script_text(package_parent: Path | None) -> str:
    lines = ["# Written by Arctis Sound Manager: starts it in the background when Windows starts.",
             "import sys"]
    if package_parent is not None:
        lines.append(f"sys.path.insert(0, {str(package_parent)!r})")
    lines += ["from arctis_sound_manager.scripts.winstart import main",
              "raise SystemExit(main())", ""]
    return "\n".join(lines)


def startup_command(executable: str | None = None, script_dir: Path | None = None) -> str:
    """The command line stored in the ``Run`` value (and the helper script it needs, written on demand)."""
    if frozen():
        return f'"{executable or sys.executable}" {ARGUMENT}'
    script = (script_dir or data_dir()) / SCRIPT_NAME
    return f'"{windowless_python(executable)}" "{script}"'


def write_launcher_script(script_dir: Path | None = None) -> Path:
    directory = script_dir or data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    parent = _package_parent()
    path = directory / SCRIPT_NAME
    text = launcher_script_text(None if _installed_package(parent) else parent)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------------------------------- registry
def _read(key_path: str, name: str):
    wr = _winreg()
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, key_path, 0, wr.KEY_READ) as key:
            return wr.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _delete(key_path: str, name: str) -> None:
    wr = _winreg()
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, key_path, 0, wr.KEY_SET_VALUE) as key:
            wr.DeleteValue(key, name)
    except OSError:
        pass                                         # not there: nothing to remove


def registered_command() -> str | None:
    value = _read(RUN_KEY, VALUE_NAME)
    return value if isinstance(value, str) and value.strip() else None


def switched_off_by_user() -> bool:
    """Task Manager's own switch: an odd first byte in the StartupApproved value means *disabled*."""
    flags = _read(APPROVED_KEY, VALUE_NAME)
    return isinstance(flags, (bytes, bytearray)) and len(flags) > 0 and bool(flags[0] & 1)


def is_enabled() -> bool:
    return registered_command() is not None and not switched_off_by_user()


def enable(executable: str | None = None, script_dir: Path | None = None) -> str:
    """Register the program to start with Windows. Returns the command that was stored."""
    if not frozen():
        write_launcher_script(script_dir)
    command = startup_command(executable, script_dir)
    wr = _winreg()
    with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_SET_VALUE) as key:
        wr.SetValueEx(key, VALUE_NAME, 0, wr.REG_SZ, command)
    if _read(APPROVED_KEY, VALUE_NAME) is not None:
        try:
            with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, APPROVED_KEY, 0, wr.KEY_SET_VALUE) as key:
                wr.SetValueEx(key, VALUE_NAME, 0, wr.REG_BINARY, _APPROVED_ENABLED)
        except OSError as exc:
            logger.warning("could not clear the Task Manager switch for the startup entry: %r", exc)
    logger.info("start with Windows: registered %s", command)
    return command


def disable() -> None:
    _delete(RUN_KEY, VALUE_NAME)
    _delete(APPROVED_KEY, VALUE_NAME)
    logger.info("start with Windows: removed")
