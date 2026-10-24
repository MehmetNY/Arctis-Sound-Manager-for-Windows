# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Windows audio probe: what does this PC offer an audio engine to work with?

    py -m arctis_sound_manager.scripts.win_audio_probe

Read-only. It plays nothing, records nothing and changes nothing. Everything it needs comes from
the Windows registry (the MMDevices endpoint list Windows itself keeps), plus a few optional
look-ups when the packages exist. A missing package is reported, never an error.

It answers the questions the audio engine's design depends on:

  * which playback and recording endpoints exist, in which format Windows mixes them (channel
    count and rate), and whether the headset base shows up as one device or several;
  * which virtual audio cables are installed (VB-CABLE, Voicemeeter, ...), because separate
    Game/Chat/Media channels need one each;
  * whether Equalizer APO is installed and its config folder can be written without admin rights;
  * whether SteelSeries GG / Sonar is still running (it owns the same devices);
  * with sounddevice installed: which channel counts WASAPI accepts on each endpoint in shared
    mode (can the headset take 8 channels for virtual 7.1?), and the host APIs.

The report goes to the console and to ``asm-audio-probe.json`` (use ``--out``).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import struct
import subprocess
import sys
import time
from pathlib import Path

MMDEVICES = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"

# property keys under <endpoint>\Properties (fmtid,pid as Windows writes them)
_PK_NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"          # PKEY_Device_DeviceDesc ("Speakers")
_PK_FRIENDLY = "{a45c254e-df1c-4efd-8020-67d146a850e0},14"     # PKEY_Device_FriendlyName
_PK_ADAPTER = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"       # the sound device it belongs to
_PK_DEVICE_FORMAT = "{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0"  # PKEY_AudioEngine_DeviceFormat (mix format)
_PK_OEM_FORMAT = "{e4870e26-3cc5-4cd2-ba46-ca0a9a70ed04},0"     # PKEY_AudioEngine_OEMFormat
_PK_FORM_FACTOR = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},0"    # PKEY_AudioEndpoint_FormFactor

DEVICE_STATES = {1: "active", 2: "disabled", 4: "not present", 8: "unplugged"}
FORM_FACTORS = {0: "network", 1: "speakers", 2: "line level", 3: "headphones", 4: "microphone",
                5: "headset", 6: "handset", 7: "unknown digital pass-through", 8: "SPDIF",
                9: "digital audio display (HDMI)", 10: "unknown"}

_SPEAKERS = [(0x1, "FL"), (0x2, "FR"), (0x4, "FC"), (0x8, "LFE"), (0x10, "BL"), (0x20, "BR"),
             (0x40, "FLC"), (0x80, "FRC"), (0x100, "BC"), (0x200, "SL"), (0x400, "SR")]
_COMMON_RATES = {8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000, 176400, 192000}
_SUBFORMATS = {"00000001-0000-0010-8000-00aa00389b71": "PCM",
               "00000003-0000-0010-8000-00aa00389b71": "float"}

# names that tell a virtual audio device from a real one
VIRTUAL_MARKERS = ("cable", "vb-audio", "voicemeeter", "virtual audio", "virtual cable", "scream",
                   "vac ", "hi-fi cable", "sonar", "steelseries sonar", "blackhole", "loopback")
CABLE_MARKERS = ("cable", "vb-audio", "voicemeeter", "vac ", "hi-fi cable")


# ── pure parsers (unit-tested without Windows) ───────────────────────────────

def _guid(raw: bytes) -> str:
    d1, d2, d3 = struct.unpack_from("<IHH", raw, 0)
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{raw[8:10].hex()}-{raw[10:16].hex()}"


def channel_mask_names(mask: int) -> list[str]:
    return [name for bit, name in _SPEAKERS if mask & bit]


