"""Run the REAL asm-daemon flow the way Windows will, in a clean interpreter.

Not a test module (no ``test_`` prefix): ``test_win_daemon_e2e.py`` launches it as a
subprocess, because it flips process-global state (``IS_WINDOWS``, the ``usb`` and
``dbus_next`` module names) that must not leak into the rest of the test session.

What is simulated: Windows' *absence* of pyudev/pulsectl/PipeWire, and the HID
device. What is real: CoreEngine, the YAML profile, the OLED manager, DbusManager,
the IPC, the shutdown path. The headset's answer is the 64-byte status frame
captured on a real Arctis Nova Pro Wireless X (PID 12e5) with ``win_probe --query``.

Prints one ``RESULT:<json>`` line.
"""
import asyncio
import importlib.abc
import json
import os
import signal
import sys
import tempfile
import time


class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"pyudev", "pulsectl", "pwd", "grp"}:
            raise ModuleNotFoundError(f"No module named {name!r} (simulated Windows)")


sys.meta_path.insert(0, _Block())

import win_posix_absence  # noqa: E402  (tests/ is sys.path[0] when run as a script)

REMOVED_POSIX_NAMES = win_posix_absence.apply()

tmp = os.environ.get("WIN_HARNESS_TMP") or tempfile.mkdtemp(prefix="asm-win-e2e-")
os.environ.update(XDG_CONFIG_HOME=f"{tmp}/cfg", XDG_RUNTIME_DIR=f"{tmp}/run", HOME=tmp,
                  QT_QPA_PLATFORM="offscreen")

import arctis_sound_manager.constants as C  # noqa: E402

C.IS_WINDOWS = True  # before pactl / platform_guard / the daemon read it

from arctis_sound_manager.win import install_dbus_shim, install_usb_shim  # noqa: E402

install_usb_shim(force=True)
install_dbus_shim(force=True)
from arctis_sound_manager.win.pyusb_shim import core as shimcore  # noqa: E402

# The real headset waits ~5 s for its transmitter to boot; the test does not need to.
import arctis_sound_manager.config as _config  # noqa: E402

_orig_load = _config.load_device_configurations


def _no_boot_delay(*args, **kwargs):
    configs = _orig_load(*args, **kwargs)
    for c in configs:
        c.init_sleep_length_ms = 0
    return configs


_config.load_device_configurations = _no_boot_delay


def _descriptor(page_lo, page_hi, rid, in_n, out_n, feat_n):
    d = [0x06, page_lo, page_hi, 0x09, 0x01, 0xA1, 0x01, 0x85, rid, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08]
    if in_n:
        d += [0x95, in_n, 0x09, 0x01, 0x81, 0x02]
    if out_n:
        d += [0x95, out_n, 0x09, 0x01, 0x91, 0x02]
    if feat_n:
        d += [0x96, feat_n & 0xFF, feat_n >> 8, 0x09, 0x01, 0xB1, 0x02]
    return bytes(d + [0xC0])


# Captured on real hardware (win_probe --query): "06 b0 01 00 01 00 08 08 08 00 02 01 05 00 08 08 ..."
STATUS_REPLY = [0x06, 0xB0, 0x01, 0x00, 0x01, 0x00, 0x08, 0x08, 0x08, 0x00, 0x02, 0x01, 0x05, 0x00, 0x08, 0x08] + [0] * 48


class Handle:
    def __init__(self, st):
        self.s = st

    def get_report_descriptor(self):
        return list(self.s["desc"])

    def write(self, buf):
        self.s["writes"].append(list(buf))
        if buf[:2] == [0x06, 0xB0]:
            self.s["replies"].append(STATUS_REPLY)
        return len(buf)

    def send_feature_report(self, buf):
        self.s["features"].append(list(buf))
        return len(buf)

    def get_feature_report(self, rid, n):
        return [rid] + [0] * (n - 1)

    def read(self, n, timeout_ms=0):
        end = time.monotonic() + (timeout_ms or 0) / 1000
        while True:
            if self.s["replies"]:
                return self.s["replies"].pop(0)
            if time.monotonic() >= end:
                return []
            time.sleep(0.005)

    def set_nonblocking(self, v):
        pass

    def close(self):
        pass

    def error(self):
        return ""


