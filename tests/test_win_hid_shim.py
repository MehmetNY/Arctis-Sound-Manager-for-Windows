"""Windows HID layer: descriptor parser, pyusb-compatible shim, portable locks.

Everything here runs on Linux CI: hidapi is replaced by a fake, and the one
module that branches on ``sys.platform`` is loaded as a private copy so the
real one is never disturbed. What these tests cannot prove is how a *real*
Windows HID stack behaves; that is what ``asm-win-probe`` is for.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from arctis_sound_manager.win import install_usb_shim
from arctis_sound_manager.win.hid_descriptor import parse_report_descriptor
from arctis_sound_manager.win.pyusb_shim import core, util

# ── report descriptors ───────────────────────────────────────────────────────

# Vendor page 0xFFC0, usage 1, unnumbered: 64-byte input + 64-byte output.
VENDOR_UNNUMBERED = bytes.fromhex(
    "06C0FF 0901 A101 1500 26FF00 7508 9540 0901 8102 0901 9102 C0".replace(" ", ""))
# Vendor page 0xFF00, report id 6: 63 payload bytes each way (+1 id = 64).
VENDOR_NUMBERED = bytes.fromhex(
    "0600FF 0901 A101 8506 7508 953F 0901 8102 0901 9102 C0".replace(" ", ""))
# Vendor page with a 33-byte feature report only (no input/output).
VENDOR_FEATURE_ONLY = bytes.fromhex(
    "06C0FF 0901 A101 7508 9521 0901 B102 C0".replace(" ", ""))
# Consumer control: input only, 2 bytes.
CONSUMER_INPUT = bytes.fromhex("050C 0901 A101 1500 26FF00 7508 9502 0901 8102 C0".replace(" ", ""))


class TestDescriptorParser:
    def test_unnumbered_vendor_collection(self):
        (c,) = parse_report_descriptor(VENDOR_UNNUMBERED)
        assert (c.usage_page, c.usage) == (0xFFC0, 1)
        assert not c.uses_report_ids
        assert (c.input_len, c.output_len, c.feature_len) == (64, 64, 0)

    def test_numbered_vendor_collection_counts_the_id_byte(self):
        (c,) = parse_report_descriptor(VENDOR_NUMBERED)
        assert c.uses_report_ids and c.report_ids == {6}
        assert (c.input_len, c.output_len) == (64, 64)

    def test_feature_only(self):
        (c,) = parse_report_descriptor(VENDOR_FEATURE_ONLY)
        assert (c.input_len, c.output_len, c.feature_len) == (0, 0, 33)

    def test_nested_collection_is_one_top_level_collection(self):
        # Application { Logical { input } }
        desc = bytes.fromhex("0600FF 0901 A101 0902 A100 7508 9504 0901 8102 C0 C0".replace(" ", ""))
        cols = parse_report_descriptor(desc)
        assert len(cols) == 1 and cols[0].input_len == 4

    def test_two_top_level_collections(self):
        cols = parse_report_descriptor(VENDOR_UNNUMBERED + CONSUMER_INPUT)
        assert [c.usage_page for c in cols] == [0xFFC0, 0x000C]

    def test_push_pop_restores_global_state(self):
        # Push, widen the report to 8x16 bits, pop, then an input item uses the old 8x2.
        desc = bytes.fromhex(
            "0600FF 0901 A101 7508 9502 A4 7510 9508 B4 0901 8102 C0".replace(" ", ""))
        (c,) = parse_report_descriptor(desc)
        assert c.input_len == 2

    def test_long_item_is_skipped(self):
        desc = bytes.fromhex("0600FF 0901 A101 FE02 10 AABB 7508 9501 0901 8102 C0".replace(" ", ""))
        (c,) = parse_report_descriptor(desc)
        assert c.input_len == 1

    def test_truncated_descriptor_does_not_raise(self):
        assert parse_report_descriptor(VENDOR_UNNUMBERED[:7]) is not None
        assert parse_report_descriptor(b"") == []


# ── a fake hidapi ────────────────────────────────────────────────────────────

class _Handle:
    """Per-path state is shared so a test can inspect whatever handle did the I/O."""

    def __init__(self, state: dict):
        self.s = state

    def get_report_descriptor(self):
        if self.s["descriptor"] is None:
            raise OSError("no descriptor")
        return list(self.s["descriptor"])

    def write(self, buf):
        self.s["writes"].append(list(buf))
        return self.s.get("write_result", len(buf))

    def send_feature_report(self, buf):
        self.s["features"].append(list(buf))
        return self.s.get("write_result", len(buf))

    def get_feature_report(self, report_id, n):
        self.s["feature_asks"].append((report_id, n))
        return list(self.s["feature_reply"])

    def read(self, n, timeout_ms=0):
        self.s["reads"].append((n, timeout_ms))
        if self.s.get("read_error"):
            raise OSError(self.s["read_error"])
        queue = self.s["read_queue"]
        return queue.pop(0) if queue else []

    def set_nonblocking(self, value):
        self.s["nonblocking"] = value

    def close(self):
        self.s["closed"] += 1

    def error(self):
        return "boom"


class _Backend:
    def __init__(self, nodes: list[dict]):
        """nodes: dicts with the hid.enumerate keys plus 'descriptor' and optional 'open_error'."""
        self.entries, self.state = [], {}
        for i, node in enumerate(nodes):
            path = f"\\\\?\\hid#vid_1038&pid_{node['product_id']:04x}&mi_{i:02x}".encode()
            self.entries.append({
                "path": path, "vendor_id": node.get("vendor_id", 0x1038),
                "product_id": node["product_id"], "interface_number": node["interface_number"],
                "usage_page": node["usage_page"], "usage": node.get("usage", 1),
                "product_string": node.get("product_string", "Fake Headset"),
            })
            self.state[path] = {
                "descriptor": node["descriptor"], "writes": [], "features": [], "feature_asks": [],
                "feature_reply": node.get("feature_reply", []), "reads": [], "read_queue": [],
                "closed": 0, "open_error": node.get("open_error"),
            }
        self.opened: list[bytes] = []

    def enumerate(self, vendor_id, product_id):
        return [e for e in self.entries
                if (not vendor_id or e["vendor_id"] == vendor_id)
                and (not product_id or e["product_id"] == product_id)]

    def open_path(self, path):
        st = self.state[path]
        if st["open_error"]:
            raise OSError(st["open_error"])
        self.opened.append(path)
        return _Handle(st)

    def st(self, interface_number: int) -> dict:
        for e in self.entries:
            if e["interface_number"] == interface_number:
                return self.state[e["path"]]
        raise KeyError(interface_number)


PID = 0x12E0


def _headset(over: dict | None = None):
    over = over or {}
    nodes = [
        {"product_id": PID, "interface_number": 0, "usage_page": 0x000C, "descriptor": CONSUMER_INPUT},
        {"product_id": PID, "interface_number": 3, "usage_page": 0xFFC0, "descriptor": VENDOR_UNNUMBERED},
        {"product_id": PID, "interface_number": 4, "usage_page": 0xFF00, "descriptor": VENDOR_NUMBERED},
    ]
    for node in nodes:
        node.update(over.get(node["interface_number"], {}))
    return _Backend(nodes)


@pytest.fixture
def be():
    return _headset()


@pytest.fixture
def dev(be):
    d = core.find(idVendor=0x1038, idProduct=PID, backend=be)
    assert d is not None
    return d


CLASS_OUT = util.build_request_type(util.CTRL_OUT, util.CTRL_TYPE_CLASS, util.CTRL_RECIPIENT_INTERFACE)
CLASS_IN = util.build_request_type(util.CTRL_IN, util.CTRL_TYPE_CLASS, util.CTRL_RECIPIENT_INTERFACE)


# ── discovery and descriptors ────────────────────────────────────────────────

class TestFind:
    def test_groups_the_interfaces_of_one_product_into_one_device(self, be):
        d = core.find(idVendor=0x1038, idProduct=PID, backend=be)
        assert (d.idVendor, d.idProduct) == (0x1038, PID)
        assert sorted(i.bInterfaceNumber for i in d.get_active_configuration()) == [0, 3, 4]

    def test_no_match_is_none(self, be):
        assert core.find(idVendor=0x1038, idProduct=0xBEEF, backend=be) is None

    def test_find_all_is_an_iterator_of_devices(self):
        be2 = _Backend([
            {"product_id": 0x12E0, "interface_number": 4, "usage_page": 0xFF00, "descriptor": VENDOR_UNNUMBERED},
            {"product_id": 0x2202, "interface_number": 3, "usage_page": 0xFF00, "descriptor": VENDOR_UNNUMBERED},
        ])
        devices = list(core.find(find_all=True, idVendor=0x1038, backend=be2))
        assert sorted(d.idProduct for d in devices) == [0x12E0, 0x2202]

    def test_identity_attributes_core_py_reads(self, dev):
        # core._device_identity() reads these; None must be tolerated, not raise.
        assert dev.bus is None and dev.port_numbers is None
        assert dev.product == "Fake Headset"


class TestConfiguration:
    def test_interfaces_are_hid_class_and_endpoints_are_methods_like_pyusb(self, dev):
        cfg = dev.get_active_configuration()
        intf = next(i for i in cfg if i.bInterfaceNumber == 3)
        assert intf.bInterfaceClass == 3 and intf.bAlternateSetting == 0
        eps = intf.endpoints()
        assert {util.endpoint_direction(e.bEndpointAddress) for e in eps} == {util.ENDPOINT_IN, util.ENDPOINT_OUT}
        assert [e.wMaxPacketSize for e in eps] == [64, 64]
        assert cfg[(3, 0)] is intf

    def test_an_interface_without_output_items_has_no_out_endpoint(self, dev):
        intf = next(i for i in dev.get_active_configuration() if i.bInterfaceNumber == 0)
        assert [util.endpoint_direction(e.bEndpointAddress) for e in intf] == [util.ENDPOINT_IN]
        assert intf.endpoints()[0].wMaxPacketSize == 2

    def test_core_py_endpoint_lookup_pattern_works(self, dev):
        # Mirrors CoreEngine.guess_interface_endpoint().
        interface = next(c for c in dev.get_active_configuration()
                         if c.bInterfaceNumber == 4 and c.bAlternateSetting == 0)
        found = next((e.bEndpointAddress, e.wMaxPacketSize) for e in interface.endpoints()
                     if util.endpoint_direction(e.bEndpointAddress) == util.ENDPOINT_IN)
        assert found == (0x85, 64)

    def test_unreadable_descriptor_falls_back_to_64_byte_endpoints(self):
        be2 = _headset({3: {"descriptor": None}})
        d = core.find(idVendor=0x1038, idProduct=PID, backend=be2)
        intf = next(i for i in d.get_active_configuration() if i.bInterfaceNumber == 3)
        assert [e.wMaxPacketSize for e in intf] == [64, 64]


class TestPyusbIterationContract:
    """The walking patterns CoreEngine / cli_tools / charging_case really use.

    Regression: the shim once lacked ``Device.__iter__``. The probe still passed
    (it never iterates), but CoreEngine._find_hid_device() swallowed the TypeError
    and reported "No supported device connected" for a headset that was right there.
    """

    def test_core_find_hid_device_walk(self, be):
        # verbatim from CoreEngine._find_hid_device
        USB_CLASS_HID = 3
        device = core.find(idVendor=0x1038, idProduct=PID, backend=be)
        devices = [device] if isinstance(device, core.Device) else list(device)
        found = None
        for dev in devices:
            for cfg in dev:
                for intf in cfg:
                    if intf.bInterfaceClass == USB_CLASS_HID:
                        found = dev
                        break
        assert found is device

    def test_cli_tools_walk_down_to_endpoints(self, dev):
        seen = [(i.bInterfaceNumber, e.bEndpointAddress)
                for cfg in dev for i in cfg for e in i]
        assert (3, 0x84) in seen and (3, 0x04) in seen

    def test_charging_case_find_descriptor_for_the_in_endpoint(self, dev):
        intf = dev.get_active_configuration()[(3, 0)]
        ep = util.find_descriptor(intf, custom_match=lambda e: util.endpoint_direction(
            e.bEndpointAddress) == util.ENDPOINT_IN)
        assert ep.bEndpointAddress == 0x84
        assert util.find_descriptor(intf, bEndpointAddress=0x99) is None

    def test_langids_only_when_strings_exist(self, dev):
        assert dev.langids == (0x0409,)


class TestUsagePageHook:
    def test_shim_reports_the_usage_page_hidapi_gave(self, dev):
        assert dev.hid_usage_page(3) == 0xFFC0
        assert dev.hid_usage_page(4) == 0xFF00
        assert dev.hid_usage_page(9) is None

    def test_core_asks_the_shim_and_ignores_sysfs(self, dev):
        from types import SimpleNamespace
        from arctis_sound_manager.core import CoreEngine
        fake = SimpleNamespace(usb_device=dev)
        assert CoreEngine._hid_usage_page(fake, 3, sys_root=Path("/nonexistent")) == 0xFFC0

    def test_a_magicmock_device_is_not_mistaken_for_the_shim(self):
        from unittest.mock import MagicMock
        from arctis_sound_manager.core import CoreEngine
        dev = MagicMock()
        dev.bus, dev.port_numbers = None, None
        fake = SimpleNamespace(usb_device=dev)
        # falls through to the sysfs path (no bus -> None), never returns a MagicMock
        assert CoreEngine._hid_usage_page(fake, 3, sys_root=Path("/nonexistent")) is None


# ── SET_REPORT / GET_REPORT / write ──────────────────────────────────────────

class TestSetReport:
    def test_unnumbered_output_gets_a_leading_zero_and_is_padded(self, dev, be):
        n = dev.ctrl_transfer(CLASS_OUT, 0x09, (0x02 << 8) | 0, 3, [0x06, 0x20])
        assert n == 2
        (buf,) = be.st(3)["writes"]
        assert buf[:3] == [0x00, 0x06, 0x20] and len(buf) == 65 and not any(buf[3:])

    def test_numbered_output_keeps_the_id_and_does_not_double_it(self, dev, be):
        dev.ctrl_transfer(CLASS_OUT, 0x09, (0x02 << 8) | 6, 4, [6, 0x8D, 0x01])
        (buf,) = be.st(4)["writes"]
        assert buf[:3] == [6, 0x8D, 0x01] and len(buf) == 64

    def test_numbered_output_without_the_id_prepends_it(self, dev, be):
        dev.ctrl_transfer(CLASS_OUT, 0x09, (0x02 << 8) | 6, 4, [0x8D, 0x01])
        assert be.st(4)["writes"][0][:3] == [6, 0x8D, 0x01]

    def test_feature_report_goes_through_send_feature_report(self, dev, be):
        be2 = _headset({3: {"descriptor": VENDOR_FEATURE_ONLY}})
        d = core.find(idVendor=0x1038, idProduct=PID, backend=be2)
        d.ctrl_transfer(CLASS_OUT, 0x09, (0x03 << 8) | 0, 3, [0x05, 0x01])
        (buf,) = be2.st(3)["features"]
        assert buf[:3] == [0x00, 0x05, 0x01] and len(buf) == 34
        assert be2.st(3)["writes"] == []

    def test_write_on_an_out_endpoint_maps_to_its_interface(self, dev, be):
        dev.write(util.ENDPOINT_OUT | (3 + 1), [0x06, 0x10])
        assert be.st(3)["writes"][0][:3] == [0x00, 0x06, 0x10]

    def test_reader_and_writer_use_separate_handles(self, dev, be):
        dev.ctrl_transfer(CLASS_OUT, 0x09, (0x02 << 8), 3, [1])
        with pytest.raises(core.USBTimeoutError):
            dev.read(0x80 | 4, 64, 10)  # nothing queued: times out, but opens the reader
        path = next(e["path"] for e in be.entries if e["interface_number"] == 3)
        assert be.opened.count(path) == 2  # one for the descriptor/reader, one for the writer

    def test_failed_write_raises_eio(self, be):
        be2 = _headset({3: {}})
        be2.st(3)["write_result"] = -1
        d = core.find(idVendor=0x1038, idProduct=PID, backend=be2)
        with pytest.raises(core.USBError) as ei:
            d.ctrl_transfer(CLASS_OUT, 0x09, (0x02 << 8), 3, [1])
        assert ei.value.errno == core.EIO

    def test_missing_interface_is_enoent_like_libusb(self, dev):
        with pytest.raises(core.USBError) as ei:
            dev.ctrl_transfer(CLASS_OUT, 0x09, (0x02 << 8), 9, [1])
        assert ei.value.errno == core.ENOENT  # the code core.py retargets on

    def test_open_failure_is_ebusy_never_eacces(self):
        be2 = _headset({3: {"open_error": "open failed"}})
        d = core.find(idVendor=0x1038, idProduct=PID, backend=be2)
        with pytest.raises(core.USBError) as ei:
            d.ctrl_transfer(CLASS_OUT, 0x09, (0x02 << 8), 3, [1])
        assert ei.value.errno == core.EBUSY  # EACCES would pop ASM's Linux udev dialog

    def test_non_class_requests_are_refused(self, dev):
        with pytest.raises(core.USBError):
            dev.ctrl_transfer(util.CTRL_IN, 0x06, 0x0100, 0, 18)  # standard GET_DESCRIPTOR


class TestGetReport:
    def test_unnumbered_feature_strips_hidapis_leading_id_byte(self):
        be2 = _headset({3: {"descriptor": VENDOR_FEATURE_ONLY, "feature_reply": [0x00, 0xAA, 0xBB, 0xCC]}})
        d = core.find(idVendor=0x1038, idProduct=PID, backend=be2)
        out = d.ctrl_transfer(CLASS_IN, 0x01, (0x03 << 8) | 0, 3, 3, timeout=1000)
        assert out == [0xAA, 0xBB, 0xCC]
        assert be2.st(3)["feature_asks"] == [(0, 4)]  # one extra byte for the id slot

    def test_numbered_feature_keeps_the_id(self):
        desc = bytes.fromhex("0600FF 0901 A101 8507 7508 9503 0901 B102 C0".replace(" ", ""))
        be2 = _headset({3: {"descriptor": desc, "feature_reply": [7, 1, 2, 3]}})
        d = core.find(idVendor=0x1038, idProduct=PID, backend=be2)
        out = d.ctrl_transfer(CLASS_IN, 0x01, (0x03 << 8) | 7, 3, 4)
        assert out == [7, 1, 2, 3]
        assert be2.st(3)["feature_asks"] == [(7, 4)]


# ── read ─────────────────────────────────────────────────────────────────────

class TestRead:
    def test_returns_the_frame_as_a_list(self, dev, be):
        be.st(3)["read_queue"].append([0x07, 0x25, 0x40])
        assert dev.read(0x80 | 4, 64, 1000) == [0x07, 0x25, 0x40]

    def test_timeout_is_usbtimeouterror_110(self, dev):
        with pytest.raises(core.USBTimeoutError) as ei:
            dev.read(0x80 | 4, 64, 5)
        assert ei.value.errno == 110 and isinstance(ei.value, core.USBError)

    def test_never_asks_hidapi_for_a_zero_timeout(self, dev, be):
        # hid.read(n, 0) means "block forever" on some builds: the listen task would hang.
        with pytest.raises(core.USBTimeoutError):
            dev.read(0x80 | 4, 64, 0)
        assert be.st(3)["reads"][-1][1] >= 1

    def test_device_gone_maps_to_enodev(self, dev, be):
        be.st(3)["read_error"] = "The device is not connected."
        with pytest.raises(core.USBError) as ei:
            dev.read(0x80 | 4, 64, 5)
        assert ei.value.errno == core.ENODEV

    def test_other_read_failure_maps_to_eio(self, dev, be):
        be.st(3)["read_error"] = "read error"
        with pytest.raises(core.USBError) as ei:
            dev.read(0x80 | 4, 64, 5)
        assert ei.value.errno == core.EIO

    def test_buffer_is_never_smaller_than_a_standard_report(self, dev, be):
        be.st(3)["read_queue"].append([1])
        dev.read(0x80 | 4, 8, 50)
        assert be.st(3)["reads"][0][0] >= 64

    def test_one_endpoint_over_two_hid_nodes_polls_both(self):
        # Same interface exposes a vendor node and a consumer node; the frame arrives on the second.
        be2 = _Backend([
            {"product_id": PID, "interface_number": 2, "usage_page": 0xFF00, "descriptor": VENDOR_UNNUMBERED},
            {"product_id": PID, "interface_number": 2, "usage_page": 0x000C, "descriptor": CONSUMER_INPUT},
        ])
        d = core.find(idVendor=0x1038, idProduct=PID, backend=be2)
        be2.state[be2.entries[1]["path"]]["read_queue"].append([0x01, 0x02])
        assert d.read(0x80 | 3, 64, 200) == [0x01, 0x02]


# ── resources and no-ops ─────────────────────────────────────────────────────

class TestLifecycle:
    def test_claim_checks_the_interface_exists(self, dev):
        util.claim_interface(dev, 3)
        with pytest.raises(core.USBError) as ei:
            util.claim_interface(dev, 9)
        assert ei.value.errno == core.ENOENT

    def test_release_and_dispose_close_handles(self, dev, be):
        dev.ctrl_transfer(CLASS_OUT, 0x09, (0x02 << 8), 3, [1])
        util.release_interface(dev, 3)
        assert be.st(3)["closed"] >= 1
        dev.ctrl_transfer(CLASS_OUT, 0x09, (0x02 << 8), 4, [6, 1])
        util.dispose_resources(dev)
        assert be.st(4)["closed"] >= 1

    def test_kernel_driver_calls_are_harmless_noops(self, dev):
        assert dev.is_kernel_driver_active(3) is False
        dev.detach_kernel_driver(3)
        dev.attach_kernel_driver(3)


# ── registration as ``usb`` ──────────────────────────────────────────────────

class TestInstall:
    def test_registers_the_three_module_names(self, monkeypatch):
        for name in ("usb", "usb.core", "usb.util"):
            monkeypatch.delitem(sys.modules, name, raising=False)
        assert install_usb_shim() is True
        import usb
        import usb.core
        import usb.util
        assert usb.core.USBError is core.USBError
        assert usb.util.ENDPOINT_IN == 0x80
        from usb.core import Device  # noqa: F401  (the form core.py uses)

    def test_a_real_pyusb_that_is_already_loaded_is_left_alone(self, monkeypatch):
        real = types.ModuleType("usb")
        monkeypatch.setitem(sys.modules, "usb", real)
        assert install_usb_shim() is False
        assert sys.modules["usb"] is real

    def test_force_overrides(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "usb", types.ModuleType("usb"))
        monkeypatch.setitem(sys.modules, "usb.core", types.ModuleType("usb.core"))
        monkeypatch.setitem(sys.modules, "usb.util", types.ModuleType("usb.util"))
        assert install_usb_shim(force=True) is True
        assert sys.modules["usb.core"] is core


# ── portable locks and paths ─────────────────────────────────────────────────

FILE_LOCK = Path(__file__).resolve().parents[1] / "src" / "arctis_sound_manager" / "file_lock.py"


def _load_file_lock_as(platform: str, monkeypatch, fake_msvcrt=None):
    """A private copy of file_lock evaluated as ``platform`` (never touches the real module)."""
    spec = importlib.util.spec_from_file_location(f"_file_lock_{platform}", FILE_LOCK)
    mod = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", platform)
        if fake_msvcrt is not None:
            m.setitem(sys.modules, "msvcrt", fake_msvcrt)
        spec.loader.exec_module(mod)
    return mod


class TestFileLock:
    def test_posix_second_holder_is_refused_and_release_works(self, tmp_path):
        from arctis_sound_manager import file_lock as fl
        a = os.open(tmp_path / "x.lock", os.O_CREAT | os.O_RDWR)
        b = os.open(tmp_path / "x.lock", os.O_CREAT | os.O_RDWR)
        try:
            fl.lock_nb(a)
            with pytest.raises(OSError):
                fl.lock_nb(b)
            fl.unlock(a)
            fl.lock_nb(b)  # free again
        finally:
            os.close(a)
            os.close(b)

    def test_windows_branch_uses_nonblocking_msvcrt_locking(self, monkeypatch, tmp_path):
        calls = []
        fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0,
                                     locking=lambda fd, mode, n: calls.append((mode, n)))
        mod = _load_file_lock_as("win32", monkeypatch, fake)
        assert mod.IS_WINDOWS
        fd = os.open(tmp_path / "w.lock", os.O_CREAT | os.O_RDWR)
        try:
            mod.lock_nb(fd)
            mod.unlock(fd)
        finally:
            os.close(fd)
        assert calls == [(2, 1), (0, 1)]

    def test_windows_branch_propagates_oserror_when_held(self, monkeypatch, tmp_path):
        def locking(fd, mode, n):
            raise OSError(13, "Permission denied")
        mod = _load_file_lock_as("win32", monkeypatch,
                                 types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking))
        fd = os.open(tmp_path / "w.lock", os.O_CREAT | os.O_RDWR)
        try:
            with pytest.raises(OSError):  # daemon.py / service_control.py catch exactly this
                mod.lock_nb(fd)
        finally:
            os.close(fd)

    def test_runtime_dir_posix_matches_the_historic_locations(self, monkeypatch):
        from arctis_sound_manager import file_lock as fl
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert fl.runtime_dir() == Path("/run/user/1000")
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        assert fl.runtime_dir() == Path(f"/tmp/arctis-{os.getuid()}")

    def test_runtime_dir_on_windows_lives_under_localappdata(self, monkeypatch, tmp_path):
        mod = _load_file_lock_as("win32", monkeypatch, types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=None))
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(mod, "IS_WINDOWS", True)
        assert mod.runtime_dir() == tmp_path / "arctis_manager" / "run"


class TestConfigHome:
    def test_posix_is_unchanged(self, monkeypatch, tmp_path):
        from arctis_sound_manager import constants
        monkeypatch.setattr(constants, "IS_WINDOWS", False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert constants._config_home() == tmp_path

    def test_windows_uses_dot_config_in_the_profile(self, monkeypatch, tmp_path):
        """Same folder as the ~60 hand-written Path.home()/.config/arctis_manager paths."""
        from arctis_sound_manager import constants
        monkeypatch.setattr(constants, "IS_WINDOWS", True)
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))        # no longer consulted
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))   # not on Windows either
        assert constants._config_home() == Path.home() / ".config"

    def test_legacy_appdata_settings_are_copied_once(self, monkeypatch, tmp_path):
        from arctis_sound_manager import constants
        monkeypatch.setattr(constants, "IS_WINDOWS", True)
        roaming, root = tmp_path / "Roaming", tmp_path / "cfg"
        (roaming / "arctis_manager" / "settings").mkdir(parents=True)
        (roaming / "arctis_manager" / "settings" / "general_settings.yaml").write_text("theme: x\n")
        monkeypatch.setenv("APPDATA", str(roaming))
        assert constants._migrate_legacy_windows_config(root) is True
        assert (root / "arctis_manager" / "settings" / "general_settings.yaml").read_text() == "theme: x\n"
        assert (roaming / "arctis_manager").is_dir()                       # copied, never moved
        # The new folder now wins: a second start must not overwrite what the user changed since.
        (root / "arctis_manager" / "settings" / "general_settings.yaml").write_text("theme: y\n")
        assert constants._migrate_legacy_windows_config(root) is False
        assert (root / "arctis_manager" / "settings" / "general_settings.yaml").read_text() == "theme: y\n"

    def test_migration_does_nothing_off_windows_or_without_legacy_data(self, monkeypatch, tmp_path):
        from arctis_sound_manager import constants
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        (tmp_path / "Roaming" / "arctis_manager").mkdir(parents=True)
        monkeypatch.setattr(constants, "IS_WINDOWS", False)
        assert constants._migrate_legacy_windows_config(tmp_path / "cfg") is False
        monkeypatch.setattr(constants, "IS_WINDOWS", True)
        monkeypatch.delenv("APPDATA")
        assert constants._migrate_legacy_windows_config(tmp_path / "cfg") is False
        monkeypatch.setenv("APPDATA", str(tmp_path / "nowhere"))
        assert constants._migrate_legacy_windows_config(tmp_path / "cfg") is False
        assert not (tmp_path / "cfg").exists()

    def test_invoking_user_lookup_survives_a_platform_without_geteuid(self, monkeypatch):
        from arctis_sound_manager import constants
        monkeypatch.delattr(os, "geteuid", raising=False)
        assert constants._invoking_user_config_home() is None