def parse_waveformat(blob: bytes) -> dict | None:
    """The WAVEFORMATEX(TENSIBLE) inside a registry property blob, or None.

    The registry wraps it in a serialized PROPVARIANT header whose length differs between
    Windows builds, so rather than trusting an offset this looks for the first position where the
    fields agree with each other (block align = channels * bytes per sample, average rate =
    rate * block align).
    """
    for off in range(0, max(0, min(len(blob) - 15, 64))):
        tag, ch, rate, avg, align, bits = struct.unpack_from("<HHIIHH", blob, off)
        if tag not in (1, 3, 0xFFFE) or not 1 <= ch <= 32 or rate not in _COMMON_RATES:
            continue
        if bits not in (8, 16, 24, 32, 64) or align != ch * bits // 8 or avg != rate * align:
            continue
        info = {"format_tag": tag, "channels": ch, "rate": rate, "bits": bits}
        if tag == 0xFFFE and len(blob) >= off + 40:
            valid_bits, mask = struct.unpack_from("<HI", blob, off + 18)
            info.update(valid_bits=valid_bits, channel_mask=mask, speakers=channel_mask_names(mask),
                        subformat=_SUBFORMATS.get(_guid(blob[off + 24:off + 40]), _guid(blob[off + 24:off + 40])))
        else:
            info["subformat"] = "float" if tag == 3 else "PCM"
        return info
    return None


def classify_endpoint(name: str) -> dict:
    low = name.lower()
    return {
        "virtual": any(m in low for m in VIRTUAL_MARKERS),
        "cable": any(m in low for m in CABLE_MARKERS),
        "steelseries": "steelseries" in low or "arctis" in low,
    }


# ── registry readers ─────────────────────────────────────────────────────────

def _read_key(winreg, root, path: str, wow64: bool = True) -> dict[str, object]:
    """All values of a key as {name: value}; {} when it cannot be opened."""
    values: dict[str, object] = {}
    flags = winreg.KEY_READ | (getattr(winreg, "KEY_WOW64_64KEY", 0) if wow64 else 0)
    try:
        key = winreg.OpenKey(root, path, 0, flags)
    except OSError:
        return values
    try:
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            values[name] = value
            i += 1
    finally:
        winreg.CloseKey(key)
    return values


def _subkeys(winreg, root, path: str) -> list[str]:
    names: list[str] = []
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0))
    except OSError:
        return names
    try:
        i = 0
        while True:
            try:
                names.append(winreg.EnumKey(key, i))
            except OSError:
                break
            i += 1
    finally:
        winreg.CloseKey(key)
    return names


def read_endpoints(winreg) -> list[dict]:
    """Every playback and recording endpoint Windows knows, including disabled/unplugged ones."""
    endpoints: list[dict] = []
    root = winreg.HKEY_LOCAL_MACHINE
    for flow, label in (("Render", "playback"), ("Capture", "recording")):
        base = f"{MMDEVICES}\\{flow}"
        for guid in _subkeys(winreg, root, base):
            state = _read_key(winreg, root, f"{base}\\{guid}").get("DeviceState")
            props = _read_key(winreg, root, f"{base}\\{guid}\\Properties")
            name = str(props.get(_PK_NAME) or "").strip()
            adapter = str(props.get(_PK_ADAPTER) or "").strip()
            friendly = str(props.get(_PK_FRIENDLY) or "").strip() or (f"{name} ({adapter})" if adapter else name)
            if not friendly:
                continue
            blob = props.get(_PK_DEVICE_FORMAT)
            oem = props.get(_PK_OEM_FORMAT)
            form = props.get(_PK_FORM_FACTOR)
            endpoints.append({
                "flow": label,
                "id": guid,
                "name": name,
                "adapter": adapter,
                "friendly_name": friendly,
                "state": DEVICE_STATES.get(state, f"state {state}") if isinstance(state, int) else "?",
                "form_factor": FORM_FACTORS.get(form, form) if isinstance(form, int) else None,
                "mix_format": parse_waveformat(blob) if isinstance(blob, (bytes, bytearray)) else None,
                "oem_format": parse_waveformat(oem) if isinstance(oem, (bytes, bytearray)) else None,
                **classify_endpoint(friendly),
            })
    return endpoints


