"""What the program costs while it idles: the HID scan, the hot-plug poll, the read loop, the retry for a missing headset.

A user watched Task Manager for three hours and saw the program grow from 600 MB to 3 GB and spike in CPU. The
causes found were all in the places that run forever: a device scan that walked every HID interface of the PC ninety
times per pass (and again every few seconds, for as long as no headset was found), a read loop that polled the
Nova Pro's two HID nodes 500 times a second, and a log line per scan. These tests pin the cheap behaviour down.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from arctis_sound_manager.win.pyusb_shim import core


# ── the enumeration cache ────────────────────────────────────────────────────

class _FakeHidModule:
    def __init__(self, entries):
        self.entries = entries
        self.calls: list[tuple[int, int]] = []

    def enumerate(self, vendor_id=0, product_id=0):
        self.calls.append((vendor_id, product_id))
        return [dict(e) for e in self.entries
                if (not vendor_id or e["vendor_id"] == vendor_id) and (not product_id or e["product_id"] == product_id)]

    def device(self):                                    # pragma: no cover - not opened here
        raise AssertionError("no handle is opened by a scan")


def _entries():
    return [
        {"path": b"a", "vendor_id": 0x1038, "product_id": 0x12E5, "interface_number": 4, "usage_page": 0xFFC0},
        {"path": b"b", "vendor_id": 0x1038, "product_id": 0x12E5, "interface_number": 3, "usage_page": 0x000C},
        {"path": b"c", "vendor_id": 0x046D, "product_id": 0xC52B, "interface_number": 0, "usage_page": 1},
    ]


def _backend():
    backend = core._HidApiBackend()
    backend._hid = _FakeHidModule(_entries())
    return backend


def test_ninety_questions_cost_one_enumeration(monkeypatch):
    monkeypatch.setattr(core.time, "monotonic", lambda: 100.0)
    backend = _backend()
    for pid in range(0x1200, 0x1200 + 90):                # one question per product id, as a scan over the profiles asks
        backend.enumerate(0x1038, pid)
    assert backend.enumerations == 1
    assert len(backend._hid.calls) == 1


def test_answers_are_filtered_by_product_id_and_vendor(monkeypatch):
    monkeypatch.setattr(core.time, "monotonic", lambda: 5.0)
    backend = _backend()
    assert {e["interface_number"] for e in backend.enumerate(0x1038, 0x12E5)} == {3, 4}
    assert backend.enumerate(0x1038, 0x9999) == []
    assert {e["path"] for e in backend.enumerate(0x1038, 0)} == {b"a", b"b"}
    assert {e["path"] for e in backend.enumerate(0, 0)} == {b"a", b"b", b"c"}   # another vendor key: its own scan


def test_the_answer_is_renewed_after_the_ttl(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: now[0])
    backend = _backend()
    backend.enumerate(0x1038, 0)
    now[0] += core._HidApiBackend.ENUMERATE_TTL_S - 0.1
    backend.enumerate(0x1038, 0)
    assert backend.enumerations == 1
    now[0] += 0.2
    backend._hid.entries = backend._hid.entries[:1]                 # the second interface went away
    assert {e["path"] for e in backend.enumerate(0x1038, 0)} == {b"a"}
    assert backend.enumerations == 2


def test_invalidate_forgets_the_answers(monkeypatch):
    monkeypatch.setattr(core.time, "monotonic", lambda: 1.0)
    backend = _backend()
    backend.enumerate(0x1038, 0)
    backend.invalidate()
    backend.enumerate(0x1038, 0)
    assert backend.enumerations == 2


def test_a_clock_that_went_backwards_does_not_freeze_the_answer(monkeypatch):
    now = [50.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: now[0])
    backend = _backend()
    backend.enumerate(0x1038, 0)
    now[0] = 1.0
    backend.enumerate(0x1038, 0)
    assert backend.enumerations == 2


def test_threads_asking_together_share_one_enumeration():
    backend = core._HidApiBackend()
    gate = threading.Event()
    hid = _FakeHidModule(_entries())
    original = hid.enumerate

    def slow(vendor_id=0, product_id=0):
        gate.wait(2.0)
        return original(vendor_id, product_id)

    hid.enumerate = slow
    backend._hid = hid
    results = []
    threads = [threading.Thread(target=lambda: results.append(len(backend.enumerate(0x1038, 0x12E5)))) for _ in range(6)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(5.0)
    assert results == [2] * 6
    assert backend.enumerations == 1


def test_find_uses_the_cache_for_every_profile(monkeypatch):
    monkeypatch.setattr(core.time, "monotonic", lambda: 3.0)
    backend = _backend()
    previous = core.get_backend()
    core.set_backend(backend)
    try:
        assert core.find(idVendor=0x1038, idProduct=0x1111) is None
        assert core.find(idVendor=0x1038, idProduct=0x2222) is None
        found = core.find(idVendor=0x1038, idProduct=0x12E5)
        assert found is not None and found.idProduct == 0x12E5
        assert [d.idProduct for d in core.find(find_all=True, idVendor=0x1038)] == [0x12E5]
    finally:
        core.set_backend(previous)
    assert backend.enumerations == 1


# ── the read loop ────────────────────────────────────────────────────────────

class _Handle:
    def __init__(self, payload=None):
        self.calls: list[tuple] = []
        self.nonblocking: list[int] = []
        self.payload = payload

    def set_nonblocking(self, value):
        self.nonblocking.append(value)

    def read(self, n, timeout_ms=0):
        self.calls.append((n, timeout_ms))
        if self.payload is not None and len(self.calls) >= 3:
            return list(self.payload)
        time.sleep(timeout_ms / 1000.0)                   # a real blocking read waits for data
        return []


def _two_node_device(first: _Handle, second: _Handle):
    entries = [
        {"path": b"p1", "interface_number": 4, "usage_page": 0xFFC0, "usage": 1, "vendor_id": 0x1038, "product_id": 1},
        {"path": b"p2", "interface_number": 4, "usage_page": 0xFF00, "usage": 1, "vendor_id": 0x1038, "product_id": 1},
    ]
    device = core.Device(0x1038, 1, entries, MagicMock())
    device._readers = lambda iface: [first, second]       # type: ignore[method-assign]
    return device


def test_reading_two_nodes_blocks_instead_of_polling():
    first, second = _Handle(), _Handle()
    device = _two_node_device(first, second)
    with pytest.raises(core.USBTimeoutError):
        device.read(0x80 | 5, 64, timeout=120)
    # a busy poll would have made hundreds of calls in 120 ms; short blocking reads make a handful
    total = len(first.calls) + len(second.calls)
    assert 2 <= total <= 40, total
    assert all(1 <= ms <= core._READ_SLICE_MS for _, ms in first.calls + second.calls)    # every call really waits
    assert set(first.nonblocking) == {0} and set(second.nonblocking) == {0}              # never left non-blocking


def test_data_on_the_second_node_is_returned():
    first, second = _Handle(), _Handle(payload=[7, 1, 2])
    device = _two_node_device(first, second)
    assert device.read(0x80 | 5, 64, timeout=500) == [7, 1, 2]


# ── the retry for a missing headset ──────────────────────────────────────────

def _engine():
    from arctis_sound_manager.core import CoreEngine
    engine = CoreEngine.__new__(CoreEngine)
    engine.logger = MagicMock()
    engine._device_ready = False
    engine._rescan_in_flight = False
    engine._rescan_misses = 0
    return engine


def test_the_retry_is_quick_at_first_and_relaxes_later():
    from arctis_sound_manager import core as core_mod
    engine = _engine()
    assert engine._rescan_interval() == core_mod.RESCAN_INTERVAL_S
    engine._rescan_misses = core_mod.RESCAN_FAST_COUNT - 1
    assert engine._rescan_interval() == core_mod.RESCAN_INTERVAL_S
    engine._rescan_misses = core_mod.RESCAN_FAST_COUNT
    assert engine._rescan_interval() == core_mod.RESCAN_SLOW_INTERVAL_S
    assert core_mod.RESCAN_SLOW_INTERVAL_S >= 5 * core_mod.RESCAN_INTERVAL_S


def test_misses_are_counted_and_a_find_resets_them():
    engine = _engine()
    with patch.object(engine, "configure_virtual_sinks"):
        for _ in range(5):
            engine._rescan_in_flight = True
            engine._rescan_for_device()
        assert engine._rescan_misses == 5
        engine._device_ready = True
        engine._rescan_in_flight = True
        engine._rescan_for_device()
        assert engine._rescan_misses == 0


def test_a_failing_scan_counts_as_a_miss_and_does_not_raise():
    engine = _engine()
    with patch.object(engine, "configure_virtual_sinks", side_effect=RuntimeError("boom")):
        engine._rescan_in_flight = True
        engine._rescan_for_device()
    assert engine._rescan_misses == 1 and engine._rescan_in_flight is False


def test_a_bare_engine_without_the_counter_still_works():
    from arctis_sound_manager.core import CoreEngine
    engine = CoreEngine.__new__(CoreEngine)
    engine.logger = MagicMock()
    engine._device_ready = False
    engine._rescan_in_flight = True
    with patch.object(engine, "configure_virtual_sinks"):
        engine._rescan_for_device()
    assert engine._rescan_misses == 1
    assert engine._rescan_interval() > 0


def test_the_missing_preferred_device_is_logged_once_per_absence():
    from arctis_sound_manager.core import CoreEngine
    engine = CoreEngine.__new__(CoreEngine)
    engine.logger = MagicMock()
    engine._detect_lock = threading.Lock()
    engine._device_configured_event = threading.Event()
    engine._device_ready = False
    engine._logged_no_device = False
    engine._logged_preferred_missing = False
    engine.device_configurations = []
    engine.general_settings = MagicMock(preferred_device="1038:12e5:abc")
    engine.usb_device = None
    engine.device_config = None
    with patch.object(engine, "_find_preferred_device", return_value=(None, None)), \
            patch.object(engine, "_setup_generic_device", return_value=False):
        for _ in range(50):
            engine.configure_virtual_sinks(only_if_unconfigured=True)
    said = [c for c in engine.logger.info.call_args_list if "Preferred device" in str(c)]
    assert len(said) == 1
    assert len([c for c in engine.logger.warning.call_args_list if "No supported device" in str(c)]) == 1


# ── the noise cancelling card on Windows ─────────────────────────────────────

@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _card(monkeypatch, *, windows: bool, library, engine="rnnoise", enabled=False):
    from arctis_sound_manager.gui import sonar_page
    from arctis_sound_manager.win.audio import rnnoise
    monkeypatch.setattr(sonar_page, "IS_WINDOWS", windows)
    monkeypatch.setattr(rnnoise, "find_library", lambda: library)
    sonar_page._NoiseCancelingCard._rnnoise_available = None
    sonar_page._NoiseCancelingCard._deepfilter_available = None
    state = {"noiseCanceling": {"enabled": enabled, "value": 0.9, "engine": engine}}
    return sonar_page._NoiseCancelingCard(state), state


def test_windows_card_with_the_library_can_be_switched_on(qapp, monkeypatch, tmp_path):
    card, state = _card(monkeypatch, windows=True, library=tmp_path / "rnnoise.dll")
    assert card._toggle.isEnabled()
    assert [card._engine_combo.itemData(i) for i in range(card._engine_combo.count())] == ["rnnoise"]
    assert card._engine_combo.isHidden()                       # nothing to choose from
    assert card._status_lbl.text() == ""
    changed = []
    card.state_changed.connect(lambda: changed.append(1))
    card._toggle.setChecked(True)
    assert state["noiseCanceling"]["enabled"] is True and changed
    assert card._slider.isEnabled()


def test_windows_card_never_offers_deepfilternet_or_asks_to_install_it(qapp, monkeypatch, tmp_path):
    # a state file written on Linux (or by an older build) names DeepFilterNet
    card, state = _card(monkeypatch, windows=True, library=tmp_path / "rnnoise.dll", engine="deepfilternet")
    assert state["noiseCanceling"]["engine"] == "rnnoise"
    assert card._engine == "rnnoise"
    with patch("PySide6.QtWidgets.QMessageBox.exec") as dialog, patch("PySide6.QtWidgets.QMessageBox.warning") as warning:
        card._toggle.setChecked(True)
    assert not dialog.called and not warning.called
    assert state["noiseCanceling"]["enabled"] is True


def test_windows_card_without_the_library_says_what_is_missing(qapp, monkeypatch):
    card, state = _card(monkeypatch, windows=True, library=None)
    assert not card._toggle.isEnabled()
    assert "rnnoise.dll" in card._status_lbl.text()
    assert state["noiseCanceling"]["enabled"] is False


def test_linux_card_keeps_both_engines(qapp, monkeypatch):
    card, _ = _card(monkeypatch, windows=False, library=None)
    assert [card._engine_combo.itemData(i) for i in range(card._engine_combo.count())] == ["rnnoise", "deepfilternet"]


def test_waveform_timer_runs_only_while_shown(qapp):
    from PySide6.QtWidgets import QWidget
    from arctis_sound_manager.gui.sonar_page import _WaveformWidget
    host = QWidget()
    wave = _WaveformWidget(host)
    assert not wave._timer.isActive()                          # built but never shown: no ticking
    host.show()
    qapp.processEvents()
    assert wave._timer.isActive()
    host.hide()
    qapp.processEvents()
    assert not wave._timer.isActive()
