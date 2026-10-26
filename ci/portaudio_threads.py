#!/usr/bin/env python3
"""Which thread may open and start a PortAudio stream on the virtual cable?

A real run showed: the engine's streams start fine when the engine is built on the main thread (at process start) but
every rebuild asked for over the control channel (another thread) answers "Error starting stream ... 9324 ... -9999"
and stays dead. This script isolates the cause. Every scenario runs in a fresh process, so nothing leaks from one into
the next; each prints one line per step. Uses the engine's own backend class, so what works here works in the engine.

    python ci/portaudio_threads.py                 # the driver: runs every scenario, writes threads.log
    python ci/portaudio_threads.py --scenario X    # one scenario (the driver calls this)
"""
from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = None


def log(*a):
    text = " ".join(str(x) for x in a)
    if LOG is not None:
        LOG.write(text + "\n")
        LOG.flush()
    print(text, flush=True)


# ── a scenario's building blocks ────────────────────────────────────────────

def child(name: str) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from arctis_sound_manager.win.audio.backend import SoundDeviceBackend, find_device

    backend = SoundDeviceBackend(latency="low")
    ole32 = ctypes.windll.ole32

    def device(kind, piece):
        found = find_device(backend.devices(), piece, kind)
        if found is None:
            raise RuntimeError(f"no {kind} device {piece!r}")
        return found

    def opened(kind):
        piece = "CABLE Input" if kind == "output" else "CABLE Output"
        opener = backend.open_output if kind == "output" else backend.open_input
        cb = (lambda block: block.fill(0.0)) if kind == "output" else (lambda block: None)
        return opener(device(kind, piece), 2, cb)

    def cycle(kinds=("output",), hold=0.4):
        streams = [opened(k) for k in kinds]
        try:
            for s in streams:
                s.start()
            time.sleep(hold)
        finally:
            for s in streams:
                s.stop()
                s.close()

    def step(label, fn):
        t0 = time.time()
        try:
            fn()
            log(f"  [{name}] {label}: OK ({time.time() - t0:.2f}s)")
            return True
        except Exception as exc:                                    # noqa: BLE001
            log(f"  [{name}] {label}: FAIL {type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:150]}")
            return False

    def on_thread(fn, coinit=None):
        box = {}

        def run():
            try:
                if coinit is not None:
                    box["co"] = ole32.CoInitializeEx(None, coinit)
                fn()
                box["ok"] = True
            except BaseException as exc:                            # noqa: BLE001
                box["err"] = exc
            finally:
                if coinit is not None:
                    ole32.CoUninitialize()

        t = threading.Thread(target=run)
        t.start()
        t.join(60)
        if "err" in box:
            raise box["err"]
        if not box.get("ok"):
            raise RuntimeError("thread did not finish")

    def worker(label, fn, coinit=None):
        return step(label, lambda: on_thread(fn, coinit))

    def repeat(label, fn, n=3):
        for i in range(1, n + 1):
            step(f"{label} #{i}", fn)

    MTA, STA = 0, 2
    if name == "main x3":
        repeat("main", cycle)
    elif name == "main then workers then main":
        step("main", cycle)
        worker("worker", cycle)
        worker("worker again", cycle)
        step("main again", cycle)
    elif name == "workers only (PortAudio initialised on main)":
        for i in range(3):
            worker(f"worker #{i + 1}", cycle)
    elif name == "one persistent worker":
        done = []

        def many():
            for i in range(3):
                try:
                    cycle()
                    log(f"  [{name}] cycle #{i + 1}: OK")
                except Exception as exc:                             # noqa: BLE001
                    log(f"  [{name}] cycle #{i + 1}: FAIL {type(exc).__name__}: {str(exc)[:120]}")
            done.append(1)
        on_thread(many)
    elif name == "workers with CoInitialize MTA":
        for i in range(3):
            worker(f"worker #{i + 1}", cycle, MTA)
    elif name == "workers with CoInitialize STA":
        for i in range(3):
            worker(f"worker #{i + 1}", cycle, STA)
    elif name == "open on main, start on worker":
        holder = {}
        step("open on main", lambda: holder.update(s=opened("output")))
        worker("start on worker", lambda: holder["s"].start())
        time.sleep(0.4)
        step("stop+close on main", lambda: (holder["s"].stop(), holder["s"].close()))
    elif name == "open on worker, start on main":
        holder = {}
        worker("open on worker", lambda: holder.update(s=opened("output")))
        step("start on main", lambda: holder["s"].start())
        time.sleep(0.4)
        step("stop+close on main", lambda: (holder["s"].stop(), holder["s"].close()))
    elif name == "refresh on a worker, then use it":
        worker("refresh + cycle", lambda: (backend.refresh(), cycle()))
        worker("again", cycle)
        step("main", cycle)
    elif name == "capture + render together, main":
        repeat("pair on main", lambda: cycle(("input", "output")))
    elif name == "capture + render together, worker":
        for i in range(3):
            worker(f"pair on worker #{i + 1}", lambda: cycle(("input", "output")))
    elif name == "PortAudio initialised on the worker":
        def all_on_worker():
            from arctis_sound_manager.win.audio.backend import SoundDeviceBackend as B
            b2 = B(latency="low")
            b2.devices()
            for i in range(3):
                try:
                    st = b2.open_output(find_device(b2.devices(), "CABLE Input", "output"), 2, lambda blk: blk.fill(0.0))
                    st.start()
                    time.sleep(0.3)
                    st.stop()
                    st.close()
                    log(f"  [{name}] worker cycle #{i + 1}: OK")
                except Exception as exc:                             # noqa: BLE001
                    log(f"  [{name}] worker cycle #{i + 1}: FAIL {type(exc).__name__}: {str(exc)[:120]}")
        # a fresh process whose main thread never touched PortAudio's devices
        on_thread(all_on_worker)
    elif name == "main thread blocked in a wait, worker streams":
        # the engine's main thread sits in host.wait() (an Event.wait) while the control thread rebuilds
        stop = threading.Event()
        res = []

        def build():
            for i in range(3):
                try:
                    cycle(("input", "output"))
                    res.append("OK")
                except Exception as exc:                             # noqa: BLE001
                    res.append(f"FAIL {str(exc)[:100]}")
            stop.set()
        threading.Thread(target=build, daemon=True).start()
        stop.wait(60)
        log(f"  [{name}] {res}")
    else:
        log("unknown scenario", name)
        return 2
    return 0


SCENARIOS = [
    "main x3",
    "main then workers then main",
    "workers only (PortAudio initialised on main)",
    "one persistent worker",
    "workers with CoInitialize MTA",
    "workers with CoInitialize STA",
    "open on main, start on worker",
    "open on worker, start on main",
    "refresh on a worker, then use it",
    "capture + render together, main",
    "capture + render together, worker",
    "PortAudio initialised on the worker",
    "main thread blocked in a wait, worker streams",
]


def main() -> int:
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=None)
    args = ap.parse_args()
    if args.scenario:
        return child(args.scenario)
    LOG = open("threads.log", "w", encoding="utf-8")
    for name in SCENARIOS:
        log(f"== {name}")
        try:
            done = subprocess.run([sys.executable, __file__, "--scenario", name], capture_output=True, text=True, timeout=120)
            out = (done.stdout + done.stderr).strip()
            log(out)
            log(f"   exit code {done.returncode}")
        except subprocess.TimeoutExpired:
            log("   TIMEOUT")
        time.sleep(1.0)
    log("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
