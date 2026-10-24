# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""``usb.core`` look-alike on top of hidapi (see the package docstring).

Fidelity notes — places where a HID stack cannot be a perfect libusb:

* One :class:`Device` per (vendor id, product id). Two identical dongles on one
  PC are indistinguishable here (hidapi exposes no container id).
* Interface numbers come from hidapi's ``interface_number`` (the ``MI_xx`` of the
  device path). Endpoint addresses are *fabricated* from them
  (IN = 0x80|n+1, OUT = n+1): hidapi never shows endpoint descriptors, and the
  callers only need an address that this module can map back to an interface.
* Only HID-class interfaces exist. That is all ASM ever talks to.
* "Kernel driver" calls are no-ops: there is nothing to detach on Windows.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterator

from arctis_sound_manager.win.hid_descriptor import Collection, parse_report_descriptor
from arctis_sound_manager.win.pyusb_shim import util

log = logging.getLogger(__name__)

# errno values the rest of ASM already interprets (see core.py's listen loop)
ENOENT, EIO, EBUSY, ENODEV, ETIMEDOUT = 2, 5, 16, 19, 110

VENDOR_PAGE_MIN = 0xFF00
USB_CLASS_HID = 3
_DEFAULT_REPORT_LEN = 64


class USBError(IOError):
    """Mirror of ``usb.core.USBError`` (errno is what callers branch on)."""

    def __init__(self, strerror: str, error_code: int | None = None, errno: int | None = None):
        IOError.__init__(self, errno, strerror)
        self.backend_error_code = error_code


class USBTimeoutError(USBError):
    """Mirror of ``usb.core.USBTimeoutError``."""


# ── hidapi access, injectable for tests ──────────────────────────────────────

class _HidApiBackend:
    """Thin wrapper over the ``hid`` module (pip package ``hidapi``)."""

    def __init__(self) -> None:
        self._hid: Any = None

    def _mod(self):
        if self._hid is None:
            try:
                import hid  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise ImportError(
                    "The Windows device backend needs the 'hidapi' package: pip install hidapi"
                ) from exc
            self._hid = hid
        return self._hid

    def enumerate(self, vendor_id: int, product_id: int) -> list[dict]:
        return list(self._mod().enumerate(vendor_id, product_id))

    def open_path(self, path: bytes):
        dev = self._mod().device()
        dev.open_path(path)
        return dev


_backend: Any = _HidApiBackend()


def set_backend(backend: Any) -> None:
    """Swap the hidapi backend (tests inject a fake)."""
    global _backend
    _backend = backend


def get_backend() -> Any:
    return _backend


# ── descriptors ──────────────────────────────────────────────────────────────

class Endpoint:
    def __init__(self, address: int, max_packet: int):
        self.bEndpointAddress = address
        self.wMaxPacketSize = max_packet
        self.bmAttributes = 0x03  # interrupt
        self.bInterval = 1


class Interface:
    def __init__(self, number: int, endpoints: list[Endpoint]):
        self.bInterfaceNumber = number
        self.bAlternateSetting = 0
        self.bInterfaceClass = USB_CLASS_HID
        self.bInterfaceSubClass = 0
        self.bInterfaceProtocol = 0
        self.bNumEndpoints = len(endpoints)
        self._endpoints = endpoints

    def endpoints(self) -> tuple[Endpoint, ...]:
        return tuple(self._endpoints)

    def __iter__(self) -> Iterator[Endpoint]:
        return iter(self._endpoints)

    def __len__(self) -> int:
        return len(self._endpoints)


class Configuration:
    def __init__(self, interfaces: list[Interface]):
        self.bConfigurationValue = 1
        self.bNumInterfaces = len(interfaces)
        self._interfaces = interfaces

    def __iter__(self) -> Iterator[Interface]:
        return iter(self._interfaces)

    def __len__(self) -> int:
        return len(self._interfaces)

    def __getitem__(self, key):
        number, alt = key if isinstance(key, tuple) else (key, 0)
        for intf in self._interfaces:
            if intf.bInterfaceNumber == number and intf.bAlternateSetting == alt:
                return intf
        raise IndexError(key)


# ── device ───────────────────────────────────────────────────────────────────

class _Coll:
    """One hidapi device node = one top-level HID collection of an interface."""

    def __init__(self, entry: dict):
        self.path: bytes = entry["path"]
        self.usage_page: int = entry.get("usage_page", 0) or 0
        self.usage: int = entry.get("usage", 0) or 0
        self.info: Collection | None = None
        self.info_loaded = False
        self.reader: Any = None
        self.writer: Any = None  # separate handle: a blocked read must not stall commands


