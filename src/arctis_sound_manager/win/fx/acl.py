# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later
"""Letting an administrator write the effect into a sound device's registry key.

The keys under ``...\\MMDevices\\Audio`` belong to the system and an administrator may only read them. Equalizer APO's
configurator does the same two steps this does: take ownership of the key, then add one entry to its access list for the
Administrators group. Nothing is removed from the list - Windows' own audio services keep every right they had.
Needs an elevated process (the installer, or the scheduled task that runs as SYSTEM).
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

SE_REGISTRY_KEY = 4
OWNER_SECURITY_INFORMATION = 0x1
DACL_SECURITY_INFORMATION = 0x4
KEY_ALL_ACCESS = 0xF003F
GRANT_ACCESS = 1
CONTAINER_INHERIT_ACE = 0x2
TRUSTEE_IS_SID = 0
TRUSTEE_IS_WELL_KNOWN_GROUP = 5
NO_MULTIPLE_TRUSTEE = 0
ADMINISTRATORS = "S-1-5-32-544"
SE_PRIVILEGE_ENABLED = 0x2
TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY = 0x20, 0x8


class _Trustee(ctypes.Structure):
    _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", ctypes.c_void_p)]


class _ExplicitAccess(ctypes.Structure):
    _fields_ = [("grfAccessPermissions", wintypes.DWORD), ("grfAccessMode", ctypes.c_int),
                ("grfInheritance", wintypes.DWORD), ("Trustee", _Trustee)]


class _Luid(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _TokenPrivileges(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Luid", _Luid), ("Attributes", wintypes.DWORD)]


def _enable_privileges(advapi, kernel, *names: str) -> None:
    token = wintypes.HANDLE()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for name in names:
            luid = _Luid()
            if not advapi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                continue
            privileges = _TokenPrivileges(1, luid, SE_PRIVILEGE_ENABLED)
            advapi.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD,
                                                     ctypes.c_void_p, ctypes.c_void_p]
            advapi.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None)
    finally:
        kernel.CloseHandle(token)


def grant_administrators(path: str) -> None:
    """Make HKEY_LOCAL_MACHINE\\<path> writable for the Administrators group (owner and one added entry)."""
    if sys.platform != "win32":
        raise OSError("only on Windows")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)          # type: ignore[attr-defined]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)          # type: ignore[attr-defined]
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.SetEntriesInAclW.argtypes = [wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.SetEntriesInAclW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    _enable_privileges(advapi, kernel, "SeTakeOwnershipPrivilege", "SeRestorePrivilege", "SeBackupPrivilege")
    name = "MACHINE\\" + path
    sid = ctypes.c_void_p()
    if not advapi.ConvertStringSidToSidW(ADMINISTRATORS, ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    old_dacl, descriptor, new_dacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        error = advapi.SetNamedSecurityInfoW(name, SE_REGISTRY_KEY, OWNER_SECURITY_INFORMATION, sid, None, None, None)
        if error:
            raise OSError(error, f"cannot take ownership of {path}")
        error = advapi.GetNamedSecurityInfoW(name, SE_REGISTRY_KEY, DACL_SECURITY_INFORMATION, None, None,
                                             ctypes.byref(old_dacl), None, ctypes.byref(descriptor))
        if error:
            raise OSError(error, f"cannot read the access list of {path}")
        entry = _ExplicitAccess(KEY_ALL_ACCESS, GRANT_ACCESS, CONTAINER_INHERIT_ACE,
                                _Trustee(None, NO_MULTIPLE_TRUSTEE, TRUSTEE_IS_SID, TRUSTEE_IS_WELL_KNOWN_GROUP, sid.value))
        error = advapi.SetEntriesInAclW(1, ctypes.byref(entry), old_dacl, ctypes.byref(new_dacl))
        if error:
            raise OSError(error, "cannot build the new access list")
        error = advapi.SetNamedSecurityInfoW(name, SE_REGISTRY_KEY, DACL_SECURITY_INFORMATION, None, None, new_dacl, None)
        if error:
            raise OSError(error, f"cannot change the access list of {path}")
    finally:
        for pointer in (new_dacl, descriptor, sid):
            if pointer.value:
                kernel.LocalFree(pointer)