def read_equalizer_apo(winreg, exists=os.path.isdir, writable=lambda p: os.access(p, os.W_OK)) -> dict:
    """Is Equalizer APO installed, where, and may this user write its config folder?"""
    install = None
    for view in (0, getattr(winreg, "KEY_WOW64_32KEY", 0)):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\EqualizerAPO", 0, winreg.KEY_READ | view)
        except OSError:
            continue
        try:
            install = str(winreg.QueryValueEx(key, "InstallPath")[0])
        except OSError:
            pass
        finally:
            winreg.CloseKey(key)
        if install:
            break
    candidates = [p for p in (install, r"C:\Program Files\EqualizerAPO") if p]
    folder = next((p for p in candidates if exists(p)), None)
    config = os.path.join(folder, "config") if folder else None
    return {
        "installed": folder is not None,
        "install_path": folder,
        "config_dir": config if config and exists(config) else None,
        "config_writable": bool(config and exists(config) and writable(config)),
    }


def running_processes(run=subprocess.run) -> list[str]:
    try:
        out = run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001 - a probe never fails on this
        return []
    names = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if parts and parts[0]:
            names.append(parts[0].strip('"'))
    return names


def steelseries_processes(process_names: list[str]) -> list[str]:
    return sorted({n for n in process_names
                   if any(marker in n.lower().replace(" ", "") for marker in ("steelseries", "sonar"))})


def package_status() -> dict[str, bool]:
    return {name: importlib.util.find_spec(name) is not None
            for name in ("numpy", "scipy", "sounddevice", "soundfile", "pycaw", "comtypes", "hid",
                         "cffi", "PySide6", "ruamel.yaml", "pyrnnoise")}


def portaudio_report() -> dict | None:
    """Host APIs and, for WASAPI, which channel counts shared mode accepts. None without sounddevice."""
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        return {"error": f"sounddevice could not be imported: {exc!r}"} if importlib.util.find_spec("sounddevice") else None
    report: dict = {"version": getattr(sd, "__version__", "?"), "portaudio": sd.get_portaudio_version()[1]}
    try:
        apis = sd.query_hostapis()
        report["host_apis"] = [a["name"] for a in apis]
        wasapi_index = next((i for i, a in enumerate(apis) if "WASAPI" in a["name"]), None)
        report["default_output"] = sd.query_devices(kind="output")["name"] if sd.default.device[1] >= 0 else None
        report["default_input"] = sd.query_devices(kind="input")["name"] if sd.default.device[0] >= 0 else None
    except Exception as exc:  # noqa: BLE001
        report["error"] = repr(exc)
        return report
    devices = []
    if wasapi_index is not None:
        for index, dev in enumerate(sd.query_devices()):
            if dev["hostapi"] != wasapi_index:
                continue
            entry = {"index": index, "name": dev["name"], "in": dev["max_input_channels"],
                     "out": dev["max_output_channels"], "default_rate": dev["default_samplerate"],
                     "accepts_out": [], "accepts_in": []}
            for channels in (1, 2, 4, 6, 8):
                for kind, field, check in (("out", "accepts_out", sd.check_output_settings),
                                           ("in", "accepts_in", sd.check_input_settings)):
                    if dev[f"max_{'output' if kind == 'out' else 'input'}_channels"] < channels:
                        continue
                    try:
                        check(device=index, channels=channels, samplerate=48000, dtype="float32")
                        entry[field].append(channels)
                    except Exception:  # noqa: BLE001
                        pass
            devices.append(entry)
    report["wasapi_devices"] = devices
    return report


def audio_sessions() -> list[dict] | None:
    """Apps that currently have an audio session (needs pycaw). None when it is not installed."""
    if importlib.util.find_spec("pycaw") is None:
        return None
    try:
        from pycaw.pycaw import AudioUtilities
        sessions = []
        for session in AudioUtilities.GetAllSessions():
            process = getattr(session, "Process", None)
            sessions.append({"process": process.name() if process else "(system sounds)",
                             "pid": getattr(session, "ProcessId", None)})
        return sessions
    except Exception as exc:  # noqa: BLE001
        return [{"error": repr(exc)}]


# ── report ───────────────────────────────────────────────────────────────────

