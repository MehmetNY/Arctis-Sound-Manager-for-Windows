# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""A stand-in for the slice of ``dbus_next`` that ASM uses, over a local IPC.

Windows has no D-Bus session bus. The daemon<->GUI/CLI protocol is a handful of
JSON-string methods (see ``dbus_service.py``), so instead of rewriting the ~25
call sites and the three service classes, this package provides the same names
(``MessageBus``, ``Message``, ``ServiceInterface``, ``@method`` ...) backed by
:mod:`arctis_sound_manager.win.dbus_shim.ipc`. Upstream code stays untouched,
which keeps merging upstream releases cheap.

Registered as ``dbus_next`` by :func:`arctis_sound_manager.win.install_dbus_shim`.
"""
from arctis_sound_manager.win.dbus_shim import aio, constants, errors, message, service  # noqa: F401
from arctis_sound_manager.win.dbus_shim.constants import BusType, MessageType  # noqa: F401
from arctis_sound_manager.win.dbus_shim.message import Message  # noqa: F401

__asm_shim__ = True
