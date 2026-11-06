# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later
"""Three scheduled tasks that let the program (which runs as the person using the PC) ask for the one thing that needs
administrator rights - attaching or removing the microphone effect - without a permission prompt each time.

They run ``ArctisSoundManager.exe --fx <action>`` as SYSTEM, only when started by hand (no schedule), and the
signed-in user may start them: the command line is fixed, the program lives in Program Files (a standard user cannot
change it), and what it does is limited to the registry keys described in :mod:`.attach`. The installer creates them
(it is elevated anyway) and the uninstaller removes them.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Callable

_log = logging.getLogger(__name__)

TASK_PREFIX = "ASM microphone effect - "
ACTIONS = ("attach", "detach", "guard")

# D:  SYSTEM and administrators: everything; signed-in users (IU): read and execute, which is what starting a task needs
# (a task is a file for Windows' permissions: FR/FX are the file rights Task Scheduler's own tasks carry)
_SDDL = "D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FRFX;;;IU)"


def task_name(action: str) -> str:
    return TASK_PREFIX + action


def _script(exe: str) -> str:
    tasks = ", ".join(f"@{{n='{task_name(a)}'; a='{a}'}}" for a in ACTIONS)
    return f"""
$ErrorActionPreference = 'Stop'
$exe = '{exe.replace("'", "''")}'
$svc = New-Object -ComObject 'Schedule.Service'
$svc.Connect()
$root = $svc.GetFolder('\\')
foreach ($t in @({tasks})) {{
    $def = $svc.NewTask(0)
    $def.RegistrationInfo.Description = 'Arctis Sound Manager: attach or remove the microphone effect (started by the program, never on a schedule)'
    $def.Principal.UserId = 'SYSTEM'
    $def.Principal.LogonType = 5
    $def.Principal.RunLevel = 1
    $def.Settings.Enabled = $true
    $def.Settings.AllowDemandStart = $true
    $def.Settings.StartWhenAvailable = $true
    $def.Settings.DisallowStartIfOnBatteries = $false
    $def.Settings.StopIfGoingOnBatteries = $false
    $def.Settings.ExecutionTimeLimit = 'PT5M'
    $act = $def.Actions.Create(0)
    $act.Path = $exe
    $act.Arguments = '--fx ' + $t.a
    [void]$root.RegisterTaskDefinition($t.n, $def, 6, 'SYSTEM', $null, 5, '{_SDDL}')
}}
'ok'
"""


def _powershell(script: str, run: Callable = subprocess.run, timeout: float = 90) -> subprocess.CompletedProcess:
    return run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"],
               input=script, capture_output=True, text=True, timeout=timeout,
               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


#: Why the last :func:`install` failed (for the command line to print; there is no log there)
last_error = ""


def install(exe: str, run: Callable = subprocess.run) -> bool:
    """Create (or replace) the three tasks. Needs administrator rights."""
    global last_error
    last_error = ""
    if sys.platform != "win32":
        last_error = "not Windows"
        return False
    try:
        done = _powershell(_script(exe), run)
    except (OSError, subprocess.SubprocessError) as exc:
        last_error = f"could not run PowerShell: {exc}"
        _log.warning("could not create the scheduled tasks: %s", exc)
        return False
    if done.returncode != 0 or "ok" not in (done.stdout or ""):
        last_error = (done.stderr or done.stdout or f"exit code {done.returncode}").strip()[:600]
        _log.warning("creating the scheduled tasks failed: %s", last_error)
        return False
    return True


def remove(run: Callable = subprocess.run) -> None:
    if sys.platform != "win32":
        return
    for action in ACTIONS:
        try:
            run(["schtasks", "/delete", "/tn", task_name(action), "/f"], capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            pass


def installed(run: Callable = subprocess.run) -> bool:
    if sys.platform != "win32":
        return False
    try:
        done = run(["schtasks", "/query", "/tn", task_name("attach")], capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def start(action: str, run: Callable = subprocess.run) -> bool:
    """Ask Windows to run one of the tasks now (works for a standard user). Returns at once; the task runs on its own."""
    if sys.platform != "win32" or action not in ACTIONS:
        return False
    try:
        done = run(["schtasks", "/run", "/tn", task_name(action)], capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("could not start the %s task: %s", action, exc)
        return False
    return done.returncode == 0
