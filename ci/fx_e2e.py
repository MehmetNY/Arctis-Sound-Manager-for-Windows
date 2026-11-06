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


def probe_com() -> None:
    """Can any process create the effect through COM, with the registration the product writes? (Then a failure to load
    it in the audio engine is about the audio engine, not the DLL or the registration.)"""
    log("--- COM activation from an ordinary process")
    sh([sys.executable, "-c", COM_PROBE])
    for line in trace_text().splitlines()[:20]:
        log("   | " + line)


ETL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp" / "asmaudio.etl"


def etw(start: bool) -> None:
    """Windows' own trace of its audio engine around a case: it names what it does with every effect it meets."""
    if start:
        sh(["logman", "stop", "asmaudio", "-ets"])
        sh(["logman", "start", "asmaudio", "-p", "{AE4BD3BE-F36F-45B6-8D21-BDD6FB832853}", "0xffffffffffffffff", "0xff",
            "-o", str(ETL), "-ets"])
        return
    sh(["logman", "stop", "asmaudio", "-ets"])
    xml = ETL.with_suffix(".xml")
    sh(["tracerpt", str(ETL), "-o", str(xml), "-of", "XML", "-y", "-summary", str(ETL.with_suffix(".txt"))])
    try:
        text = xml.read_text(encoding="utf-16", errors="replace")
    except OSError:
        try:
            text = xml.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log("   (no trace: %r)" % exc)
            return
    import re
    events = re.findall(r"<Event .*?</Event>", text, flags=re.S)
    log(f"   {len(events)} audio events; the ones that mention effects:")
    shown = 0
    for e in events:
        if re.search(r"APO|[Ee]ffect|A998FC6D|asm_micfx|CLSID|SFX|EFX|MFX|Fx", e):
            flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e))
            log("   * " + flat[:300])
            shown += 1
            if shown >= 40:
                break


def snapshot_audiodg() -> None:
    """While the streams are open: what the audio engine process has loaded."""
    log("--- audiodg while a stream is open")
    ps("(Get-Process audiodg).Modules | ForEach-Object { $_.ModuleName } | Sort-Object | Out-String -Width 250", 60)
    sh(["tasklist", "/m", "/fi", "imagename eq audiodg.exe"], lines=40)


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
    mic, play = find_devices()
    log(f"devices: capture {mic} playback {play}")
    if mic is None or play is None:
        raise RuntimeError("no CABLE endpoints")
    extra = sd.WasapiSettings(auto_convert=True)
    phase = [0]
    chunks: list[np.ndarray] = []

    def out_cb(outdata, frames, _t, _status):
        t = (phase[0] + np.arange(frames)) / RATE
        outdata[:] = (AMPLITUDE * np.sin(2 * math.pi * 1000.0 * t))[:, None]
        phase[0] += frames

    def in_cb(indata, _frames, _t, _status):
        chunks.append(indata[:, 0].copy())

    engine = Engine()
    engine.start()
    started = time.monotonic()
    result: dict = {}
    with sd.OutputStream(device=play, samplerate=RATE, channels=2, dtype="float32", callback=out_cb, extra_settings=extra), \
         sd.InputStream(device=mic, samplerate=RATE, channels=2, dtype="float32", callback=in_cb, extra_settings=extra):
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
    result["engine"] = engine.report
    return result


def check(levels: dict) -> dict:
    full = AMPLITUDE / math.sqrt(2)
    quarter = full * GAIN
    verdict = {}
    for name, level in levels.items():
        want = quarter if name.startswith("processing") else full
        verdict[name] = {"level": round(level, 4), "expected": round(want, 4), "ok": bool(want * 0.7 <= level <= want * 1.3)}
    return verdict


VARIANTS = [
    # (title, arguments of "attach", does it use the product's way of switching Windows' audio protection off)
    ("EFX, audio service restarted (the plan)", ["--slots", "efx"], True),
    ("EFX, endpoint builder restarted too", ["--slots", "efx", "--builder"], True),
    ("SFX (stream effect)", ["--slots", "sfx"], True),
    ("SFX + MFX + EFX", ["--slots", "sfx,mfx,efx", "--builder"], True),
    ("MFX (mode effect)", ["--slots", "mfx"], True),
    ("LFX (old pre-mix)", ["--slots", "lfx"], True),
    ("LFX + GFX (old pre- and post-mix)", ["--slots", "lfx,gfx", "--builder"], True),
    ("EFX, audio protection left alone", ["--slots", "efx"], False),
]


def case(title: str, extra: list[str], protect: bool) -> dict:
    log("")
    log(f"########## {title}")
    fx("detach")
    trace_text()
    deep = title.startswith(("EFX, audio service", "SFX + MFX"))                # the full diagnosis for two of the cases
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
        summary["verdict"] = check(result["levels"]) if result.get("levels") else {}
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
            + (f"  (shared memory after {seen} s)" if seen is not None else ""))
        samples = s.get("engine", {}).get("samples") or []
        if samples:
            log("      last report from the effect:", json.dumps(samples[-1]))
    working = [s for s in summaries if passed(s)]
    if not working:
        FAILS.append("no way of attaching the effect worked")
    elif not passed(summaries[0]):
        log("NOTE: the plan (first case) did not work but another did:", "; ".join(s["title"] for s in working))
    protected = [s for s in summaries if not s["protect"]]
    if protected:
        log("Windows loads the effect without switching the audio protection off: " + ("YES" if passed(protected[-1]) else "no"))
    Path(ROOT / "fx_e2e.json").write_text(json.dumps(summaries, indent=1, default=str), encoding="utf-8")
    log("RESULT:", "FAIL " + "; ".join(FAILS) if FAILS else "all good")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
