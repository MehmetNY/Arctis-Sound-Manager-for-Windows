"""The names of the HID interfaces Windows currently has, without opening any of them.

``hid.enumerate`` answers "which devices are there?" by opening every HID interface of the PC (keyboards, mice,
controllers, RGB lighting, the headset's own nodes: twenty to sixty ``CreateFile`` calls) to read its ids and
strings. The daemon asks that question for as long as the program runs, to notice a headset that is plugged in or
taken out, so with the PC left on all day the answer was bought tens of thousands of times for a result that rarely
changes, and every one of those calls is a chance for the driver stack or the C library to keep something back.

Windows also keeps the list of interface *names* (``\\\\?\\HID#VID_1038&PID_12E5&MI_04#...``) and hands it out in one
call that opens nothing: ``CM_Get_Device_Interface_List``. The vendor and product ids are in the name, so "is
there a SteelSeries device, and is it the same set as a moment ago?" can be answered from that list alone. Only
when the set changes is a real enumeration needed.

Pure ctypes (``cfgmgr32.dll`` is part of Windows). Anywhere else, or if the call ever fails, :func:`interface_paths`
returns ``None`` and the caller falls back to enumerating.
"""
from __future__ import annotations

import ctypes
import sys
from typing import Sequence

CR_SUCCESS = 0
CR_BUFFER_SMALL = 0x1A
_PRESENT_ONLY = 0          # CM_GET_DEVICE_INTERFACE_LIST_PRESENT


class _Guid(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


# GUID_DEVINTERFACE_HID {4D1E55B2-F16F-11CF-88CB-001111000030}
_HID_GUID = _Guid(0x4D1E55B2, 0xF16F, 0x11CF, (ctypes.c_ubyte * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))

_lib = None


def parse_multi_sz(text: str) -> list[str]:
    """A double-NUL-terminated list of strings, as Windows returns them, as a Python list."""
    return [part for part in text.split("\0") if part]


def _cfgmgr():
    global _lib
    if _lib is None:
        lib = ctypes.WinDLL("cfgmgr32")                           # type: ignore[attr-defined]  # Windows only
        pointer = ctypes.c_void_p
        lib.CM_Get_Device_Interface_List_SizeW.argtypes = [ctypes.POINTER(ctypes.c_ulong), pointer, pointer,
                                                           ctypes.c_ulong]
        lib.CM_Get_Device_Interface_List_SizeW.restype = ctypes.c_ulong
        lib.CM_Get_Device_Interface_ListW.argtypes = [pointer, pointer, pointer, ctypes.c_ulong, ctypes.c_ulong]
        lib.CM_Get_Device_Interface_ListW.restype = ctypes.c_ulong
        _lib = lib
    return _lib


def interface_paths() -> list[str] | None:
    """Lower-case names of every present HID interface, or ``None`` when Windows cannot say (or this is not Windows)."""
    if sys.platform != "win32":
        return None
    try:
        lib = _cfgmgr()
        guid = ctypes.byref(_HID_GUID)
        for _ in range(4):                                         # the list can grow between the two calls
            size = ctypes.c_ulong(0)
            if lib.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), guid, None, _PRESENT_ONLY) != CR_SUCCESS:
                return None
            buffer = ctypes.create_unicode_buffer(max(size.value, 2))
            result = lib.CM_Get_Device_Interface_ListW(guid, None, buffer, size.value, _PRESENT_ONLY)
            if result == CR_SUCCESS:
                return [part.lower() for part in parse_multi_sz(ctypes.wstring_at(buffer, size.value))]
            if result != CR_BUFFER_SMALL:
                return None
    except Exception:  # noqa: BLE001 - never let a discovery shortcut take the daemon down
        return None
    return None


def signature(paths: Sequence[str], vendor_id: int) -> tuple[str, ...]:
    """The interface names that belong to ``vendor_id``, sorted: equal signatures mean the same devices.

    USB devices carry ``vid_1038`` in their name; a Bluetooth HID device has ``vid&021038`` (USB-IF vendor id) or
    ``vid&011038`` (Bluetooth SIG vendor id). Anything that might be the vendor's counts, so the shortcut can only
    ever err towards enumerating for real.
    """
    markers = (f"vid_{vendor_id:04x}", f"vid&02{vendor_id:04x}", f"vid&01{vendor_id:04x}")
    return tuple(sorted(path for path in paths if any(marker in path for marker in markers)))
