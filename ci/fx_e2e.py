#!/usr/bin/env python3
"""The microphone effect inside a real Windows audio engine.

The CI machine has no sound card, so VB-CABLE (installed by ci/setup_audio.ps1 as a test fixture only) provides a
microphone endpoint: "CABLE Output" is a capture device that records whatever is played into "CABLE Input". The
effect is attached to *that* endpoint exactly as it is attached to the headset's microphone on a real PC; a tone is
played in, recorded, and the level of the recording says what the effect did to it. This script stands in for the ASM
audio engine (it only multiplies the microphone by 0.25), so the test is the transport and the effect, not the DSP.

Timeline of one case (seconds after the recording started) and what the recording must show:

    0 - 4     no engine is there                    -> the tone passes through untouched   (level A)
    4 - 9.5   an engine claims the rings, wants it  -> the tone comes back at a quarter     (level A / 4)
    9.5 - 13.5 the engine says "nothing to process" -> untouched again (bypass)              (level A)
    13.5 - 17.5 wants it again                      -> a quarter again
    17.5 - 22  the engine goes away                 -> untouched again

It is run once for every way of attaching the effect that could work (which slot of the endpoint's FxProperties, whether
the endpoint builder is restarted too, whether Windows' audio protection is switched off), to learn from a real Windows
which of them the product must use. Every case ends with a dump of what Windows and the effect's own trace (see
native/micfx/asm_micfx.cpp: Trace) say, so a failure explains itself.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
LOG = open(ROOT / "fx_e2e.log", "w", encoding="utf-8")
BUILT = ROOT / "build" / "asm_micfx.dll"
# where the product keeps it: Windows' audio service (a different account) must be able to read the file
DLL = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ArctisSoundManager" / "fx" / "asm_micfx.dll"
TRACE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp" / "asm_micfx_trace.log"
ENDPOINT = {"guid": ""}
RATE = 48000
AMPLITUDE = 0.5
GAIN = 0.25
TIMELINE = {"engine_on": 4.0, "bypass_on": 9.5, "bypass_off": 13.5, "engine_off": 17.5, "end": 22.0}
FAILS: list[str] = []


def log(*parts) -> None:
    text = " ".join(str(p) for p in parts)
    print(text, flush=True)
    LOG.write(text + "\n")
    LOG.flush()


def sh(cmd: list[str], timeout: float = 120.0, env: dict | None = None, lines: int = 60) -> str:
    log("$", " ".join(cmd))
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})})
    except Exception as exc:                                              # noqa: BLE001
        log("   failed:", repr(exc))
        return ""
    out = ((done.stdout or "") + (done.stderr or "")).strip()
    for line in out.splitlines()[:lines]:
        log("   " + line)
    return out


def fx(*args: str) -> str:
    code = "import sys; from arctis_sound_manager.win.fx import cli; sys.exit(cli.main(sys.argv[1:]))"
    return sh([sys.executable, "-c", code, *args], env={"PYTHONPATH": str(ROOT / "src"), "ASM_MICFX_DLL": str(DLL)})


def endpoint_guid() -> str:
    out = fx("status", "--match", "CABLE Output", "--json")
    try:
        wanted = json.loads(out.splitlines()[-1])["wanted"]
        return wanted[0] if wanted else ""
    except Exception:                                                     # noqa: BLE001
        return ""


def safe(fn):
    """A diagnosis step that fails is a line in the log, never the end of the test."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                                          # noqa: BLE001
            log(f"   ({fn.__name__} failed: {exc!r})")
    wrapper.__name__ = fn.__name__
    return wrapper


def ps(script: str, timeout: float = 120.0) -> str:
    return sh(["powershell", "-NoProfile", "-Command", script], timeout=timeout)


def trace_text(clear: bool = True) -> str:
    try:
        text = TRACE.read_text(encoding="utf-8", errors="replace")
        if clear:
            TRACE.write_text("", encoding="utf-8")
        return text
    except OSError as exc:
        return f"(no trace file: {exc!r})"


