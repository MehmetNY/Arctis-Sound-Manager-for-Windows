# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later
"""Registering the microphone effect with Windows and attaching it to the headset's microphone, and undoing all of it.

What "attach" does, in the order it does it (an administrator only; the installer and the scheduled task run it):

1. COM: ``HKLM\\SOFTWARE\\Classes\\CLSID\\{effect}\\InprocServer32`` names the DLL, and
   ``...\\AudioEngine\\AudioProcessingObjects\\{effect}`` is the effect's registration (what Windows' audio engine reads
   to know it exists).
2. Windows' audio engine only loads effects signed by Microsoft unless its "protected audio" mode is off
   (``HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Audio\\DisableProtectedAudioDG = 1``; Equalizer APO's installer
   sets the same value). The value is only set while the effect is attached, and its previous state is kept so that
   detaching puts it back exactly.
3. Every microphone endpoint that belongs to the headset gets the effect as its *endpoint effect* (EFX: one instance per
   device, whoever records). The endpoint's earlier value, if any, is kept so that detaching restores it.
4. Windows' audio service is restarted once so the audio engine notices.

Everything goes through :class:`~.registry.Registry`, so the tests run the same code against an in-memory registry.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from arctis_sound_manager.win.fx.registry import REG_DWORD, REG_SZ, Registry

_log = logging.getLogger(__name__)

CLSID = "{A998FC6D-EC01-4F8A-BB8F-B20F8F4A5808}"
DLL_NAME = "asm_micfx.dll"
FRIENDLY_NAME = "Arctis Sound Manager microphone effect"

CAPTURE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
EFX = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},7"                  # PKEY_FX_EndpointEffectClsid
AUDIO = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Audio"
PROTECT = "DisableProtectedAudioDG"
STATE = r"SOFTWARE\ArctisSoundManager\MicFx"
COM_KEY = rf"SOFTWARE\Classes\CLSID\{CLSID}"
APO_KEY = rf"SOFTWARE\Classes\AudioEngine\AudioProcessingObjects\{CLSID}"
# IAudioProcessingObject, IAudioProcessingObjectConfiguration, IAudioProcessingObjectRT: what the effect implements
APO_INTERFACES = ("{FD7F2B29-24D0-4B5C-B177-592C39F9CA10}", "{0E5ED805-ABA6-49C3-8F9A-2B8C889C4FA8}",
                  "{9E1D6A6D-DDBC-4E95-A4C7-AD64BA37846C}")

# Names that mark a microphone as the headset's (looked for in the endpoint's own properties) and names that rule a
# device out (SteelSeries GG's Sonar adds virtual microphones with the vendor's name).
HEADSET_WORDS = ("arctis", "vid_1038")
NOT_THE_HEADSET = ("sonar", "virtual")

MARKER_NAME = "micfx-disabled.txt"


def program_data_dir() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ArctisSoundManager"


@dataclass(frozen=True)
class Endpoint:
    guid: str
    state: int
    text: str                       # every text property of the endpoint, joined and lower-cased

    @property
    def path(self) -> str:
        return f"{CAPTURE}\\{self.guid}"

    @property
    def fx_path(self) -> str:
        return f"{CAPTURE}\\{self.guid}\\FxProperties"


def endpoints(reg: Registry) -> list[Endpoint]:
    found = []
    for guid in reg.subkeys(CAPTURE):
        base = f"{CAPTURE}\\{guid}"
        state = reg.get(base, "DeviceState")
        props = reg.values(base + r"\Properties")
        text = " ".join(str(v.data) for v in props.values() if v.kind == REG_SZ).lower()
        found.append(Endpoint(guid, int(state.data) if state is not None and isinstance(state.data, int) else 0, text))
    return found


def is_headset_microphone(ep: Endpoint, words: Sequence[str] = HEADSET_WORDS) -> bool:
    return any(w in ep.text for w in words) and not any(w in ep.text for w in NOT_THE_HEADSET)


def selector(words: Sequence[str] | None = None) -> Callable[[Endpoint], bool]:
    """The test an endpoint must pass: the headset's words by default, or the words the caller names (CI)."""
    if not words:
        return is_headset_microphone
    lowered = tuple(w.lower() for w in words)
    return lambda ep: any(w in ep.text for w in lowered)


