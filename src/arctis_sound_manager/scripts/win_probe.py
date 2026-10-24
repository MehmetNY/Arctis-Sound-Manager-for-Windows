# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Windows hardware probe: does hidapi reach the headset the way the port assumes?

    py -m pip install hidapi
    py -m arctis_sound_manager.scripts.win_probe            # read-only, writes nothing
    py -m arctis_sound_manager.scripts.win_probe --query    # + one status poll

Three stages, each reported on its own so a failure in one does not hide the rest:

  1. ENUMERATE  every SteelSeries (0x1038) HID node hidapi can see, with its
                interface number and usage page. Checks the assumption that
                ``interface_number`` matches the ``bInterfaceNumber`` the device
                YAML profiles use.
  2. OPEN       try to open each node and read its report descriptor. A failure
                here usually means another program (SteelSeries GG!) holds the
                interface exclusively.
  3. QUERY      (--query only) send the profile's status request — the very
                frame ASM polls every couple of seconds — through the pyusb
                shim and print what comes back. This is the end-to-end test of
                the shim: framing, report ids, padding, and the read loop.

Nothing is ever *configured*: no settings, EQ or OLED frames are sent.
The report goes to the console and to ``asm-win-probe.json`` (use ``--out``).
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

STEELSERIES = 0x1038


def _hex(data) -> str:
    return " ".join(f"{b:02x}" for b in data)


def stage_enumerate(hid) -> list[dict]:
    nodes = []
    for e in hid.enumerate(STEELSERIES, 0):
        nodes.append({
            "pid": f"{e['product_id']:04x}",
            "interface_number": e.get("interface_number"),
            "usage_page": f"{e.get('usage_page', 0):04x}",
            "usage": f"{e.get('usage', 0):04x}",
            "product": e.get("product_string"),
            "path": e["path"].decode("utf-8", "replace") if isinstance(e["path"], bytes) else str(e["path"]),
            "_raw": e,
        })
    return nodes


def stage_open(hid, nodes: list[dict]) -> None:
    from arctis_sound_manager.win.hid_descriptor import parse_report_descriptor

    for node in nodes:
        info: dict = {"opened": False}
        try:
            dev = hid.device()
            dev.open_path(node["_raw"]["path"])
            info["opened"] = True
            try:
                desc = bytes(dev.get_report_descriptor())
                info["descriptor_hex"] = desc.hex()
                cols = parse_report_descriptor(desc)
                info["collections"] = [{
                    "usage_page": f"{c.usage_page:04x}", "usage": f"{c.usage:04x}",
                    "numbered_reports": c.uses_report_ids, "report_ids": sorted(c.report_ids),
                    "input_len": c.input_len, "output_len": c.output_len, "feature_len": c.feature_len,
                } for c in cols]
            except Exception as exc:  # noqa: BLE001
                info["descriptor_error"] = repr(exc)
            finally:
                dev.close()
        except Exception as exc:  # noqa: BLE001
            info["open_error"] = repr(exc)
        node["open"] = info


def _profile_for(pid: int):
    from arctis_sound_manager.config import load_device_configurations

    for cfg in load_device_configurations():
        if cfg.vendor_id == STEELSERIES and pid in cfg.product_ids:
            return cfg
    return None


