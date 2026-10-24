# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

import sys as _sys

if _sys.platform == "win32":
    # Windows has no libusb path to the headset (the HID class driver owns every
    # HID interface) and no D-Bus session bus, so `import usb` / `import dbus_next`
    # are answered by look-alikes. Done here, at package import, so every module
    # that imports them is covered without being edited.
    from arctis_sound_manager.win import install_dbus_shim as _install_dbus_shim
    from arctis_sound_manager.win import install_usb_shim as _install_usb_shim

    _install_usb_shim()
    _install_dbus_shim()  # no session bus on Windows: same names, local IPC underneath
