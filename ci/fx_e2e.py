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

It is run twice: with Windows' audio protection left alone, and with it switched off the way the product does, to find
out whether Windows loads the effect at all without that.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
LOG = open(ROOT / "fx_e2e.log", "w", encoding="utf-8")
DLL = ROOT / "build" / "asm_micfx.dll"
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


def sh(cmd: list[str], timeout: float = 120.0, env: dict | None = None) -> str:
    log("$", " ".join(cmd))
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})})
    except Exception as exc:                                              # noqa: BLE001
        log("   failed:", repr(exc))
        return ""
    out = ((done.stdout or "") + (done.stderr or "")).strip()
    for line in out.splitlines()[:60]:
        log("   " + line)
    return out


def fx(*args: str) -> str:
    code = "import sys; from arctis_sound_manager.win.fx import cli; sys.exit(cli.main(sys.argv[1:]))"
    return sh([sys.executable, "-c", code, *args], env={"PYTHONPATH": str(ROOT / "src"), "ASM_MICFX_DLL": str(DLL)})


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


def timeline() -> dict:
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
    engine.ctl["stop"] = True
    engine.join(3)
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


def case(title: str, protect: bool) -> dict:
    log("")
    log(f"########## {title}")
    fx("detach")
    out = fx("attach", "--match", "CABLE Output", *(() if protect else ("--no-protect",)))
    fx("status", "--match", "CABLE Output")
    time.sleep(3)
    summary: dict = {"title": title, "protect": protect}
    try:
        result = timeline()
        summary.update(result)
        summary["verdict"] = check(result["levels"])
    except Exception as exc:                                              # noqa: BLE001
        log("   timeline failed:", repr(exc))
        summary["error"] = repr(exc)
    sh(["tasklist", "/m", "asm_micfx.dll"])
    sh(["powershell", "-NoProfile", "-Command",
        "Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddMinutes(-10)} -ErrorAction SilentlyContinue "
        "| Where-Object { $_.Message -match 'audiodg|asm_micfx' } | Select-Object -First 8 | Format-List TimeCreated, Id, Message"])
    fx("detach")
    return summary


def main() -> int:
    if not DLL.is_file():
        log("build\\asm_micfx.dll is missing")
        return 1
    sh(["powershell", "-NoProfile", "-Command",
        "Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue | Format-Table FriendlyName, Status, InstanceId -AutoSize | Out-String -Width 250"])
    summaries = [case("audio protection switched off (what the product does)", True),
                 case("audio protection left alone", False)]
    log("")
    log("========== SUMMARY")
    for s in summaries:
        verdict = s.get("verdict") or {}
        log(f"{s['title']}: " + ("; ".join(f"{k} {v['level']} (want {v['expected']}) {'ok' if v['ok'] else 'WRONG'}" for k, v in verdict.items())
                                or s.get("error", "no result")))
        seen = s.get("engine", {}).get("mapping_seen_s")
        log(f"   the effect's shared memory appeared after {seen} s" if seen is not None else "   the effect's shared memory never appeared")
        samples = s.get("engine", {}).get("samples") or []
        if samples:
            log("   last report from the effect:", json.dumps(samples[-1]))
    product = summaries[0].get("verdict") or {}
    if not product or not all(v["ok"] for v in product.values()):
        FAILS.append("with the product's settings the effect did not behave")
    protected = summaries[1].get("verdict") or {}
    log("Windows loads the effect without switching the audio protection off: "
        + ("YES" if protected and all(v["ok"] for v in protected.values()) else "no"))
    Path(ROOT / "fx_e2e.json").write_text(json.dumps(summaries, indent=1, default=str), encoding="utf-8")
    log("RESULT:", "FAIL " + "; ".join(FAILS) if FAILS else "all good")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
