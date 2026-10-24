# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""``py -m arctis_sound_manager.win.audio.engine_main`` — the audio engine process and its diagnostics.

    (no arguments)      run the engine until Ctrl+C or a ``shutdown`` command
    --list-devices      print the sound devices (add --json for machines)
    --suggest           print a first guess at win_audio_engine.json for this computer (--write to save it)
    --selftest          check the set-up: devices, a cable loopback tone per channel, and a CPU benchmark
    --bench             just the CPU benchmark of the processing graph

Everything is also logged to ``asm-audio-engine.log`` in ASM's config folder.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import sys
import time
from pathlib import Path

import numpy as np

from arctis_sound_manager.win.audio import fast
from arctis_sound_manager.win.audio.backend import (BackendError, SoundDeviceBackend, device_to_dict, find_device)
from arctis_sound_manager.win.audio.control import ControlServer, EngineClient
from arctis_sound_manager.win.audio.engine_config import (EngineConfig, config_path, load_config, resolve,
                                                          save_config, suggest_config)
from arctis_sound_manager.win.audio.graph import CHANNELS, EngineGraph, read_state
from arctis_sound_manager.win.audio.host import EngineHost
from arctis_sound_manager.win.audio.params import default_config_dir
from arctis_sound_manager.win.audio.realtime import TimerResolution

RATE = 48000
LOG_NAME = "asm-audio-engine.log"


def setup_logging(config_dir: Path, verbose: bool = False, console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(config_dir / LOG_NAME, maxBytes=1_000_000, backupCount=3,
                                                       encoding="utf-8")
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError:
        pass
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)


# ── diagnostics ──────────────────────────────────────────────────────────────

def list_devices(backend, as_json: bool, out=print) -> int:
    devices = backend.devices()
    if as_json:
        out(json.dumps([device_to_dict(d) for d in devices]))
        return 0
    for d in sorted(devices, key=lambda d: (not d.wasapi, d.index)):
        flags = ("  default-in" if d.default_input else "") + ("  default-out" if d.default_output else "")
        out(d.label() + flags)
    return 0


def benchmark(config_dir: Path, block: int = 960, seconds: float = 3.0, hrir_id: str | None = None) -> dict:
    """Time the real processing graph on synthetic 8-channel audio, with every channel and the microphone busy.

    Returns milliseconds per block and the share of the real-time budget one core would spend. A single
    Python thread does all the processing, so the number to watch is ``load``: below ~0.5 is comfortable,
    above 1.0 the sound would crackle.
    """
    warmup = fast.warmup()
    graph = EngineGraph(RATE, block)
    snapshot = read_state(config_dir, hrir_id, RATE)
    graph.apply_playback(snapshot)
    graph.apply_mic(snapshot)
    rng = np.random.default_rng(1)
    n_blocks = max(8, int(seconds * RATE / block))
    play_times, mic_times = [], []
    mic_block = 480
    frames = {ch: rng.standard_normal((8 if ch != "chat" else 2, block)) * 0.05 for ch in CHANNELS}
    mic = rng.standard_normal(mic_block) * 0.05
    for _ in range(5):                                                   # warm up caches, FFT plans
        graph.process(frames)
        graph.process_mic(mic)
    for _ in range(n_blocks):
        t = time.perf_counter()
        graph.process(frames)
        play_times.append(time.perf_counter() - t)
    for _ in range(int(n_blocks * block / mic_block)):
        t = time.perf_counter()
        graph.process_mic(mic)
        mic_times.append(time.perf_counter() - t)
    budget = block / RATE
    mic_budget = mic_block / RATE
    play = np.array(play_times)
    micro = np.array(mic_times)
    return {
        "block": block,
        "compiled": fast.HAVE_NUMBA,
        "warmup_s": round(warmup, 2),
        "playback_ms_mean": round(float(play.mean() * 1000), 3),
        "playback_ms_p99": round(float(np.percentile(play, 99) * 1000), 3),
        "playback_load": round(float(play.mean() / budget), 3),
        "playback_worst_load": round(float(play.max() / budget), 3),
        "mic_ms_mean": round(float(micro.mean() * 1000), 3),
        "mic_load": round(float(micro.mean() / mic_budget), 3),
        "spatial": [ch for ch, s in graph.strips.items() if s.spatial_active],
        "noise_engine": graph.mic.noise_engine_active,
    }


