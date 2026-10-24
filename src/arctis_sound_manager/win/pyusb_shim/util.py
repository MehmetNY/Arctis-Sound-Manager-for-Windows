# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""``usb.util`` look-alike for the Windows HID backend."""
from __future__ import annotations

ENDPOINT_IN = 0x80
ENDPOINT_OUT = 0x00

CTRL_OUT = 0x00
CTRL_IN = 0x80

CTRL_TYPE_STANDARD = 0x00
CTRL_TYPE_CLASS = 0x20
CTRL_TYPE_VENDOR = 0x40

CTRL_RECIPIENT_DEVICE = 0x00
CTRL_RECIPIENT_INTERFACE = 0x01
CTRL_RECIPIENT_ENDPOINT = 0x02
CTRL_RECIPIENT_OTHER = 0x03


def build_request_type(direction: int, type: int, recipient: int) -> int:  # noqa: A002 (pyusb's name)
    return direction | type | recipient


def endpoint_direction(address: int) -> int:
    return address & ENDPOINT_IN


def claim_interface(device, interface: int) -> None:
    device._claim(interface)


def release_interface(device, interface: int) -> None:
    device._release(interface)


def dispose_resources(device) -> None:
    device._dispose()


def find_descriptor(desc, find_all: bool = False, custom_match=None, **args):
    """``usb.util.find_descriptor``: search a configuration/interface for a child
    whose attributes equal ``args`` and which satisfies ``custom_match``."""
    def matches(item) -> bool:
        return all(getattr(item, key, None) == value for key, value in args.items()) and (
            custom_match is None or custom_match(item))

    found = (item for item in desc if matches(item))
    return found if find_all else next(found, None)
