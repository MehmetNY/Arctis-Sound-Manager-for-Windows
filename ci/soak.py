#!/usr/bin/env python3
"""Does the installed program grow? Memory, CPU, handles and threads of every ASM process over time, on real Windows.

Run after ci/chain_install.ps1. Starts the program the way "Start with Windows" does (--background: tray + daemon + audio
engine + the per-application helper), opens the window as a user would, lets two programs play through the engine, and
samples every process every SAMPLE seconds. At the end it fits a line to each process's memory and says how many MB per
hour it grows, and what share of a core it uses. The CI machine has no headset, so the daemon sits in the state a real
machine is in while the headset is off or not found: it keeps looking for it.

    python ci/soak.py [minutes]
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import psutil

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
WORK = ROOT / "soak-work"
WORK.mkdir(exist_ok=True)
LOG = open(ROOT / "soak.log", "w", encoding="utf-8")
APP = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Arctis Sound Manager"
EXE = APP / "ArctisSoundManager.exe"
CONFIG_DIR = Path.home() / ".config" / "arctis_manager"
ENDPOINT = "CABLE Input"
SAMPLE = 10.0
FAILS: list[str] = []


def log(*parts) -> None:
    text = " ".join(str(p) for p in parts)
    LOG.write(text + "\n")
    LOG.flush()
    print(text, flush=True)


def write_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    taps = {"game": "ASM Tap game", "chat": "ASM Tap chat", "media": "ASM Tap media", "aux": "ASM Tap aux"}
    (CONFIG_DIR / "win_audio_engine.json").write_text(json.dumps({
        "latency": "balanced", "inputs": taps, "outputs": {"headset": ENDPOINT}, "mic": {"input": "", "output": ""},
        "master_limiter": True}, indent=2), encoding="utf-8")


class Fake:
    def __init__(self, name: str, amp: float, freq: float, secs: float):
        self.exe = WORK / f"{name}.exe"
        shutil.copy(BUILD / "audioprobe.exe", self.exe)
        self.proc = subprocess.Popen([str(self.exe), "tone", "--device", ENDPOINT, "--amp", str(amp), "--freq", str(freq),
                                      "--secs", str(secs)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.proc.stdout.readline()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


def role_of(proc: psutil.Process) -> str:
    try:
        name = proc.name().lower()
        cmd = " ".join(proc.cmdline())
    except (psutil.Error, OSError):
        return ""
    if name == "asm-audiotap.exe":
        return "helper"
    if name != "arctissoundmanager.exe":
        return ""
    for flag, role in (("--engine", "engine"), ("--daemon", "daemon"), ("--background", "tray")):
        if flag in cmd:
            return role
    return "window"


def asm_processes() -> dict[int, tuple[str, psutil.Process]]:
    found = {}
    for p in psutil.process_iter():
        role = role_of(p)
        if role:
            found[p.pid] = (role, p)
    return found


def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    log(f"== soak: {minutes:.0f} minutes, one sample every {SAMPLE:.0f} s")
    if not EXE.exists():
        log("FAIL the program is not installed")
        return 1
    write_config()
    subprocess.Popen([str(EXE), "--background"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(60)                                        # first start compiles the audio kernels
    window = subprocess.Popen([str(EXE)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(20)
    log("   the window process exit code (None = still open):", window.poll())
    programs = [Fake("fakegame", 0.3, 440, minutes * 60 + 120), Fake("discord", 0.2, 880, minutes * 60 + 120)]

    series: dict[int, dict] = {}
    t0 = time.time()
    end = t0 + minutes * 60
    n = 0
    while time.time() < end:
        now = time.time() - t0
        seen = asm_processes()
        row = []
        for pid, (role, proc) in seen.items():
            rec = series.setdefault(pid, {"role": role, "t": [], "rss": [], "uss": [], "cpu": [], "threads": [], "handles": [],
                                          "proc": proc})
            try:
                with proc.oneshot():
                    full = proc.memory_full_info()
                    rec["t"].append(now)
                    rec["rss"].append(full.rss / 2**20)
                    rec["uss"].append(full.uss / 2**20)
                    rec["cpu"].append(proc.cpu_percent(None))
                    rec["threads"].append(proc.num_threads())
                    rec["handles"].append(proc.num_handles())
                row.append(f"{role}:{rec['uss'][-1]:.0f}MB/{rec['cpu'][-1]:.0f}%")
            except (psutil.Error, OSError):
                continue
        n += 1
        if n % 3 == 1:
            log(f"   t={now:6.0f}s  " + "  ".join(sorted(row)))
        time.sleep(SAMPLE)

    log("== result (private memory = what the process itself holds; growth fitted over the second half of the run)")
    log(f"{'process':<10}{'pid':>7}{'start MB':>10}{'end MB':>9}{'growth MB/h':>13}{'cpu avg %':>11}{'cpu max %':>11}"
        f"{'threads':>9}{'handles +':>11}")
    worst = 0.0
    for pid, rec in sorted(series.items(), key=lambda kv: kv[1]["role"]):
        if len(rec["t"]) < 8:
            continue
        t = np.array(rec["t"])
        uss = np.array(rec["uss"])
        half = len(t) // 2
        slope = float(np.polyfit(t[half:], uss[half:], 1)[0]) * 3600
        cpu = np.array(rec["cpu"][1:] or [0.0])
        handles = rec["handles"][-1] - rec["handles"][0]
        log(f"{rec['role']:<10}{pid:>7}{uss[0]:>10.0f}{uss[-1]:>9.0f}{slope:>13.0f}{cpu.mean():>11.1f}{cpu.max():>11.0f}"
            f"{rec['threads'][-1]:>9}{handles:>11}")
        worst = max(worst, slope)
    log(f"worst growth: {worst:.0f} MB per hour")
    for root in (CONFIG_DIR, Path(os.environ.get("LOCALAPPDATA", "")) / "arctis_manager"):
        for path in sorted(root.rglob("*.log")) if root.exists() else []:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            log(f"--- {path} ({len(lines)} lines), last 15")
            for line in lines[-15:]:
                log("    " + line[:260])
    for p in programs:
        p.kill()
    subprocess.run([str(EXE), "--quit-all"], capture_output=True, timeout=120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