def stage_query(pid: int, listen_seconds: float) -> dict:
    """One status poll through the pyusb shim, exactly as CoreEngine frames it."""
    from arctis_sound_manager.config import CommandTransport
    from arctis_sound_manager.win.pyusb_shim import core, util

    result: dict = {"pid": f"{pid:04x}"}
    cfg = _profile_for(pid)
    if cfg is None:
        result["error"] = "no device profile for this product id"
        return result
    result["profile"] = cfg.name
    if cfg.status is None:
        result["error"] = "profile has no status request: nothing safe to poll"
        return result

    dev = core.find(idVendor=STEELSERIES, idProduct=pid)
    if dev is None:
        result["error"] = "device vanished between stages"
        return result

    cmd_iface = cfg.command_interface_index[0]
    result["command_interface"] = cmd_iface
    result["listen_interfaces"] = list(cfg.listen_interface_indexes)
    result["transport"] = cfg.command_transport.value

    # Same framing as CoreEngine.send_command(): hex string -> bytes -> pad with filler.
    frame = f"{cfg.status.request:02x}"
    frame = ("0" + frame) if len(frame) % 2 else frame
    frame_bytes = [int(frame[i:i + 2], 16) for i in range(0, len(frame), 2)]
    pad = cfg.command_padding
    frame_bytes += [pad.filler] * max(0, pad.length - len(frame_bytes))
    result["request_hex"] = _hex(frame_bytes[:8]) + " ..."

    # Pick the listen endpoint the same way guess_interface_endpoint() does.
    listen_ep = None
    for intf in dev.get_active_configuration():
        if intf.bInterfaceNumber in cfg.listen_interface_indexes:
            for ep in intf.endpoints():
                if util.endpoint_direction(ep.bEndpointAddress) == util.ENDPOINT_IN:
                    listen_ep = (ep.bEndpointAddress, ep.wMaxPacketSize)
                    break
        if listen_ep:
            break
    if listen_ep is None:
        result["error"] = "no listen endpoint found: interface numbers in the profile do not match hidapi's"
        return result
    result["listen_endpoint"] = f"0x{listen_ep[0]:02x}"

    try:
        if cfg.command_transport == CommandTransport.INTERRUPT:
            out_ep = next((ep.bEndpointAddress
                           for i in dev.get_active_configuration() if i.bInterfaceNumber == cmd_iface
                           for ep in i.endpoints()
                           if util.endpoint_direction(ep.bEndpointAddress) == util.ENDPOINT_OUT), None)
            if out_ep is None:
                result["error"] = "profile says interrupt OUT but the command interface exposes no output report"
                return result
            dev.write(out_ep, frame_bytes)
        else:
            rtype = 0x03 if cfg.command_transport == CommandTransport.CTRL_FEATURE else 0x02
            rid = cfg.command_report_id or 0
            req = util.build_request_type(util.CTRL_OUT, util.CTRL_TYPE_CLASS, util.CTRL_RECIPIENT_INTERFACE)
            dev.ctrl_transfer(req, 0x09, (rtype << 8) | (rid & 0xFF), cmd_iface, frame_bytes)
        result["request_sent"] = True
    except core.USBError as exc:
        result["request_sent"] = False
        result["send_error"] = f"errno={exc.errno}: {exc}"
        return result

    frames = []
    deadline = time.monotonic() + listen_seconds
    while time.monotonic() < deadline:
        try:
            data = dev.read(listen_ep[0], listen_ep[1], 300)
            frames.append(_hex(data[:16]) + (" ..." if len(data) > 16 else ""))
        except core.USBTimeoutError:
            continue
        except core.USBError as exc:
            result["read_error"] = f"errno={exc.errno}: {exc}"
            break
    result["frames"] = frames
    result["got_reply"] = bool(frames)
    util.dispose_resources(dev)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Windows HID probe for Arctis Sound Manager")
    ap.add_argument("--query", action="store_true",
                    help="also send the profile's status request through the shim and print the replies")
    ap.add_argument("--listen", type=float, default=2.0, help="seconds to listen for replies (default 2)")
    ap.add_argument("--out", default="asm-win-probe.json", help="where to write the JSON report")
    args = ap.parse_args(argv)

    if sys.platform != "win32":
        print("This probe is for Windows (it exercises hidapi against the Windows HID stack).")
        return 2
    try:
        import hid  # type: ignore[import-not-found]
    except ImportError:
        print("hidapi is missing. Install it with:  py -m pip install hidapi")
        return 2

    report: dict = {
        "python": sys.version.split()[0], "windows": platform.platform(),
        "hidapi": getattr(hid, "__version__", "?"),
    }
    print(f"Python {report['python']} · {report['windows']} · hidapi {report['hidapi']}\n")

    print("== 1/3 ENUMERATE ==")
    nodes = stage_enumerate(hid)
    if not nodes:
        print("  No SteelSeries (1038) HID device found. Is the headset/dongle plugged in and powered?")
    for n in nodes:
        print(f"  pid={n['pid']}  interface={n['interface_number']}  usage_page={n['usage_page']}  "
              f"usage={n['usage']}  {n['product']!r}")

    if nodes:
        print("\n== 2/3 OPEN + DESCRIPTORS ==")
        stage_open(hid, nodes)
        for n in nodes:
            o = n["open"]
            status = "OK" if o["opened"] else f"FAILED ({o.get('open_error')})"
            print(f"  pid={n['pid']} if={n['interface_number']} page={n['usage_page']}: {status}")
            for c in o.get("collections", []):
                print(f"      numbered={c['numbered_reports']} ids={c['report_ids']} "
                      f"in={c['input_len']} out={c['output_len']} feature={c['feature_len']}")
            if "descriptor_error" in o:
                print(f"      descriptor unavailable: {o['descriptor_error']}")
        if not any(n["open"]["opened"] for n in nodes):
            print("\n  Nothing could be opened. Close SteelSeries GG (and any other headset tool) and retry.")

    if args.query and nodes:
        print("\n== 3/3 STATUS QUERY (through the pyusb shim) ==")
        report["queries"] = []
        for pid in sorted({int(n["pid"], 16) for n in nodes}):
            q = stage_query(pid, args.listen)
            report["queries"].append(q)
            print(f"  pid={q['pid']} profile={q.get('profile')!r}")
            for key in ("transport", "command_interface", "listen_interfaces", "listen_endpoint",
                        "request_hex", "request_sent", "send_error", "read_error", "error"):
                if key in q:
                    print(f"      {key}: {q[key]}")
            for f in q.get("frames", []):
                print(f"      reply: {f}")
            if q.get("request_sent") and not q.get("got_reply"):
                print("      (request sent, no reply within the listen window)")
    elif args.query:
        print("\n(--query skipped: no device)")

    for n in nodes:
        n.pop("_raw", None)
    report["nodes"] = nodes
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport written to {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
