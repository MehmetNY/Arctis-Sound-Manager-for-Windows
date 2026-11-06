# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later
"""The slice of the registry the microphone effect needs, behind a small interface so the logic that uses it
(:mod:`.attach`) is the same code in the tests (an in-memory registry) and on Windows (HKEY_LOCAL_MACHINE).

All paths are relative to HKEY_LOCAL_MACHINE and use backslashes. Values are ``Value(data, kind)`` with ``kind`` the
registry's own type number (``REG_SZ`` = 1, ``REG_DWORD`` = 4).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol

REG_SZ = 1
REG_DWORD = 4


@dataclass(frozen=True)
class Value:
    data: object
    kind: int = REG_SZ


class Registry(Protocol):
    def exists(self, path: str) -> bool: ...
    def subkeys(self, path: str) -> list[str]: ...
    def values(self, path: str) -> dict[str, Value]: ...
    def get(self, path: str, name: str) -> Value | None: ...
    def set(self, path: str, name: str, data: object, kind: int = REG_SZ) -> None: ...
    def delete_value(self, path: str, name: str) -> None: ...
    def delete_key(self, path: str) -> None: ...
    def make_writable(self, path: str) -> None: ...


class MemoryRegistry:
    """A registry in a dict (tests). ``locked`` paths refuse writes until ``make_writable`` was called for them."""

    def __init__(self, locked: set[str] | None = None):
        self._keys: dict[str, tuple[str, dict[str, Value]]] = {}      # lower-case path -> (path as written, values)
        self.locked = {p.strip("\\").lower() for p in (locked or set())}
        self.opened: list[str] = []

    @staticmethod
    def _norm(path: str) -> str:
        return path.strip("\\").lower()

    def _ensure(self, path: str) -> dict[str, Value]:
        parts = path.strip("\\").split("\\")
        for i in range(1, len(parts) + 1):
            sub = "\\".join(parts[:i])
            self._keys.setdefault(sub.lower(), (sub, {}))
        return self._keys[self._norm(path)][1]

    def add(self, path: str, values: dict[str, Value] | None = None) -> None:
        """Test helper: create a key (and its parents) with values, without the lock getting in the way."""
        self._ensure(path).update(values or {})

    def exists(self, path: str) -> bool:
        return self._norm(path) in self._keys

    def subkeys(self, path: str) -> list[str]:
        prefix = self._norm(path) + "\\"
        found: dict[str, str] = {}
        for lowered, (written, _values) in self._keys.items():
            if lowered.startswith(prefix):
                head = written.strip("\\").split("\\")[len(prefix.strip("\\").split("\\"))]
                found.setdefault(head.lower(), head)
        return sorted(found.values())

    def values(self, path: str) -> dict[str, Value]:
        return dict(self._keys.get(self._norm(path), ("", {}))[1])

    def get(self, path: str, name: str) -> Value | None:
        return self._keys.get(self._norm(path), ("", {}))[1].get(name)

    def _check(self, path: str) -> None:
        norm = self._norm(path)
        for locked in self.locked:
            if norm == locked or norm.startswith(locked + "\\"):
                raise PermissionError(f"access denied: {path}")

    def set(self, path: str, name: str, data: object, kind: int = REG_SZ) -> None:
        self._check(path)
        self._ensure(path)[name] = Value(data, kind)

    def delete_value(self, path: str, name: str) -> None:
        self._check(path)
        self._keys.get(self._norm(path), ("", {}))[1].pop(name, None)

    def delete_key(self, path: str) -> None:
        self._check(path)
        norm = self._norm(path)
        for key in [k for k in self._keys if k == norm or k.startswith(norm + "\\")]:
            del self._keys[key]

    def make_writable(self, path: str) -> None:
        norm = self._norm(path)
        self.opened.append(path)
        self.locked = {p for p in self.locked if not (norm == p or norm.startswith(p + "\\"))}


class WinRegistry:
    """HKEY_LOCAL_MACHINE, in its 64-bit view (the audio engine is a 64-bit process)."""

    def __init__(self):
        import winreg
        self._w = winreg
        self._root = winreg.HKEY_LOCAL_MACHINE
        self._wow = winreg.KEY_WOW64_64KEY

    def _open(self, path: str, access: int):
        return self._w.OpenKey(self._root, path, 0, access | self._wow)

    def exists(self, path: str) -> bool:
        try:
            with self._open(path, self._w.KEY_READ):
                return True
        except OSError:
            return False

    def subkeys(self, path: str) -> list[str]:
        out = []
        try:
            with self._open(path, self._w.KEY_READ) as key:
                i = 0
                while True:
                    try:
                        out.append(self._w.EnumKey(key, i))
                    except OSError:
                        break
                    i += 1
        except OSError:
            pass
        return out

    def values(self, path: str) -> dict[str, Value]:
        out: dict[str, Value] = {}
        try:
            with self._open(path, self._w.KEY_READ) as key:
                i = 0
                while True:
                    try:
                        name, data, kind = self._w.EnumValue(key, i)
                    except OSError:
                        break
                    out[name] = Value(data, kind)
                    i += 1
        except OSError:
            pass
        return out

    def get(self, path: str, name: str) -> Value | None:
        try:
            with self._open(path, self._w.KEY_READ) as key:
                data, kind = self._w.QueryValueEx(key, name)
                return Value(data, kind)
        except OSError:
            return None

    def set(self, path: str, name: str, data: object, kind: int = REG_SZ) -> None:
        with self._w.CreateKeyEx(self._root, path, 0, self._w.KEY_SET_VALUE | self._w.KEY_CREATE_SUB_KEY | self._wow) as key:
            self._w.SetValueEx(key, name, 0, kind, data)

    def delete_value(self, path: str, name: str) -> None:
        try:
            with self._open(path, self._w.KEY_SET_VALUE) as key:
                self._w.DeleteValue(key, name)
        except FileNotFoundError:
            pass

    def delete_key(self, path: str) -> None:
        for sub in self.subkeys(path):
            self.delete_key(path + "\\" + sub)
        try:
            self._w.DeleteKeyEx(self._root, path, self._wow, 0)
        except FileNotFoundError:
            pass

    def make_writable(self, path: str) -> None:
        from arctis_sound_manager.win.fx import acl
        acl.grant_administrators(path)


def system_registry() -> Registry:
    if sys.platform != "win32":
        raise OSError("the registry is only there on Windows")
    return WinRegistry()
