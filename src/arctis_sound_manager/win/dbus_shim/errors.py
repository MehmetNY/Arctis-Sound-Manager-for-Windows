# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""``dbus_next.errors`` look-alike."""
from arctis_sound_manager.win.dbus_shim.constants import FAILED


class DBusError(Exception):
    def __init__(self, type_: str = FAILED, text: str = "", reply=None):
        super().__init__(text)
        self.type = type_
        self.text = text
        self.reply = reply


class InvalidBusNameError(TypeError):
    pass


class InvalidObjectPathError(TypeError):
    pass


class InvalidInterfaceNameError(TypeError):
    pass


class InvalidMemberNameError(TypeError):
    pass


class InterfaceNotFoundError(Exception):
    pass