@dataclass
class Result:
    ok: bool = True
    changed: bool = False
    restarted: bool = False
    attached: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)
        _log.warning("microphone effect: %s", message)


# ── the audio service ────────────────────────────────────────────────────────

def restart_audio_service(run: Callable = subprocess.run, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Stop and start Windows' audio service (audiodg.exe goes with it, so effects are loaded or released). ~2 seconds."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        run(["net", "stop", "audiosrv", "/y"], capture_output=True, creationflags=flags, timeout=60)
        sleep(0.5)
        run(["net", "start", "audiosrv"], capture_output=True, creationflags=flags, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("could not restart the audio service: %s", exc)
        return False
    return True


# ── pieces ───────────────────────────────────────────────────────────────────

def register_com(reg: Registry, dll: str) -> bool:
    """The effect's class and registration. ``True`` if anything changed."""
    changed = False
    current = reg.get(COM_KEY + r"\InprocServer32", "")
    if current is None or str(current.data).lower() != dll.lower():
        reg.set(COM_KEY, "", FRIENDLY_NAME)
        reg.set(COM_KEY + r"\InprocServer32", "", dll)
        reg.set(COM_KEY + r"\InprocServer32", "ThreadingModel", "Both")
        changed = True
    wanted = {"FriendlyName": (FRIENDLY_NAME, REG_SZ), "Copyright": ("GPL-3.0-or-later", REG_SZ),
              "MajorVersion": (1, REG_DWORD), "MinorVersion": (0, REG_DWORD), "Flags": (0xF, REG_DWORD),
              "MinInputConnections": (1, REG_DWORD), "MaxInputConnections": (1, REG_DWORD),
              "MinOutputConnections": (1, REG_DWORD), "MaxOutputConnections": (1, REG_DWORD),
              "MaxInstances": (0xFFFFFFFF, REG_DWORD), "NumAPOInterfaces": (len(APO_INTERFACES), REG_DWORD),
              **{f"APOInterface{i}": (iid, REG_SZ) for i, iid in enumerate(APO_INTERFACES)}}
    have = reg.values(APO_KEY)
    for name, (data, kind) in wanted.items():
        if name not in have or have[name].data != data:
            reg.set(APO_KEY, name, data, kind)
            changed = True
    return changed


def unregister_com(reg: Registry) -> bool:
    existed = reg.exists(COM_KEY) or reg.exists(APO_KEY)
    reg.delete_key(COM_KEY)
    reg.delete_key(APO_KEY)
    return existed


def _write_protected(reg: Registry, path: str, name: str, data: object, kind: int) -> None:
    """Write into a system-owned key: as it is, and when Windows says no, after making it writable."""
    try:
        reg.set(path, name, data, kind)
    except PermissionError:
        reg.make_writable(path if reg.exists(path) else path.rsplit("\\", 1)[0])
        reg.set(path, name, data, kind)


def _delete_protected(reg: Registry, path: str, name: str) -> None:
    try:
        reg.delete_value(path, name)
    except PermissionError:
        reg.make_writable(path)
        reg.delete_value(path, name)


def audio_protection(reg: Registry) -> int | None:
    value = reg.get(AUDIO, PROTECT)
    return int(value.data) if value is not None and isinstance(value.data, int) else None


def disable_audio_protection(reg: Registry) -> bool:
    """Set ``DisableProtectedAudioDG = 1``, once remembering what was there. ``True`` if it had to be changed."""
    current = audio_protection(reg)
    if current == 1:
        return False
    if reg.get(STATE, "ProtectBefore") is None:
        reg.set(STATE, "ProtectBefore", "absent" if current is None else str(current), REG_SZ)
    reg.set(AUDIO, PROTECT, 1, REG_DWORD)
    return True


def restore_audio_protection(reg: Registry) -> bool:
    before = reg.get(STATE, "ProtectBefore")
    if before is None:
        return False                                              # we never changed it (or it was already off): leave it
    if str(before.data) == "absent":
        reg.delete_value(AUDIO, PROTECT)
    else:
        reg.set(AUDIO, PROTECT, int(str(before.data)), REG_DWORD)
    reg.delete_value(STATE, "ProtectBefore")
    return True


def attached_guids(reg: Registry) -> list[str]:
    """Endpoints that carry the effect as their endpoint effect right now."""
    out = []
    for ep in endpoints(reg):
        value = reg.get(ep.fx_path, EFX)
        if value is not None and str(value.data).upper() == CLSID:
            out.append(ep.guid)
    return out


# ── attach / detach ──────────────────────────────────────────────────────────

def disabled_reason(marker_dir: Path | None = None) -> str | None:
    path = (marker_dir or program_data_dir()) / MARKER_NAME
    try:
        return path.read_text(encoding="utf-8").strip() or "disabled"
    except OSError:
        return None


def attach(reg: Registry, dll: str, select: Callable[[Endpoint], bool] = is_headset_microphone,
           restart: Callable[[], bool] | None = restart_audio_service, marker_dir: Path | None = None,
           force: bool = False, protect: bool = True) -> Result:
    """Everything described in the module docstring. Safe to repeat: it changes (and restarts audio) only when needed."""
    result = Result()
    reason = None if force else disabled_reason(marker_dir)
    if reason:
        result.fail(f"the microphone effect was switched off ({reason}); enable it again in the settings")
        return result
    if not Path(dll).is_file() and sys.platform == "win32":
        result.fail(f"the effect's file is missing: {dll}")
        return result
    chosen = [ep for ep in endpoints(reg) if select(ep)]
    if not chosen:
        result.notes.append("no microphone of the headset is known to Windows yet")
    try:
        if register_com(reg, dll):
            result.changed = True
        if chosen and protect and disable_audio_protection(reg):
            result.changed = True
    except OSError as exc:
        result.fail(f"cannot register the effect: {exc}")
        return result
    for ep in chosen:
        try:
            have = reg.get(ep.fx_path, EFX)
            if have is not None and str(have.data).upper() == CLSID:
                result.attached.append(ep.guid)
                continue
            if reg.get(f"{STATE}\\Endpoints\\{ep.guid}", "Before") is None:
                reg.set(f"{STATE}\\Endpoints\\{ep.guid}", "Before", "" if have is None else str(have.data), REG_SZ)
            _write_protected(reg, ep.fx_path, EFX, CLSID, REG_SZ)
            result.attached.append(ep.guid)
            result.changed = True
        except OSError as exc:
            result.fail(f"cannot attach to {ep.guid}: {exc}")
    if result.changed and restart is not None:
        result.restarted = bool(restart())
    return result


def detach(reg: Registry, restart: Callable[[], bool] | None = restart_audio_service, reason: str = "",
           marker_dir: Path | None = None, remove_com: bool = True) -> Result:
    """Put every endpoint back as it was, the audio protection too, and forget the effect."""
    result = Result()
    state_endpoints = f"{STATE}\\Endpoints"
    for guid in reg.subkeys(state_endpoints):
        fx_path = f"{CAPTURE}\\{guid}\\FxProperties"
        before = reg.get(f"{state_endpoints}\\{guid}", "Before")
        try:
            now = reg.get(fx_path, EFX)
            if now is not None and str(now.data).upper() == CLSID:
                if before is not None and str(before.data):
                    _write_protected(reg, fx_path, EFX, str(before.data), REG_SZ)
                else:
                    _delete_protected(reg, fx_path, EFX)
                result.changed = True
            result.attached.append(guid)
        except OSError as exc:
            result.fail(f"cannot restore {guid}: {exc}")
    for guid in attached_guids(reg):                               # attached by an earlier build that kept no record
        if guid not in result.attached:
            try:
                _delete_protected(reg, f"{CAPTURE}\\{guid}\\FxProperties", EFX)
                result.changed = True
            except OSError as exc:
                result.fail(f"cannot detach from {guid}: {exc}")
    if result.ok:
        reg.delete_key(state_endpoints)
        if restore_audio_protection(reg):
            result.changed = True
        if remove_com and unregister_com(reg):
            result.changed = True
        if reg.exists(STATE) and not reg.subkeys(STATE) and not reg.values(STATE):
            reg.delete_key(STATE)
    if reason:
        try:
            folder = marker_dir or program_data_dir()
            folder.mkdir(parents=True, exist_ok=True)
            (folder / MARKER_NAME).write_text(reason + "\n", encoding="utf-8")
        except OSError as exc:
            result.notes.append(f"could not write the marker file: {exc}")
    if result.changed and restart is not None:
        result.restarted = bool(restart())
    return result


def clear_marker(marker_dir: Path | None = None) -> None:
    try:
        ((marker_dir or program_data_dir()) / MARKER_NAME).unlink()
    except OSError:
        pass


# ── looking ──────────────────────────────────────────────────────────────────

@dataclass
class Status:
    dll: str = ""
    dll_present: bool = False
    com_registered: bool = False
    protection_off: bool = False
    attached: list[str] = field(default_factory=list)
    wanted: list[str] = field(default_factory=list)                # headset microphones Windows knows
    missing: list[str] = field(default_factory=list)              # ... that do not carry the effect
    disabled: str | None = None

    @property
    def needs_attach(self) -> bool:
        return bool(self.missing) or (bool(self.wanted) and not (self.com_registered and self.protection_off))

    @property
    def ready(self) -> bool:
        return bool(self.attached) and self.com_registered and self.protection_off


def status(reg: Registry, dll: str = "", select: Callable[[Endpoint], bool] = is_headset_microphone,
           marker_dir: Path | None = None) -> Status:
    """What is in place, from readable keys only (works without administrator rights)."""
    out = Status(dll=dll, dll_present=bool(dll) and Path(dll).is_file())
    current = reg.get(COM_KEY + r"\InprocServer32", "")
    out.com_registered = current is not None and (not dll or str(current.data).lower() == dll.lower()) and reg.exists(APO_KEY)
    out.protection_off = audio_protection(reg) == 1
    out.attached = attached_guids(reg)
    out.wanted = [ep.guid for ep in endpoints(reg) if select(ep)]
    out.missing = [g for g in out.wanted if g not in out.attached]
    out.disabled = disabled_reason(marker_dir)
    return out


def audiodg_crashes(minutes: int = 30, run: Callable = subprocess.run) -> int:
    """How many times audiodg.exe crashed in the effect's DLL during the last ``minutes`` (from the Application log)."""
    query = (f"*[System[Provider[@Name='Application Error'] and (EventID=1000) and "
             f"TimeCreated[timediff(@SystemTime) <= {int(minutes) * 60000}]]]")
    try:
        done = run(["wevtutil", "qe", "Application", f"/q:{query}", "/f:xml", "/c:50", "/rd:true"], capture_output=True,
                   text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return 0
    events = re.split(r"</Event>", done.stdout or "")
    return sum(1 for e in events if "audiodg.exe" in e.lower() and DLL_NAME in e.lower())


def guard(reg: Registry, crashes: Callable[[], int] = audiodg_crashes, limit: int = 2,
          restart: Callable[[], bool] | None = restart_audio_service, marker_dir: Path | None = None) -> Result:
    """If the audio engine has crashed ``limit`` times in the effect, take the effect away and keep it away."""
    count = crashes()
    if count < limit:
        result = Result()
        result.notes.append(f"{count} crash(es) in the last half hour")
        return result
    return detach(reg, restart=restart, reason=f"audiodg.exe crashed {count} times in the effect", marker_dir=marker_dir)
