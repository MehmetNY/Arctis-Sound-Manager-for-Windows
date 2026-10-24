# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""``dbus_next.service`` look-alike: ``ServiceInterface``, ``@method``, ``@signal``.

Unlike dbus_next, a decorated method stays an ordinary callable that returns its
value, so Python code may call it directly.

Signals are real: emitting one (calling the decorated function) pushes it to every
IPC client that subscribed to ``(interface, member)``. The consumer is the DAC-wheel
on-screen display, which listens to ``StatusChanged``.
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

_MEMBER = "_asm_dbus_member"
_KIND = "_asm_dbus_kind"


def method(name: str | None = None, disabled: bool = False) -> Callable:
    def decorate(fn: Callable) -> Callable:
        setattr(fn, _MEMBER, name or fn.__name__)
        setattr(fn, _KIND, "method")
        setattr(fn, "_asm_dbus_disabled", disabled)
        return fn
    return decorate


def signal(name: str | None = None, disabled: bool = False) -> Callable:
    def decorate(fn: Callable) -> Callable:
        member = name or fn.__name__

        @functools.wraps(fn)
        def emit(self, *args, **kwargs):
            body = fn(self, *args, **kwargs)
            bus = getattr(self, "_asm_bus", None)
            if disabled or bus is None:
                log.debug("signal %s.%s has no bus to go out on", getattr(self, "name", "?"), member)
                return body
            try:
                bus._emit_signal(self.name, member, pack_result(fn, body))
            except Exception:  # noqa: BLE001 - a signal is best effort and must never break its emitter
                log.debug("signal %s.%s could not be delivered", self.name, member, exc_info=True)
            return body

        setattr(emit, _MEMBER, member)
        setattr(emit, _KIND, "signal")
        setattr(emit, "_asm_dbus_disabled", disabled)
        return emit
    return decorate


class ServiceInterface:
    def __init__(self, name: str):
        self.name = name
        self._asm_bus = None   # set by MessageBus.export(); signals go out through it

    def _asm_find_method(self, member: str) -> Callable | None:
        for attr in dir(type(self)):
            raw = getattr(type(self), attr, None)
            if (getattr(raw, _KIND, None) == "method" and getattr(raw, _MEMBER, None) == member
                    and not getattr(raw, "_asm_dbus_disabled", False)):
                return getattr(self, attr)
        return None


def describe(interface: "ServiceInterface") -> dict[str, list[str]]:
    """The members a client can use, as ``{"methods": [...], "signals": [...]}`` (what introspection returns)."""
    methods: list[str] = []
    signals: list[str] = []
    for attr in dir(type(interface)):
        raw = getattr(type(interface), attr, None)
        kind = getattr(raw, _KIND, None)
        if kind is None or getattr(raw, "_asm_dbus_disabled", False):
            continue
        (methods if kind == "method" else signals).append(getattr(raw, _MEMBER))
    return {"methods": sorted(methods), "signals": sorted(signals)}


def count_complete_types(signature: str) -> int:
    """Number of complete D-Bus types in ``signature`` (``''`` -> 0, ``'bs'`` -> 2, ``'a{sv}'`` -> 1)."""
    count, i, n = 0, 0, len(signature)

    def skip_one(pos: int) -> int:
        c = signature[pos]
        if c == "a":
            return skip_one(pos + 1)
        if c in "({":
            close = ")" if c == "(" else "}"
            pos += 1
            while pos < n and signature[pos] != close:
                pos = skip_one(pos)
            return pos + 1
        return pos + 1

    while i < n:
        i = skip_one(i)
        count += 1
    return count


def pack_result(fn: Callable, result: Any) -> list:
    """Turn a handler's return value into a reply body using its return annotation."""
    annotation = getattr(fn, "__annotations__", {}).get("return")
    sig = annotation.strip("'\"") if isinstance(annotation, str) else ""
    wanted = count_complete_types(sig)
    if wanted == 0:
        return []
    if wanted == 1:
        return [result]
    return list(result)
