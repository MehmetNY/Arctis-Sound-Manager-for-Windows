# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""``dbus_next.message.Message`` look-alike: the fields ASM reads and writes."""
from __future__ import annotations

from typing import Any

from arctis_sound_manager.win.dbus_shim.constants import MessageType


class Message:
    def __init__(self, destination: str | None = None, path: str | None = None,
                 interface: str | None = None, member: str | None = None,
                 message_type: MessageType = MessageType.METHOD_CALL, flags: Any = 0,
                 error_name: str | None = None, reply_serial: int | None = None,
                 sender: str | None = None, unix_fds: list | None = None,
                 signature: str = "", body: list | None = None, serial: int = 0):
        self.destination = destination
        self.path = path
        self.interface = interface
        self.member = member
        self.message_type = message_type
        self.flags = flags
        self.error_name = error_name
        self.reply_serial = reply_serial
        self.sender = sender
        self.unix_fds = unix_fds or []
        self.signature = signature
        self.body = list(body) if body else []
        self.serial = serial

    def __repr__(self) -> str:
        return (f"<Message {self.message_type.name} {self.interface}.{self.member} "
                f"error={self.error_name} body={self.body!r}>")