class Backend:
    PID = 0x12E5  # Arctis Nova Pro Wireless X

    def __init__(self):
        def mk(path, ifn, page, desc):
            return dict(path=path, vendor_id=0x1038, product_id=self.PID, interface_number=ifn,
                        usage_page=page, usage=1, product_string="Arctis Nova Pro Wireless", desc=desc)
        consumer = bytes([0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x01, 0x15, 0, 0x26, 0xFF, 0, 0x75, 8,
                          0x95, 1, 0x09, 1, 0x81, 2, 0xC0])
        # The three HID nodes win_probe reported on the real machine.
        self.nodes = [mk(b"a-ffc0", 4, 0xFFC0, _descriptor(0xC0, 0xFF, 6, 63, 63, 1023)),
                      mk(b"b-ff00", 4, 0xFF00, _descriptor(0x00, 0xFF, 7, 63, 63, 63)),
                      mk(b"c-000c", 3, 0x000C, consumer)]
        self.state = {n["path"]: dict(desc=n["desc"], writes=[], features=[], replies=[]) for n in self.nodes}

    def enumerate(self, vendor_id, product_id):
        return [{k: v for k, v in n.items() if k != "desc"} for n in self.nodes
                if (not vendor_id or vendor_id == 0x1038) and (not product_id or product_id == self.PID)]

    def open_path(self, path):
        return Handle(self.state[path])


backend = Backend()
shimcore.set_backend(backend)

import arctis_sound_manager.constants as K  # noqa: E402
from arctis_sound_manager.scripts import daemon  # noqa: E402
from arctis_sound_manager.win.dbus_shim.aio.message_bus import MessageBus  # noqa: E402
from arctis_sound_manager.win.dbus_shim.message import Message  # noqa: E402


async def call(member, iface=K.DBUS_STATUS_INTERFACE_NAME, path=K.DBUS_STATUS_OBJECT_PATH):
    bus = MessageBus()
    await bus.connect()
    return await bus.call(Message(destination=K.DBUS_BUS_NAME, path=path, interface=iface, member=member))


async def main():
    result = {"tmp": tmp}
    task = asyncio.create_task(daemon.main_async())

    status = None
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and not task.done():
        await asyncio.sleep(0.5)
        reply = await call("GetStatus")
        if reply.message_type.name == "METHOD_RETURN":
            parsed = json.loads(reply.body[0])
            if parsed.get("headset"):
                status = parsed
                break
    result["status"] = status

    if os.environ.get("WIN_HARNESS_SERVE"):  # keep the fake headset + daemon up for a GUI process
        print("READY", flush=True)
        await task
        return

    unknown = await call("NoSuchMethod")
    result["unknown_method_error"] = unknown.error_name
    settings = await call("GetSettings", K.DBUS_SETTINGS_INTERFACE_NAME, K.DBUS_SETTINGS_OBJECT_PATH)
    result["get_settings"] = settings.message_type.name
    result["get_settings_has_general"] = "general" in json.loads(settings.body[0]) if settings.body else False

    result["writes_ffc0"] = len(backend.state[b"a-ffc0"]["writes"])
    result["ffc0_write_ids"] = sorted({w[0] for w in backend.state[b"a-ffc0"]["writes"]})
    result["ffc0_write_lens"] = sorted({len(w) for w in backend.state[b"a-ffc0"]["writes"]})
    result["feature_report_lens"] = sorted({len(f) for st in backend.state.values() for f in st["features"]})

    t0 = time.monotonic()
    os.kill(os.getpid(), signal.SIGINT)  # a real Ctrl-C
    try:
        await asyncio.wait_for(asyncio.shield(task), 25)
        result["shutdown_s"] = round(time.monotonic() - t0, 2)
        result["shutdown_clean"] = True
    except Exception as exc:  # noqa: BLE001
        result["shutdown_clean"] = False
        result["shutdown_error"] = repr(exc)
    # What the base station was told on the way out (see device_init / shutdown_commands in the YAML:
    # software-ChatMix mode is switched on at start and must be switched off again at exit).
    result["shutdown_tail"] = [w[:3] for w in backend.state[b"a-ffc0"]["writes"][-6:]]
    print("RESULT:" + json.dumps(result), flush=True)


asyncio.run(main())
