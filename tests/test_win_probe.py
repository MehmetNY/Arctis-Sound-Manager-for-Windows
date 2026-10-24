"""The Windows probe, end to end, against a fake hidapi.

Real YAML profile (Arctis Nova Pro Wireless) + the pyusb shim + the probe's
three stages. A fake "headset" answers a status request with a frame, so this
proves the probe's framing/endpoint selection against the real profile loader.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

import arctis_sound_manager.config  # noqa: F401  (preloaded: main() imports it under a patched platform)
from arctis_sound_manager.scripts import win_probe
from arctis_sound_manager.win.pyusb_shim import core

VENDOR_64 = bytes.fromhex("06C0FF 0901 A101 1500 26FF00 7508 9540 0901 8102 0901 9102 C0".replace(" ", ""))
PID = 0x12E0  # Arctis Nova Pro Wireless: command + listen on interface 4
REPLY = [0x07, 0x25, 0x1C] + [0] * 61


class _FakeHeadsetBackend:
    def __init__(self):
        self.path = b"\\\\?\\hid#vid_1038&pid_12e0&mi_04#7&abc&0&0000"
        self.entry = {"path": self.path, "vendor_id": 0x1038, "product_id": PID,
                      "interface_number": 4, "usage_page": 0xFFC0, "usage": 1,
                      "product_string": "Arctis Nova Pro Wireless"}
        self.writes: list[list[int]] = []
        self.pending: list[list[int]] = []

    def enumerate(self, vid, pid):
        return [self.entry] if (not vid or vid == 0x1038) and (not pid or pid == PID) else []

    def open_path(self, path):
        outer = self

        class H:
            def get_report_descriptor(self): return list(VENDOR_64)
            def write(self, buf):
                outer.writes.append(list(buf))
                outer.pending.append(REPLY)  # the headset answers any frame
                return len(buf)
            def send_feature_report(self, buf): return len(buf)
            def read(self, n, timeout_ms=0):
                return outer.pending.pop(0) if outer.pending else []
            def set_nonblocking(self, v): pass
            def close(self): pass
            def error(self): return ""
        return H()


@pytest.fixture
def backend():
    be = _FakeHeadsetBackend()
    previous = core.get_backend()
    core.set_backend(be)
    yield be
    core.set_backend(previous)


def test_status_poll_uses_the_profiles_own_frame_and_hears_the_reply(backend):
    q = win_probe.stage_query(PID, listen_seconds=0.5)
    assert q.get("error") is None and q["request_sent"] is True
    assert q["profile"].startswith("SteelSeries Arctis Nova Pro Wireless")
    assert q["command_interface"] == 4 and q["listen_interfaces"] == [4]
    # 0x06b0 padded to the profile's 64 bytes, behind hidapi's leading report-id 0.
    (sent,) = backend.writes
    assert sent[:3] == [0x00, 0x06, 0xB0] and len(sent) == 65
    assert q["got_reply"] and q["frames"][0].startswith("07 25 1c")


def test_unknown_product_is_reported_not_raised(backend):
    assert "no device profile" in win_probe.stage_query(0xDEAD, 0.1)["error"]


def test_a_profile_interface_that_hidapi_does_not_have_is_called_out(backend):
    backend.entry["interface_number"] = 7  # profile says 4
    q = win_probe.stage_query(PID, 0.1)
    assert "do not match" in q["error"]


def test_refuses_to_run_off_windows(capsys):
    assert win_probe.main([]) == 2
    assert "Windows" in capsys.readouterr().out


def test_main_runs_all_three_stages_and_writes_the_report(backend, monkeypatch, tmp_path, capsys):
    hid = types.ModuleType("hid")
    hid.__version__ = "fake"
    hid.enumerate = backend.enumerate

    class Dev:
        def open_path(self, path): self.h = backend.open_path(path)
        def get_report_descriptor(self): return self.h.get_report_descriptor()
        def close(self): pass
    hid.device = Dev
    monkeypatch.setitem(sys.modules, "hid", hid)
    monkeypatch.setattr(sys, "platform", "win32")

    out = tmp_path / "probe.json"
    assert win_probe.main(["--query", "--listen", "0.3", "--out", str(out)]) == 0

    report = json.loads(out.read_text())
    (node,) = report["nodes"]
    assert node["pid"] == "12e0" and node["interface_number"] == 4 and node["usage_page"] == "ffc0"
    assert node["open"]["opened"] and node["open"]["collections"][0]["output_len"] == 64
    assert report["queries"][0]["got_reply"] is True
    assert "_raw" not in node  # raw hidapi dict (bytes) must not leak into the JSON
    assert "ENUMERATE" in capsys.readouterr().out
