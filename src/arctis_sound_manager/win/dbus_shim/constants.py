# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""``dbus_next.constants`` look-alike (values match the D-Bus specification)."""
from enum import Enum, IntEnum

SERVICE_UNKNOWN = "org.freedesktop.DBus.Error.ServiceUnknown"
UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"
UNKNOWN_INTERFACE = "org.freedesktop.DBus.Error.UnknownInterface"
ACCESS_DENIED = "org.freedesktop.DBus.Error.AccessDenied"
FAILED = "org.freedesktop.DBus.Error.Failed"
INVALID_ARGS = "org.freedesktop.DBus.Error.InvalidArgs"


class BusType(Enum):
    SESSION = 1
    SYSTEM = 2


class MessageType(Enum):
    METHOD_CALL = 1
    METHOD_RETURN = 2
    ERROR = 3
    SIGNAL = 4


class RequestNameReply(IntEnum):
    PRIMARY_OWNER = 1
    IN_QUEUE = 2
    EXISTS = 3
    ALREADY_OWNER = 4