def _fmt(mix: dict | None) -> str:
    if not mix:
        return "format unknown"
    extra = f" [{' '.join(mix['speakers'])}]" if mix.get("speakers") else ""
    return f"{mix['channels']}ch {mix['rate']}Hz {mix['bits']}bit {mix.get('subformat', '')}{extra}"


def build_report(winreg, *, procs=None, packages=None, portaudio=None, sessions=None, apo=None) -> dict:
    endpoints = read_endpoints(winreg)
    procs = running_processes() if procs is None else procs
    return {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "windows_build": getattr(sys, "getwindowsversion", lambda: None)() and tuple(sys.getwindowsversion()[:3]),
        "endpoints": endpoints,
        "equalizer_apo": apo if apo is not None else read_equalizer_apo(winreg),
        "steelseries_processes": steelseries_processes(procs),
        "packages": package_status() if packages is None else packages,
        "portaudio": portaudio_report() if portaudio is None else portaudio,
        "audio_sessions": audio_sessions() if sessions is None else sessions,
    }


def format_report(report: dict) -> str:
    lines = [f"ASM ses probu  {report['when']}",
             f"Python {report['python']}  |  {report['platform']}  |  build {report['windows_build']}", ""]
    for flow, title in (("playback", "CALMA (hoparlor/kulaklik) uc noktalari"), ("recording", "KAYIT (mikrofon) uc noktalari")):
        lines.append(title)
        rows = [e for e in report["endpoints"] if e["flow"] == flow]
        if not rows:
            lines.append("   (yok)")
        for e in sorted(rows, key=lambda e: (e["state"] != "active", e["friendly_name"])):
            tags = [t for t, on in (("SANAL", e["virtual"]), ("KABLO", e["cable"]), ("STEELSERIES", e["steelseries"])) if on]
            lines.append(f"   [{e['state']:>11}] {e['friendly_name']}  |  {_fmt(e['mix_format'])}"
                         + (f"  |  {e['form_factor']}" if e.get("form_factor") else "")
                         + (f"  <{', '.join(tags)}>" if tags else ""))
        lines.append("")
    cables = [e for e in report["endpoints"] if e["cable"] and e["state"] == "active"]
    lines.append(f"Sanal kablo/ses surucusu (aktif): {len(cables)} uc nokta" + ("" if cables else "  -> hic yok"))
    apo = report["equalizer_apo"]
    lines.append("Equalizer APO: " + (f"kurulu ({apo['install_path']}), config klasoru "
                 + ("YAZILABILIR" if apo["config_writable"] else "yazilamaz / yok") if apo["installed"] else "kurulu degil"))
    lines.append("SteelSeries sureclari: " + (", ".join(report["steelseries_processes"]) or "calismiyor"))
    lines.append("Paketler: " + ", ".join(f"{k}={'var' if v else 'YOK'}" for k, v in report["packages"].items()))
    pa = report["portaudio"]
    if pa is None:
        lines.append("PortAudio/sounddevice: kurulu degil (py -m pip install sounddevice numpy scipy)")
    elif "wasapi_devices" not in pa:
        lines.append(f"PortAudio/sounddevice: {pa.get('error', 'WASAPI bulunamadi')}")
    else:
        lines.append(f"PortAudio {pa['portaudio']}  |  varsayilan cikis: {pa.get('default_output')}  |  giris: {pa.get('default_input')}")
        lines.append("WASAPI (paylasimli mod) kabul edilen kanal sayilari @48kHz:")
        for d in pa["wasapi_devices"]:
            lines.append(f"   {d['name']}: cikis {d['accepts_out'] or '-'}  giris {d['accepts_in'] or '-'}")
    sessions = report["audio_sessions"]
    if sessions is not None:
        lines.append("Ses oturumu olan uygulamalar: " + (", ".join(sorted({s.get('process', '?') for s in sessions})) or "(yok)"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ASM Windows ses probu (salt okunur)")
    parser.add_argument("--out", default="asm-audio-probe.json")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    if sys.platform != "win32":
        print("Bu prob yalnizca Windows icindir.")
        return 2
    import winreg
    report = build_report(winreg)
    print(format_report(report))
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nAyrintili rapor: {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