@safe
def diagnose(label: str) -> None:
    """What Windows and the effect say after a case."""
    log(f"--- diagnosis: {label}")
    guid = ENDPOINT["guid"]
    base = rf"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture\{guid}"
    sh(["reg", "query", base, "/s"], lines=150)
    sh(["reg", "query", r"HKLM\SOFTWARE\Classes\CLSID\{A998FC6D-EC01-4F8A-BB8F-B20F8F4A5808}", "/s"])
    sh(["reg", "query", r"HKLM\SOFTWARE\Classes\AudioEngine\AudioProcessingObjects\{A998FC6D-EC01-4F8A-BB8F-B20F8F4A5808}"])
    sh(["reg", "query", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Audio", "/v", "DisableProtectedAudioDG"])
    ps("Get-Process audiodg -ErrorAction SilentlyContinue | ForEach-Object { 'audiodg pid ' + $_.Id + ' started ' + $_.StartTime }")
    ps("try { (Get-Process audiodg).Modules | Where-Object { $_.ModuleName -match 'asm|micfx' } | ForEach-Object { 'LOADED ' + $_.FileName } } catch { 'modules not readable: ' + $_.Exception.Message }")
    sh(["tasklist", "/m", "/fi", "imagename eq audiodg.exe"])
    log("the effect's own trace:")
    for line in trace_text().splitlines()[:80]:
        log("   | " + line)
    ps("foreach ($n in 'Microsoft-Windows-CodeIntegrity/Operational') { Get-WinEvent -FilterHashtable @{LogName=$n; StartTime=(Get-Date).AddMinutes(-4)} "
       "-ErrorAction SilentlyContinue | Select-Object -First 15 | ForEach-Object { $n + ' ' + $_.TimeCreated.ToString('HH:mm:ss') + ' id ' + $_.Id + ': ' + "
       "($_.Message -replace '\\s+',' ').Substring(0, [Math]::Min(400, $_.Message.Length)) } }")
    ps("Get-WinEvent -ListLog *Audio* -ErrorAction SilentlyContinue | ForEach-Object { $_.LogName + ' (' + $_.RecordCount + ' records, enabled ' + $_.IsEnabled + ')' }")
    ps("foreach ($n in 'Microsoft-Windows-Audio/Operational','System','Application') { "
       "Get-WinEvent -FilterHashtable @{LogName=$n; StartTime=(Get-Date).AddMinutes(-3)} -ErrorAction SilentlyContinue "
       "| Where-Object { $_.ProviderName -match 'Audio|audiodg|Application Error|Windows Error' -or $_.Message -match 'audiodg|asm_micfx|APO' } "
       "| Select-Object -First 12 | ForEach-Object { $n + ' ' + $_.TimeCreated.ToString('HH:mm:ss') + ' id ' + $_.Id + ' ' + $_.ProviderName + ': ' + ($_.Message -replace '\\s+',' ').Substring(0, [Math]::Min(300, $_.Message.Length)) } }")


COM_PROBE = r"""
import ctypes, sys
from ctypes import wintypes
class GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]
def guid(text):
    import uuid
    u = uuid.UUID(text)
    g = GUID()
    g.a, g.b, g.c = u.time_low, u.time_mid, u.time_hi_version
    for i, byte in enumerate(u.bytes[8:]):
        g.d[i] = byte
    return g
ole = ctypes.OleDLL("ole32")
ole.CoInitializeEx(None, 0)
iface = ctypes.c_void_p()
try:
    ole.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    hr = ole.CoCreateInstance(ctypes.byref(guid("A998FC6D-EC01-4F8A-BB8F-B20F8F4A5808")), None, 1, ctypes.byref(guid("FD7F2B29-24D0-4B5C-B177-592C39F9CA10")), ctypes.byref(iface))
    print("CoCreateInstance hr=0x%08x object=%s" % (hr & 0xFFFFFFFF, bool(iface.value)))
    if iface.value:
        vtbl = ctypes.cast(ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        print("Release ->", release(iface))
except Exception as exc:
    print("COM probe failed:", repr(exc))
"""


@safe
def probe_com() -> None:
    """Can any process create the effect through COM, with the registration the product writes? (Then a failure to load
    it in the audio engine is about the audio engine, not the DLL or the registration.)"""
    log("--- COM activation from an ordinary process")
    sh([sys.executable, "-c", COM_PROBE])
    for line in trace_text().splitlines()[:20]:
        log("   | " + line)


ETL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp" / "asmaudio.etl"
PROVIDERS = [("{AE4BD3BE-F36F-45B6-8D21-BDD6FB832853}", "0xffffffffffffffff", "0xff"),      # Microsoft-Windows-Audio
             ("{70EB4F03-C1DE-4F73-A051-33D13D5413BD}", "0xffffffffffffffff", "0xff"),      # Microsoft-Windows-Kernel-Registry
             ("{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}", "0x40", "0xff")]                    # Microsoft-Windows-Kernel-Process: image loads


def audio_provider_lines() -> list[tuple[str, str, str]]:
    """Every ETW provider on this machine that has to do with audio: (name, guid, level)."""
    out = subprocess.run(["logman", "query", "providers"], capture_output=True, text=True, timeout=60).stdout
    found = []
    for line in out.splitlines():
        m = re.match(r"^(?P<name>.+?)\s+(?P<guid>\{[0-9A-Fa-f-]{36}\})\s*$", line.strip())
        if m and re.search(r"audio|mmdevice|sound", m.group("name"), re.I) and "Kernel" not in m.group("name"):
            found.append((m.group("guid"), "0xffffffffffffffff", "0xff", m.group("name").strip()))
    return found[:14]


def etw(start: bool) -> None:
    """Windows' own trace around a case: which registry keys the audio engine reads (does it ever look at the effect, and
    what exactly does it ask for?), which DLLs the audio engine loads, and what the audio providers themselves say."""
    try:
        if start:
            sh(["logman", "stop", "asmaudio", "-ets"], lines=2)
            pf = ETL.with_suffix(".providers")
            audio = audio_provider_lines()
            log("   audio providers traced:", "; ".join(f"{p[3]} {p[0]}" for p in audio) or "(none found)")
            lines = [" ".join(p[:3]) for p in PROVIDERS] + [" ".join(p[:3]) for p in audio if p[0].upper() not in {q[0].upper() for q in PROVIDERS}]
            pf.write_text("\n".join(lines) + "\n", encoding="ascii")
            sh(["logman", "start", "asmaudio", "-pf", str(pf), "-o", str(ETL), "-ets", "-bs", "1024", "-nb", "64", "256"])
            return
        sh(["logman", "stop", "asmaudio", "-ets"])
        csv_path = ETL.with_suffix(".csv")
        sh(["tracerpt", str(ETL), "-o", str(csv_path), "-of", "CSV", "-y"], timeout=300, lines=6)
        import csv as csvmod
        wanted = re.compile(r"A998FC6D|asm_micfx", re.I)
        interesting = re.compile(r"A998FC6D|FxProperties|AudioProcessingObjects|" + re.escape(ENDPOINT["guid"].strip("{}")), re.I)
        pid_hex = "0x%08x" % AUDIODG["pid"] if AUDIODG["pid"] else None
        pid_dec = str(AUDIODG["pid"]) if AUDIODG["pid"] else None
        total = 0
        paths: dict[str, str] = {}                  # key object -> path, learnt from the opens
        reads: list[str] = []                       # what the other processes asked of the effect / the endpoint
        reads_audiodg: list[str] = []
        audio_events: list[str] = []
        images: list[str] = []
        with open(csv_path, encoding="utf-8", errors="replace", newline="") as handle:
            for row in csvmod.reader(handle):
                total += 1
                if len(row) < 2:
                    continue
                provider, event = row[0].strip(), row[1].strip()
                text = ",".join(row)
                if provider.startswith("Microsoft-Windows-Kernel-Registry") and len(row) > 24:
                    pid = row[9].strip().lower()
                    name = row[24].strip().strip('"') if event != "QueryValueKey" else row[24].strip().strip('"')
                    if event in ("OpenKey", "CreateKey"):
                        base = paths.get(row[19].strip(), "")
                        paths[row[20].strip()] = (base + "\\" + name) if base and not name.upper().startswith(("\\REGISTRY", "\\Registry".upper())) else name
                        shown = paths[row[20].strip()]
                        status = row[21].strip()
                    elif event in ("QueryValueKey", "SetValueKey", "EnumerateValueKey", "DeleteValueKey"):
                        shown = paths.get(row[19].strip(), "?") + "  VALUE " + name
                        status = row[20].strip()
                    else:
                        continue
                    if event in ("CreateKey", "SetValueKey", "DeleteValueKey") and wanted.search(shown) and pid != pid_hex:
                        continue                                                # our own tools writing the registration
                    if event == "EnumerateValueKey" or "ArctisSoundManager\\MicFx" in shown:
                        continue                                                # noise
                    if interesting.search(shown):
                        mine = bool(pid_hex) and pid == pid_hex
                        if "Render" in shown and not wanted.search(shown):
                            continue
                        if mine and len(reads_audiodg) < 500:
                            reads_audiodg.append(f"AUDIODG      {event:16} {status:11} {shown[-170:]}")
                        elif not mine and len(reads) < 120:
                            reads.append(f"pid {pid} {event:16} {status:11} {shown[-170:]}")
                elif provider.startswith("Microsoft-Windows-Kernel-Process") and "image" in event.lower():
                    if (pid_hex and pid_hex in text.lower()) or (pid_dec and re.search(r"(^|,)\s*" + pid_dec + r"\s*(,|$)", text)):
                        images.append(re.sub(r"\s+", " ", text)[-170:])
                elif not provider.startswith("Microsoft-Windows-Kernel") and len(audio_events) < 150:
                    audio_events.append(re.sub(r"\s+", " ", text)[:260])
        log(f"   {total} trace events; audiodg is {pid_hex}")
        log("   what audiodg read about the effect and the endpoint:")
        for line in reads_audiodg:
            log("   r " + line)
        log("   what the other processes did with them (first 120):")
        for line in reads:
            log("   o " + line)
        log(f"   image loads of audiodg: {len(images)}")
        for line in images[:100]:
            log("   > " + line)
        log(f"   events of the audio providers: {len(audio_events)} (first 150)")
        for line in audio_events:
            log("   a " + line)
    except Exception as exc:                                              # noqa: BLE001 — a diagnosis must never end the test
        log("   (trace failed: %r)" % exc)


AUDIODG: dict = {"pid": None}


@safe
def snapshot_audiodg() -> None:
    """While the streams are open: what the audio engine process has loaded."""
    log("--- audiodg while a stream is open")
    out = sh(["tasklist", "/fi", "imagename eq audiodg.exe", "/fo", "csv", "/nh"], lines=3)
    found = re.search(r'"audiodg\.exe","(\d+)"', out or "", re.I)
    AUDIODG["pid"] = int(found.group(1)) if found else None
    ps("(Get-Process audiodg).Modules | ForEach-Object { $_.ModuleName } | Sort-Object | Out-String -Width 250", 60)
    ps("$p = (Get-Process audiodg).Id; 'audiodg pid ' + $p; Get-ProcessMitigation -Id $p | Out-String -Width 200", 60)


def install_dll() -> None:
    DLL.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUILT, DLL)
    sh(["icacls", str(DLL)])
    sh(["icacls", str(BUILT)])
    TRACE.write_text("", encoding="utf-8")
    sh(["icacls", str(TRACE), "/grant", "*S-1-1-0:(F)"])                  # Everyone: the audio service writes the trace


def find_devices():
    import sounddevice as sd
    apis = sd.query_hostapis()
    wasapi = next(i for i, a in enumerate(apis) if "WASAPI" in a["name"])
    mic = play = None
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != wasapi:
            continue
        if "CABLE Output" in d["name"] and d["max_input_channels"] > 0 and mic is None:
            mic = i
        if "CABLE Input" in d["name"] and d["max_output_channels"] > 0 and "16ch" not in d["name"] and play is None:
            play = i
    return mic, play


class Engine(threading.Thread):
    """Stands in for the ASM audio engine: claims the rings and multiplies the microphone by GAIN."""

    def __init__(self):
        super().__init__(daemon=True)
        self.ctl = {"run": False, "wants": True, "stop": False}
        self.report: dict = {"mapping_seen_s": None, "samples": []}
        self.t0 = time.monotonic()

    def run(self) -> None:
        from arctis_sound_manager.win.fx import shared
        link = None
        claimed = False
        last_info = 0.0
        while not self.ctl["stop"]:
            if link is None:
                link = shared.open_link()
                if link is None:
                    time.sleep(0.1)
                    continue
                self.report["mapping_seen_s"] = round(time.monotonic() - self.t0, 2)
            now = time.monotonic()
            if now - last_info > 1.0:
                last_info = now
                self.report["samples"].append({"t": round(now - self.t0, 1), **link.info()})
            if self.ctl["run"]:
                if not claimed:
                    claimed = link.claim(wants=self.ctl["wants"], lead_frames=960)
                    if not claimed:
                        time.sleep(0.1)
                        continue
                link.heartbeat(self.ctl["wants"], 960)
                link.sync()
                link.wait(0.02)
                rate, channels = link.format()
                channels = channels if channels in (1, 2) else 2
                while link.available() >= 480:
                    link.write(link.read(480, channels) * GAIN)
            else:
                if claimed:
                    link.release()
                    claimed = False
                time.sleep(0.05)
        if link is not None:
            link.close()


def rms(samples: np.ndarray, start_s: float, end_s: float) -> float:
    part = samples[int(start_s * RATE):int(end_s * RATE)]
    return float(np.sqrt(np.mean(part ** 2))) if part.size else float("nan")


def timeline(quick: bool = True, snapshot=None) -> dict:
    import sounddevice as sd
    mic = play = None
    for attempt in range(12):                                             # right after the endpoint builder restarted, the endpoints come back late
        try:                                                              # the device list of an earlier case may be stale
            sd._terminate()
            sd._initialize()
        except Exception as exc:                                          # noqa: BLE001
            log("   (could not refresh PortAudio:", repr(exc), ")")
        mic, play = find_devices()
        if mic is not None and play is not None:
            break
        time.sleep(2)
    log(f"devices: capture {mic} playback {play} (attempt {attempt + 1})")
    if mic is None or play is None:
        raise RuntimeError("no CABLE endpoints")
    extra = sd.WasapiSettings(auto_convert=True)
    phase = [0]
    chunks: list[np.ndarray] = []
    chunks_b: list[np.ndarray] = []                                       # a second program recording the same microphone

    def out_cb(outdata, frames, _t, _status):
        t = (phase[0] + np.arange(frames)) / RATE
        outdata[:] = (AMPLITUDE * np.sin(2 * math.pi * 1000.0 * t))[:, None]
        phase[0] += frames

    def in_cb(indata, _frames, _t, _status):
        chunks.append(indata[:, 0].copy())

    def in_cb_b(indata, _frames, _t, _status):
        chunks_b.append(indata[:, 0].copy())

    engine = Engine()
    engine.start()
    started = time.monotonic()
    result: dict = {}
    with sd.OutputStream(device=play, samplerate=RATE, channels=2, dtype="float32", callback=out_cb, extra_settings=extra), \
         sd.InputStream(device=mic, samplerate=RATE, channels=2, dtype="float32", callback=in_cb, extra_settings=extra), \
         sd.InputStream(device=mic, samplerate=RATE, channels=1, dtype="float32", callback=in_cb_b, extra_settings=extra):
        steps = [(TIMELINE["engine_on"], lambda: engine.ctl.update(run=True, wants=True)),
                 (TIMELINE["bypass_on"], lambda: engine.ctl.update(wants=False)),
                 (TIMELINE["bypass_off"], lambda: engine.ctl.update(wants=True)),
                 (TIMELINE["engine_off"], lambda: engine.ctl.update(run=False)),
                 (TIMELINE["end"], None)]
        for at, action in steps:
            while time.monotonic() - started < at:
                time.sleep(0.02)
            if action:
                action()
                log(f"t={at:5.1f}s  {engine.report['samples'][-1] if engine.report['samples'] else '(effect not seen yet)'}")
            if quick and at == TIMELINE["engine_on"]:                    # give the effect a few seconds to show itself
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline and engine.report["mapping_seen_s"] is None:
                    time.sleep(0.05)
                if engine.report["mapping_seen_s"] is None:
                    log("   the effect never showed itself: cutting this case short")
                    if snapshot is not None:
                        snapshot()
                    result["aborted"] = True
                    break
    engine.ctl["stop"] = True
    engine.join(3)
    if result.get("aborted"):
        result["engine"] = engine.report
        result["levels"] = {}
        return result
    recording = np.concatenate(chunks) if chunks else np.zeros(0)
    log(f"recorded {recording.size / RATE:.1f} s")
    result["levels"] = {
        "no_engine": rms(recording, 1.5, 3.5),
        "processing_1": rms(recording, 6.0, 9.0),
        "bypass": rms(recording, 11.0, 13.0),
        "processing_2": rms(recording, 15.0, 17.0),
        "engine_gone": rms(recording, 19.5, 21.5),
    }
    recording_b = np.concatenate(chunks_b) if chunks_b else np.zeros(0)
    result["levels_b"] = {                                                # the second program must be processed too
        "b_no_engine": rms(recording_b, 1.5, 3.5),
        "b_processing_1": rms(recording_b, 6.0, 9.0),
        "b_bypass": rms(recording_b, 11.0, 13.0),
        "b_processing_2": rms(recording_b, 15.0, 17.0),
        "b_engine_gone": rms(recording_b, 19.5, 21.5),
    }
    result["engine"] = engine.report
    return result


def check(levels: dict) -> dict:
    full = AMPLITUDE / math.sqrt(2)
    quarter = full * GAIN
    verdict = {}
    for name, level in levels.items():
        want = quarter if name.startswith(("processing", "b_processing")) else full
        verdict[name] = {"level": round(level, 4), "expected": round(want, 4), "ok": bool(want * 0.7 <= level <= want * 1.3)}
    return verdict


VARIANTS = [
    # (title, arguments of "attach", does it use the product's way of switching Windows' audio protection off)
    # The first case is the product's own way (no arguments: whatever the program attaches by default); it has to work.
    # The others show what else works on this Windows, to choose from if the product's way does not on some machine.
    # Every case records the microphone with two programs at once: both must get the processed sound.
    ("the product's way (default slots)", [], True),
    ("GFX, endpoint builder restarted too", ["--slots", "gfx", "--builder"], True),
    ("LFX + GFX (the old pair)", ["--slots", "lfx,gfx"], True),
    ("SFX (stream effect)", ["--slots", "sfx"], True),
    ("GFX, audio protection left alone", ["--slots", "gfx"], False),
]


def case(title: str, extra: list[str], protect: bool) -> dict:
    log("")
    log(f"########## {title}")
    fx("detach")
    trace_text()
    deep = title.startswith("the product's way")         # the full diagnosis for two of the cases
    if deep:
        etw(True)
    fx("attach", "--match", "CABLE Output", *extra, *(() if protect else ("--no-protect",)))
    fx("status", "--match", "CABLE Output")
    if deep:
        probe_com()
    time.sleep(3)
    summary: dict = {"title": title, "protect": protect, "args": extra}
    try:
        result = timeline(snapshot=snapshot_audiodg if deep else None)
        summary.update(result)
        summary["verdict"] = check({**result["levels"], **result.get("levels_b", {})}) if result.get("levels") else {}
        text = trace_text(clear=False)
        summary["owners"] = text.count("LockForProcess done, owner=1")
        summary["followers"] = text.count("LockForProcess done, owner=0")
        summary["follows"] = text.count("follower starts")
    except Exception as exc:                                              # noqa: BLE001
        log("   timeline failed:", repr(exc))
        summary["error"] = repr(exc)
    if deep:
        etw(False)
    diagnose(title)
    fx("detach")
    return summary


def passed(summary: dict) -> bool:
    verdict = summary.get("verdict") or {}
    return bool(verdict) and all(v["ok"] for v in verdict.values())


def main() -> int:
    if not BUILT.is_file():
        log("build\\asm_micfx.dll is missing")
        return 1
    install_dll()
    sh(["powershell", "-NoProfile", "-Command",
        "Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue | Format-Table FriendlyName, Status, InstanceId -AutoSize | Out-String -Width 250"])
    ENDPOINT["guid"] = endpoint_guid()
    log("endpoint under test:", ENDPOINT["guid"] or "(not found)")
    summaries = []
    for title, extra, protect in VARIANTS:
        summaries.append(case(title, extra, protect))
        if passed(summaries[-1]) and len(summaries) >= 2 and not protect:
            break
    log("")
    log("========== SUMMARY")
    for s in summaries:
        verdict = s.get("verdict") or {}
        seen = s.get("engine", {}).get("mapping_seen_s")
        log(f"[{'PASS' if passed(s) else 'fail'}] {s['title']}: "
            + ("; ".join(f"{k} {v['level']} (want {v['expected']}) {'ok' if v['ok'] else 'WRONG'}" for k, v in verdict.items())
               or s.get("error") or "the effect never loaded")
            + (f"  (shared memory after {seen} s)" if seen is not None else "")
            + (f"  [instances: {s.get('owners')} owner, {s.get('followers')} follower; {s.get('follows')} follows]" if "owners" in s else ""))
        samples = s.get("engine", {}).get("samples") or []
        if samples:
            log("      last report from the effect:", json.dumps(samples[-1]))
    working = [s for s in summaries if passed(s)]
    if not working:
        FAILS.append("no way of attaching the effect worked")
    log("Ways that worked:", "; ".join(s["title"] for s in working) or "none")
    if not passed(summaries[0]):
        FAILS.append("the product's own way of attaching the effect does not work")
    protected = [s for s in summaries if not s["protect"]]
    if protected:
        log("Windows loads the effect without switching the audio protection off: " + ("YES" if passed(protected[-1]) else "no"))
    Path(ROOT / "fx_e2e.json").write_text(json.dumps(summaries, indent=1, default=str), encoding="utf-8")
    log("RESULT:", "FAIL " + "; ".join(FAILS) if FAILS else "all good")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