class _Iface:
    def __init__(self, number: int):
        self.number = number
        self.colls: list[_Coll] = []
        self.write_lock = threading.Lock()


def _hid_failed(result: Any) -> bool:
    return result is None or (isinstance(result, int) and result < 0)


class Device:
    default_timeout = 1000

    def __init__(self, vendor_id: int, product_id: int, entries: list[dict], backend: Any):
        self.idVendor = vendor_id
        self.idProduct = product_id
        self.bus = None            # hidapi exposes no bus/port topology
        self.address = None
        self.port_numbers = None
        first = entries[0] if entries else {}
        self.manufacturer = first.get("manufacturer_string") or None
        self.product = first.get("product_string") or None
        self.serial_number = first.get("serial_number") or None
        self._backend = backend
        self._open_lock = threading.Lock()
        self._cfg: Configuration | None = None
        self._ifaces: dict[int, _Iface] = {}
        for entry in entries:
            number = entry.get("interface_number", -1)
            number = 0 if number is None or number < 0 else number
            self._ifaces.setdefault(number, _Iface(number)).colls.append(_Coll(entry))

    # -- handles ------------------------------------------------------------

    def _iface(self, number: int) -> _Iface:
        try:
            return self._ifaces[number]
        except KeyError:
            raise USBError(f"Entity not found: no HID interface {number}", errno=ENOENT) from None

    def _open(self, coll: _Coll, role: str) -> Any:
        handle = getattr(coll, role)
        if handle is not None:
            return handle
        with self._open_lock:
            handle = getattr(coll, role)
            if handle is not None:
                return handle
            try:
                handle = self._backend.open_path(coll.path)
            except (OSError, ValueError) as exc:
                # Not EACCES: ASM reads that as a Linux udev problem and pops
                # a rules dialog. On Windows this is a sharing violation, i.e.
                # another program holds the interface exclusively.
                raise USBError(
                    f"cannot open HID collection (usage page 0x{coll.usage_page:04x}): {exc}",
                    errno=EBUSY) from exc
            setattr(coll, role, handle)
            return handle

    def _info(self, coll: _Coll) -> Collection | None:
        """The parsed report descriptor of ``coll``, or None when unreadable."""
        if not coll.info_loaded:
            coll.info_loaded = True
            try:
                raw = self._open(coll, "reader").get_report_descriptor()
                parsed = parse_report_descriptor(raw)
                matching = [c for c in parsed if c.usage_page == coll.usage_page]
                coll.info = (matching or parsed or [None])[0]
            except Exception as exc:  # noqa: BLE001 - old hidapi, exclusive node, ...
                log.debug("no report descriptor for usage page 0x%04x: %r", coll.usage_page, exc)
                coll.info = None
        return coll.info

    def _coll_for(self, iface: _Iface, kind: str) -> _Coll:
        """The collection that serves ``kind`` ('input'|'output'|'feature') reports."""
        declared, unknown = [], []
        for coll in iface.colls:
            info = self._info(coll)
            if info is None:
                unknown.append(coll)
            elif getattr(info, f"{kind}_len"):
                declared.append(coll)
        pool = declared or unknown or iface.colls
        return sorted(pool, key=lambda c: 0 if c.usage_page >= VENDOR_PAGE_MIN else 1)[0]

    def _readers(self, iface: _Iface) -> list[Any]:
        candidates = []
        for coll in iface.colls:
            info = self._info(coll)
            if info is None or info.input_len:
                candidates.append(coll)
        candidates.sort(key=lambda c: 0 if c.usage_page >= VENDOR_PAGE_MIN else 1)
        handles: list[Any] = []
        last: USBError | None = None
        for coll in candidates:
            try:
                handles.append(self._open(coll, "reader"))
            except USBError as exc:
                last = exc
        if not handles:
            raise last or USBError("no readable HID collection", errno=ENOENT)
        return handles

    # -- pyusb surface ------------------------------------------------------

    def __iter__(self) -> Iterator[Configuration]:
        """``for cfg in dev`` yields configurations, as in pyusb. CoreEngine's
        ``_find_hid_device`` walks ``dev -> cfg -> interface`` this way, and swallows
        any exception as "not this device", so a missing ``__iter__`` reads as
        "no headset connected" rather than as an error."""
        yield self.get_active_configuration()

    @property
    def langids(self) -> tuple[int, ...]:
        return (0x0409,) if (self.product or self.manufacturer) else ()

    def hid_usage_page(self, interface_number: int) -> int | None:
        """Usage page of the interface's first HID collection (hidapi reports it
        directly; on Linux this is read from sysfs instead). None if unknown."""
        iface = self._ifaces.get(interface_number)
        return iface.colls[0].usage_page if iface and iface.colls else None

    def get_active_configuration(self) -> Configuration:
        if self._cfg is None:
            interfaces: list[Interface] = []
            for number in sorted(self._ifaces):
                iface = self._ifaces[number]
                infos = [self._info(c) for c in iface.colls]
                known = [i for i in infos if i is not None]
                in_len = max((i.input_len for i in known), default=0) if known else _DEFAULT_REPORT_LEN
                out_len = max((i.output_len for i in known), default=0) if known else _DEFAULT_REPORT_LEN
                endpoints = []
                if in_len:
                    endpoints.append(Endpoint(util.ENDPOINT_IN | ((number + 1) & 0x0F), in_len))
                if out_len:
                    endpoints.append(Endpoint(util.ENDPOINT_OUT | ((number + 1) & 0x0F), out_len))
                interfaces.append(Interface(number, endpoints))
            self._cfg = Configuration(interfaces)
        return self._cfg

    def set_configuration(self, configuration: int | None = None) -> None:
        return None

    def reset(self) -> None:
        log.debug("reset() is a no-op on the Windows HID backend")

    def is_kernel_driver_active(self, interface: int) -> bool:
        return False

    def detach_kernel_driver(self, interface: int) -> None:
        return None

    def attach_kernel_driver(self, interface: int) -> None:
        return None

    def _claim(self, interface: int) -> None:
        self._iface(interface)  # ENOENT for a missing interface, like libusb

    def _release(self, interface: int) -> None:
        iface = self._ifaces.get(interface)
        if iface is not None:
            self._close(iface)

    def _dispose(self) -> None:
        for iface in self._ifaces.values():
            self._close(iface)
        self._cfg = None

    @staticmethod
    def _close(iface: _Iface) -> None:
        for coll in iface.colls:
            for role in ("reader", "writer"):
                handle = getattr(coll, role)
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:  # noqa: BLE001
                        pass
                    setattr(coll, role, None)
            coll.info_loaded = False
            coll.info = None

    # -- transfers ----------------------------------------------------------

    def _set_report(self, interface: int, kind: str, report_id: int | None, data) -> int:
        iface = self._iface(interface)
        coll = self._coll_for(iface, kind)
        info = self._info(coll)
        numbered = bool(info and info.uses_report_ids)
        payload = list(data)

        if numbered:
            rid = report_id if report_id is not None else (payload[0] if payload else 0)
            # On the wire a numbered report starts with its id; hidapi wants the same.
            buf = payload if payload and payload[0] == rid else [rid] + payload
            wanted = getattr(info, f"{kind}_len", 0)
        else:
            buf = [0] + payload  # hidapi: leading 0x00 = "no report id"
            wanted = getattr(info, f"{kind}_len", 0) + 1 if info and getattr(info, f"{kind}_len", 0) else 0
        if wanted and len(buf) < wanted:
            buf = buf + [0] * (wanted - len(buf))  # Windows wants the exact report size

        handle = self._open(coll, "writer")
        try:
            with iface.write_lock:
                result = handle.write(buf) if kind == "output" else handle.send_feature_report(buf)
        except (OSError, ValueError) as exc:
            raise USBError(f"HID {kind} report failed: {exc}", errno=EIO) from exc
        if _hid_failed(result):
            raise USBError(f"HID {kind} report failed: {_error_text(handle)}", errno=EIO)
        return len(payload)

    def _get_report(self, interface: int, report_type: int, report_id: int, length: int) -> list[int]:
        iface = self._iface(interface)
        kind = {1: "input", 3: "feature"}.get(report_type)
        if kind is None:
            raise USBError(f"GET_REPORT type {report_type} is not supported", errno=EIO)
        coll = self._coll_for(iface, kind)
        info = self._info(coll)
        numbered = bool(info and info.uses_report_ids)
        handle = self._open(coll, "writer")
        ask = length if numbered else length + 1
        try:
            getter = handle.get_feature_report if kind == "feature" else handle.get_input_report
            result = getter(report_id, ask)
        except (OSError, ValueError, AttributeError) as exc:
            raise USBError(f"HID GET_REPORT failed: {exc}", errno=EIO) from exc
        if _hid_failed(result) or not result:
            raise USBError(f"HID GET_REPORT failed: {_error_text(handle)}", errno=EIO)
        data = list(result)
        # hidapi always leads with the report id byte; raw USB only does for numbered reports.
        return (data if numbered else data[1:])[:length]

    def ctrl_transfer(self, bmRequestType: int, bRequest: int, wValue: int = 0, wIndex: int = 0,
                      data_or_wLength: Any = None, timeout: int | None = None):
        is_in = bool(bmRequestType & util.CTRL_IN)
        is_class_iface = (bmRequestType & 0x60) == util.CTRL_TYPE_CLASS and (bmRequestType & 0x1F) == util.CTRL_RECIPIENT_INTERFACE
        if not is_class_iface:
            raise USBError("only HID class interface requests exist on the Windows backend", errno=EIO)

        report_type, report_id = (wValue >> 8) & 0xFF, wValue & 0xFF
        if bRequest == 0x09 and not is_in:  # SET_REPORT
            if report_type not in (2, 3):
                raise USBError(f"SET_REPORT type {report_type} is not supported", errno=EIO)
            kind = "output" if report_type == 2 else "feature"
            return self._set_report(wIndex, kind, report_id, data_or_wLength or [])
        if bRequest == 0x01 and is_in:  # GET_REPORT
            return self._get_report(wIndex, report_type, report_id, int(data_or_wLength or 0))
        raise USBError(f"request 0x{bRequest:02x} is not supported on the Windows backend", errno=EIO)

    def write(self, endpoint: int, data, timeout: int | None = None) -> int:
        if endpoint & util.ENDPOINT_IN:
            raise USBError("write() on an IN endpoint", errno=EIO)
        return self._set_report((endpoint & 0x7F) - 1, "output", None, data)

    def read(self, endpoint: int, size_or_buffer: Any, timeout: int | None = None):
        if not endpoint & util.ENDPOINT_IN:
            raise USBError("read() on an OUT endpoint", errno=EIO)
        iface = self._iface((endpoint & 0x7F) - 1)
        size = size_or_buffer if isinstance(size_or_buffer, int) else len(size_or_buffer)
        size = max(size, _DEFAULT_REPORT_LEN)
        timeout_ms = self.default_timeout if timeout is None else timeout
        readers = self._readers(iface)

        try:
            if len(readers) == 1:
                data = readers[0].read(size, max(1, timeout_ms))
            else:
                # One endpoint, several HID nodes: poll them all until the deadline.
                for handle in readers:
                    handle.set_nonblocking(1)
                deadline = time.monotonic() + timeout_ms / 1000
                data = []
                while not data:
                    for handle in readers:
                        data = handle.read(size)
                        if data:
                            break
                    if data or time.monotonic() >= deadline:
                        break
                    time.sleep(0.002)
        except (OSError, ValueError) as exc:
            text = str(exc).lower()
            gone = "not connected" in text or "no such device" in text or "removed" in text
            raise USBError(f"HID read failed: {exc}", errno=ENODEV if gone else EIO) from exc

        if isinstance(data, int):  # some hidapi builds return -1 instead of raising
            raise USBError("HID read failed", errno=EIO)
        if not data:
            raise USBTimeoutError("Operation timed out", errno=ETIMEDOUT)
        return list(data)


def _error_text(handle: Any) -> str:
    try:
        return str(handle.error())
    except Exception:  # noqa: BLE001
        return "unknown error"


# ── discovery ────────────────────────────────────────────────────────────────

def find(find_all: bool = False, backend: Any = None, custom_match=None, **args: Any):
    """``usb.core.find`` for ``idVendor`` / ``idProduct`` (the only keys ASM uses)."""
    hid_backend = backend if backend is not None else get_backend()
    vendor_id, product_id = args.get("idVendor"), args.get("idProduct")
    entries = hid_backend.enumerate(vendor_id or 0, product_id or 0)

    groups: dict[tuple[int, int], list[dict]] = {}
    for entry in entries:
        groups.setdefault((entry["vendor_id"], entry["product_id"]), []).append(entry)

    devices = [Device(v, p, es, hid_backend) for (v, p), es in sorted(groups.items())]
    if custom_match is not None:
        devices = [d for d in devices if custom_match(d)]
    if find_all:
        return iter(devices)
    return devices[0] if devices else None
