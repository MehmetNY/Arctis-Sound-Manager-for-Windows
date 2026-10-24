# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""A drop-in stand-in for the slice of ``pyusb`` that ASM uses, built on hidapi.

Why a shim and not a rewrite: ASM's device layer (core.py, the OLED manager, the
charging-case monitor, the ~20 device YAML profiles) speaks HID class requests
through pyusb — ``SET_REPORT``/``GET_REPORT`` control transfers plus interrupt
reads. Windows cannot run that path: its HID class driver owns every HID
interface and libusb would need the driver replaced (Zadig), which breaks
SteelSeries GG and the headset's own HID functions. hidapi talks *through* the
Windows HID stack instead, and every request ASM makes maps onto it 1:1.

Registered as ``usb`` / ``usb.core`` / ``usb.util`` by
:func:`arctis_sound_manager.win.install_usb_shim` on Windows only.
"""
from arctis_sound_manager.win.pyusb_shim import core, util  # noqa: F401

__asm_shim__ = True
