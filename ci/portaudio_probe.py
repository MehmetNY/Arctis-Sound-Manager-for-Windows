#!/usr/bin/env python3
"""Why would PortAudio refuse to open a render device on this machine? Lists everything and tries every way of opening."""
import sys
import time
import traceback

import numpy as np
import sounddevice as sd

OUT = open("portaudio.log", "w", encoding="utf-8")


def log(*a):
    text = " ".join(str(x) for x in a)
    OUT.write(text + "\n")
    OUT.flush()
    print(text, flush=True)


log("sounddevice", sd.__version__, "| portaudio", sd.get_portaudio_version())
hostapis = sd.query_hostapis()
for i, h in enumerate(hostapis):
    log(f"HOSTAPI {i}: {h['name']} devices={h['devices']} default_in={h['default_input_device']} default_out={h['default_output_device']}")
devices = sd.query_devices()
for i, d in enumerate(devices):
    log(f"DEVICE {i}: {d['name']!r} api={hostapis[d['hostapi']]['name']} in={d['max_input_channels']} out={d['max_output_channels']} "
        f"rate={d['default_samplerate']} low_out={d['default_low_output_latency']:.4f} high_out={d['default_high_output_latency']:.4f}")

try:
    wasapi_info = sd.WasapiSettings
    log("WasapiSettings available")
except AttributeError:
    log("no WasapiSettings")


def attempt(kind, index, **kw):
    label = f"{kind} dev={index} " + " ".join(f"{k}={v!r}" for k, v in kw.items() if k != "callback")
    try:
        cls = sd.OutputStream if kind == "output" else sd.InputStream
        cb = (lambda outdata, frames, t, s: outdata.fill(0)) if kind == "output" else (lambda indata, frames, t, s: None)
        stream = cls(device=index, callback=cb, dtype="float32", **kw)
        stream.start()
        time.sleep(0.3)
        info = f"rate={stream.samplerate} ch={stream.channels} latency={stream.latency}"
        stream.stop()
        stream.close()
        log("  OK   ", label, "->", info)
        return True
    except Exception as exc:                                       # noqa: BLE001
        log("  FAIL ", label, "->", type(exc).__name__, str(exc).replace("\n", " ")[:200])
        return False


for i, d in enumerate(devices):
    api = hostapis[d["hostapi"]]["name"]
    if "cable" not in d["name"].lower() and "speaker" not in d["name"].lower():
        continue
    for kind, n in (("output", d["max_output_channels"]), ("input", d["max_input_channels"])):
        if n <= 0:
            continue
        log(f"--- {kind} {i} {d['name']!r} ({api}, max {n} ch)")
        for channels in sorted({2, 1, n}):
            if channels > n:
                continue
            for rate in (48000, int(d["default_samplerate"])):
                attempt(kind, i, samplerate=rate, channels=channels)
                attempt(kind, i, samplerate=rate, channels=channels, latency="low")
                attempt(kind, i, samplerate=rate, channels=channels, latency="low", blocksize=0)
                if "WASAPI" in api:
                    attempt(kind, i, samplerate=rate, channels=channels, latency="low",
                            extra_settings=sd.WasapiSettings(exclusive=False, auto_convert=True))
                    attempt(kind, i, samplerate=rate, channels=channels, latency="low",
                            extra_settings=sd.WasapiSettings(exclusive=False))

log("== the same after PortAudio is re-initialised (what the engine does when a device appears)")
try:
    sd._terminate()
    sd._initialize()
    devices2 = sd.query_devices()
    log("devices after re-init:", len(devices2), "(before:", len(devices), ")")
    for i, d in enumerate(devices2):
        if "cable" in d["name"].lower():
            log(f"DEVICE {i}: {d['name']!r} api={sd.query_hostapis()[d['hostapi']]['name']} in={d['max_input_channels']} out={d['max_output_channels']}")
            if d["max_output_channels"] > 0:
                attempt("output", i, samplerate=48000, channels=2, latency="low",
                        extra_settings=sd.WasapiSettings(exclusive=False, auto_convert=True) if "WASAPI" in sd.query_hostapis()[d["hostapi"]]["name"] else None)
except Exception:                                                   # noqa: BLE001
    log(traceback.format_exc())
log("DONE")
