# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Windows-only support code. Nothing here is imported on Linux."""
from __future__ import annotations

import sys


def install_usb_shim(force: bool = False) -> bool:
    """Make ``import usb`` / ``usb.core`` / ``usb.util`` resolve to the hidapi shim.

    ASM's device code (core.py, the OLED manager, the charging-case monitor ...)
    imports ``usb`` at a dozen places. Registering the shim once, as early as
    the package import, is what keeps all of them untouched.

    Returns True when the shim was installed. A real pyusb that was already
    imported is left alone unless ``force`` is set.
    """
    existing = sys.modules.get("usb")
    if existing is not None and not getattr(existing, "__asm_shim__", False) and not force:
        return False

    from arctis_sound_manager.win import pyusb_shim

    sys.modules["usb"] = pyusb_shim
    sys.modules["usb.core"] = pyusb_shim.core
    sys.modules["usb.util"] = pyusb_shim.util
    return True


def install_dbus_shim(force: bool = False) -> bool:
    """Make ``import dbus_next`` (and its submodules) resolve to the local-IPC shim.

    Same contract as :func:`install_usb_shim`: a real dbus_next that is already
    imported is left alone unless ``force`` is set.
    """
    existing = sys.modules.get("dbus_next")
    if existing is not None and not getattr(existing, "__asm_shim__", False) and not force:
        return False

    from arctis_sound_manager.win import dbus_shim

    sys.modules["dbus_next"] = dbus_shim
    sys.modules["dbus_next.aio"] = dbus_shim.aio
    sys.modules["dbus_next.aio.message_bus"] = dbus_shim.aio.message_bus
    sys.modules["dbus_next.constants"] = dbus_shim.constants
    sys.modules["dbus_next.errors"] = dbus_shim.errors
    sys.modules["dbus_next.message"] = dbus_shim.message
    sys.modules["dbus_next.service"] = dbus_shim.service
    return True