def check_cable(backend, capture, render, seconds: float = 1.5, pump=None, sleep=time.sleep, freq: float = 1000.0) -> dict:
    """Play a tone into a cable's render end and record it from the capture end: does the cable work, how
    loud does it come back, and how far is its clock from the system's (ppm)."""
    state = {"phase": 0, "frames": 0, "recorded": []}

    def play(out):
        n = out.shape[0]
        t = (np.arange(n) + state["phase"]) / RATE
        out[:] = (0.25 * np.sin(2 * np.pi * freq * t))[:, None]
        state["phase"] += n

    def record(data):
        state["recorded"].append(np.array(data[:, 0], dtype=np.float64))

    out_stream = backend.open_output(render, 2, play)
    in_stream = backend.open_input(capture, 2, record)
    started = time.perf_counter()
    try:
        in_stream.start()
        out_stream.start()
        while time.perf_counter() - started < seconds:
            if pump is not None:
                pump()
            sleep(0.02)
    finally:
        out_stream.stop(); in_stream.stop(); out_stream.close(); in_stream.close()
    elapsed = time.perf_counter() - started
    rec = np.concatenate(state["recorded"]) if state["recorded"] else np.zeros(0)
    if rec.size < RATE // 4:
        return {"ok": False, "error": "nothing came back through the cable", "frames": int(rec.size)}
    body = rec[rec.size // 4:]
    level = float(np.sqrt(np.mean(body ** 2)))
    spectrum = np.abs(np.fft.rfft(body * np.hanning(body.size)))
    peak_hz = float(np.argmax(spectrum) * RATE / body.size)
    return {"ok": level > 0.05, "level": round(level, 3), "expected": round(0.25 / np.sqrt(2), 3),
            "tone_hz": round(peak_hz, 1), "frames": int(rec.size), "seconds": round(elapsed, 2),
            "rate_ratio": round(rec.size / (elapsed * RATE), 4)}


def selftest(config_dir: Path, backend=None, out=print, seconds: float = 1.5, pump=None) -> int:
    cfg = load_config(config_dir)
    failures = 0
    out("== 1. Configuration")
    problems = cfg.problems()
    if problems:
        for p in problems:
            out(f"   PROBLEM: {p}")
        out("   (py -m arctis_sound_manager.win.audio.engine_main --suggest  prints a first guess)")
        failures += 1
    else:
        out(f"   latency profile '{cfg.latency}': block {cfg.playback_block}, about {cfg.profile.approx_latency_ms():.0f} ms")
    out("== 2. Devices")
    try:
        backend = backend or SoundDeviceBackend(latency=cfg.device_latency)
        devices = backend.devices()
    except BackendError as exc:
        out(f"   FAILED: {exc}")
        devices = []
        failures += 1
    resolved = resolve(cfg, devices)
    for ch, d in resolved.inputs.items():
        out(f"   {ch:6s} <- {d.label()}")
    for bus, d in resolved.outputs.items():
        out(f"   {bus:6s} -> {d.label()}")
    if resolved.mic_input:
        out(f"   mic    <- {resolved.mic_input.label()}")
    if resolved.mic_output:
        out(f"   mic    -> {resolved.mic_output.label()}")
    for line in resolved.missing:
        out(f"   MISSING: {line}")
        failures += 1
    out("== 3. Cable loopback (a quiet tone goes into each cable and must come back out)")
    for ch, capture in resolved.inputs.items():
        render = _render_end_of(capture, devices)
        if render is None:
            out(f"   {ch}: no matching playback end found for '{capture.name}' (skipped)")
            continue
        try:
            r = check_cable(backend, capture, render, seconds=seconds, pump=pump)
        except BackendError as exc:
            out(f"   {ch}: FAILED to open: {exc}")
            failures += 1
            continue
        verdict = "ok" if r["ok"] else "FAILED"
        out(f"   {ch}: {verdict} {r}")
        failures += 0 if r["ok"] else 1
    out("== 4. Processing speed (the same graph the engine runs, every channel and the microphone busy)")
    b = benchmark(config_dir, cfg.playback_block, seconds=2.0)
    out(f"   playback: {b['playback_ms_mean']} ms per {cfg.playback_block}-frame block "
        f"(budget {cfg.playback_block / 48:.1f} ms) = {b['playback_load'] * 100:.0f}% of one core, worst {b['playback_worst_load'] * 100:.0f}%")
    out(f"   microphone: {b['mic_ms_mean']} ms per 480-frame block = {b['mic_load'] * 100:.0f}% of one core "
        f"(noise cancelling: {b['noise_engine'] or 'off'})")
    if b["playback_load"] > 0.7:
        out("   WARNING: this computer is slow for this profile; use latency 'safe' in win_audio_engine.json")
        failures += 1
    out("== Result: " + ("everything checked out" if failures == 0 else f"{failures} thing(s) need attention"))
    return 1 if failures else 0


def _render_end_of(capture, devices):
    """The playback end of the same virtual cable: 'CABLE-A Output (...)' <-> 'CABLE-A Input (...)'."""
    from arctis_sound_manager.win.audio.engine_config import _cable_base
    base = _cable_base(capture.name)
    candidates = [d for d in devices if d.max_outputs > 0 and _cable_base(d.name) == base and d.wasapi == capture.wasapi]
    return candidates[0] if candidates else None


# ── entry point ──────────────────────────────────────────────────────────────

def run_engine(config_dir: Path, verbose: bool = False, force: bool = False) -> int:
    client = EngineClient(config_dir, timeout=1.0)
    if not force and client.available():
        print("The audio engine is already running.")
        return 0
    setup_logging(config_dir, verbose)
    log = logging.getLogger("engine")
    host = EngineHost(config_dir)
    server = ControlServer(host.handle, config_dir)
    server.start()
    fast.warmup_async()                                                  # compile the kernels while the GUI is still talking to us
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda *_: host.shutdown_requested.set())
            except (ValueError, OSError):
                pass
    log.info("ASM audio engine starting (config folder %s)", config_dir)
    try:
        with TimerResolution(1):
            host.start()
            host.wait()
    finally:
        log.info("ASM audio engine stopping")
        host.stop()
        server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engine_main", description=__doc__.split("\n")[0])
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--suggest", action="store_true")
    ap.add_argument("--write", action="store_true", help="with --suggest: save the guess if there is no configuration yet")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--block", type=int, default=None, help="block size for --bench")
    ap.add_argument("--force", action="store_true", help="start even if an engine answers on the control port")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    config_dir = args.config_dir or default_config_dir()

    if args.bench:
        cfg = load_config(config_dir)
        print(json.dumps(benchmark(config_dir, args.block or cfg.playback_block), indent=2))
        return 0
    if args.list_devices or args.suggest or args.selftest:
        logging.basicConfig(level=logging.WARNING)
    if args.list_devices:
        try:
            return list_devices(SoundDeviceBackend(), args.json)
        except BackendError as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.suggest:
        try:
            devices = SoundDeviceBackend().devices()
        except BackendError as exc:
            print(exc, file=sys.stderr)
            return 2
        cfg = suggest_config(devices)
        print(json.dumps(cfg.to_dict(), indent=2))
        problems = cfg.problems()
        for p in problems:
            print(f"# still missing: {p}", file=sys.stderr)
        if args.write:
            if config_path(config_dir).exists():
                print("# not writing: a configuration already exists", file=sys.stderr)
            else:
                print(f"# saved to {save_config(cfg, config_dir)}", file=sys.stderr)
        return 0
    if args.selftest:
        return selftest(config_dir)
    return run_engine(config_dir, args.verbose, args.force)


if __name__ == "__main__":
    sys.exit(main())
