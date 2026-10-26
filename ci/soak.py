#!/usr/bin/env python3
"""Does the installed program grow? Memory, CPU, handles and threads of every ASM process over time, on real Windows.

Run after ci/chain_install.ps1. Two phases:

1. *With the window open.* Starts the program the way "Start with Windows" does (--background: tray + daemon + audio
   engine + the per-application helper), opens the window as a user would (and checks that it is really on the screen),
   steps through all its pages every few seconds, lets two programs play through the engine, and samples every process
   from outside (psutil) and from inside (the "health:" lines each process writes to its log, which also carry GDI/USER
   objects, handles, Python objects and Qt widgets). At the end it fits a line to each process's memory.
2. *Without the headset, with the window closed.* The engine's output device does not exist (a headset that is off), the
   window is opened and closed, and the tray must replace itself by a fresh one (the idle release) while the daemon and
   the engine keep running; the engine must not keep probing for the missing device.

The CI machine has no headset, so the daemon sits in the state a real machine is in while the headset is off or not found.

    python ci/soak.py [minutes of phase 1]
"""
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soak_report  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
WORK = ROOT / "soak-work"
WORK.mkdir(exist_ok=True)
LOG = open(ROOT / "soak.log", "w", encoding="utf-8")
APP = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Arctis Sound Manager"
EXE = APP / "ArctisSoundManager.exe"
CONFIG_DIR = Path.home() / ".config" / "arctis_manager"
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "arctis_manager" / "logs"
ENDPOINT = "CABLE Input"
MISSING = "No Such Headset 123"
SAMPLE = 10.0
FAILS: list[str] = []

# every process the program starts inherits these: a health line every 30 s, object-type diagnostics, the window cycles
ENV = dict(os.environ, ASM_DIAG="1", ASM_HEALTH_INTERVAL="30", ASM_SOAK_CYCLE_S="5")

user32 = ctypes.windll.user32
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def log(*parts) -> None:
    text = " ".join(str(p) for p in parts)
    LOG.write(text + "\n")
    LOG.flush()
    print(text, flush=True)


def fail(message: str) -> None:
    FAILS.append(message)
    log("FAIL", message)


def write_config(output: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    taps = {"game": "ASM Tap game", "chat": "ASM Tap chat", "media": "ASM Tap media", "aux": "ASM Tap aux"}
    (CONFIG_DIR / "win_audio_engine.json").write_text(json.dumps({
        "latency": "balanced", "inputs": taps, "outputs": {"headset": output}, "mic": {"input": "", "output": ""},
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


# ── processes ────────────────────────────────────────────────────────────────

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
    try:                                                   # what the audio service itself holds (it was 864 MB on a user's PC)
        pid = psutil.win_service_get("Audiosrv").as_dict().get("pid")
        if pid:
            found[pid] = ("audiosrv", psutil.Process(pid))
    except (psutil.Error, OSError, ValueError):
        pass
    return found


def pid_of(role: str) -> int | None:
    pids = [pid for pid, (r, _) in asm_processes().items() if r == role]
    return pids[0] if pids else None


class Sampler:
    """Reads every ASM process from outside every ``SAMPLE`` seconds."""

    def __init__(self):
        self.series: dict[int, dict] = {}
        self.t0 = time.time()

    def run(self, seconds: float, label: str) -> None:
        end = time.time() + seconds
        n = 0
        while time.time() < end:
            now = time.time() - self.t0
            row = []
            for pid, (role, proc) in asm_processes().items():
                rec = self.series.setdefault(pid, {"role": role, "t": [], "rss": [], "uss": [], "cpu": [], "threads": [],
                                                   "handles": []})
                try:
                    with proc.oneshot():
                        full = proc.memory_full_info()
                        rec["t"].append(now)
                        rec["rss"].append(full.rss / 2**20)
                        rec["uss"].append(full.uss / 2**20)
                        rec["cpu"].append(proc.cpu_percent(None))
                        rec["threads"].append(proc.num_threads())
                        rec["handles"].append(proc.num_handles() if role != "audiosrv" else 0)
                    row.append(f"{role}:{rec['uss'][-1]:.0f}MB/{rec['cpu'][-1]:.0f}%")
                except (psutil.Error, OSError):
                    continue
            n += 1
            if n % 3 == 1:
                log(f"   [{label}] t={now:6.0f}s  " + "  ".join(sorted(row)))
            time.sleep(SAMPLE)

    def report(self, title: str) -> dict[str, float]:
        log(f"== {title} (private working set seen from outside; growth fitted over the second half)")
        log(f"{'process':<10}{'pid':>7}{'start MB':>10}{'end MB':>9}{'growth MB/h':>13}{'cpu avg %':>11}{'cpu max %':>11}"
            f"{'threads':>9}{'handles +':>11}")
        cpu_avg: dict[str, float] = {}
        for pid, rec in sorted(self.series.items(), key=lambda kv: kv[1]["role"]):
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
            cpu_avg[rec["role"]] = max(cpu_avg.get(rec["role"], 0.0), float(cpu.mean()))
        return cpu_avg


# ── windows ──────────────────────────────────────────────────────────────────

def top_windows(pids: set[int] | None = None) -> list[dict]:
    """Visible top-level windows (of the given processes), with their title and size."""
    found: list[dict] = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pids is not None and pid.value not in pids:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append({"hwnd": hwnd, "pid": pid.value, "title": buf.value, "w": rect.right - rect.left,
                      "h": rect.bottom - rect.top})
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return found


def asm_window() -> dict | None:
    pids = {pid for pid, (role, _) in asm_processes().items() if role in ("tray", "window")}
    for w in top_windows(pids):
        if w["title"].startswith("Arctis Sound Manager") and w["w"] >= 400 and w["h"] >= 300:
            return w
    return None


def open_window(env: dict) -> bool:
    """Ask the running program for its window the way the Start menu shortcut does (``--show``) and wait for it to appear.

    A bare second launch is the same as ``--background``: it only tells the running tray that it is alive."""
    handoff = subprocess.Popen([str(EXE), "--show"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 90
    while time.time() < deadline:
        w = asm_window()
        if w:
            log(f"   the window is on the screen: pid {w['pid']} '{w['title']}' {w['w']}x{w['h']}"
                f" (the second launch exited with {handoff.poll()})")
            return True
        time.sleep(2)
    log("   NO window appeared. Visible windows of any process:")
    for w in top_windows()[:25]:
        log(f"      pid {w['pid']:>6} {w['w']:>5}x{w['h']:<5} '{w['title']}'")
    return False


# ── the engine, from the outside ─────────────────────────────────────────────

def engine_status() -> dict | None:
    try:
        info = json.loads((CONFIG_DIR / "win_audio_engine.port").read_text(encoding="utf-8"))
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=3) as sock:
            sock.settimeout(3)
            sock.sendall((json.dumps({"token": info["token"], "cmd": "status"}) + "\n").encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        answer = json.loads(data.decode("utf-8"))
        return answer.get("status", answer)
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ── logs ─────────────────────────────────────────────────────────────────────

def log_files() -> list[Path]:
    files: list[Path] = []
    for root in (CONFIG_DIR, LOG_DIR):
        if root.exists():
            files += sorted(root.rglob("*.log"))
    return files


def read_logs() -> dict[Path, str]:
    out = {}
    for path in log_files():
        try:
            out[path] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out


MIN_JUDGED_SPAN_S = 600.0                                   # a process younger than this is still starting up


def judge_health(title: str) -> None:
    texts = read_logs()
    series = soak_report.collect(texts.values())
    log(f"== {title}: what the processes report about themselves (private = committed memory, not shrunk by Windows)")
    if not series:
        fail(f"{title}: no process wrote a 'health:' line")
        return
    log(soak_report.table(series))
    for problem in soak_report.verdict(series, min_span=MIN_JUDGED_SPAN_S):
        fail(f"{title}: {problem}")
    roles = {role for role, _ in series}
    for needed in ("tray", "daemon", "engine"):
        if needed not in roles:
            fail(f"{title}: no health line from the {needed}")
    for path, text in texts.items():
        diag = soak_report.diag_lines(text)
        if diag:
            log(f"--- {path.name}: the last diagnostics ({len(diag)} lines)")
            for line in diag[-12:]:
                log("    " + line[:300])


def dump_logs(tail: int = 12) -> None:
    for path, text in read_logs().items():
        lines = text.splitlines()
        log(f"--- {path} ({len(lines)} lines), last {tail}")
        for line in lines[-tail:]:
            log("    " + line[:260])


def start_program(env: dict) -> None:
    subprocess.Popen([str(EXE), "--background"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def quit_all() -> None:
    subprocess.run([str(EXE), "--quit-all"], capture_output=True, timeout=120)
    time.sleep(5)


# ── phase 1 ──────────────────────────────────────────────────────────────────

def phase_one(minutes: float, cpu_limits: dict) -> None:
    log(f"== phase 1: window open, two programs playing, {minutes:.0f} minutes, one sample every {SAMPLE:.0f} s")
    write_config(ENDPOINT)
    start_program(ENV)
    time.sleep(60)                                          # first start compiles the audio kernels
    if not open_window(ENV):
        fail("phase 1: the window did not open")
    programs = [Fake("fakegame", 0.3, 440, minutes * 60 + 120), Fake("discord", 0.2, 880, minutes * 60 + 120)]
    sampler = Sampler()
    sampler.run(minutes * 60, "1")
    still = asm_window()
    log("   the window is still on the screen at the end:", bool(still))
    if not still:
        fail("phase 1: the window disappeared during the run")
    cpu = sampler.report("phase 1 result")
    for role, limit in cpu_limits.items():
        if cpu.get(role, 0.0) > limit:
            fail(f"phase 1: the {role} used {cpu[role]:.1f}% of a core on average (limit {limit}%)")
    judge_health("phase 1")
    for p in programs:
        p.kill()
    # What a PC costs while nobody is playing anything: every device is open, the cable delivers silence. This is
    # most of a day's time, so it is the number that decides whether ASM is "light".
    time.sleep(8)
    quiet = Sampler()
    quiet.run(120, "1q")
    cpu_quiet = quiet.report("phase 1, nothing playing (devices open, silence)")
    for role, limit in {"engine": 15.0, "helper": 8.0, "tray": 5.0, "daemon": 4.0}.items():
        if cpu_quiet.get(role, 0.0) > limit:
            fail(f"phase 1: the {role} used {cpu_quiet[role]:.1f}% of a core on average while nothing played (limit {limit}%)")
    dump_logs()


# ── phase 2 ──────────────────────────────────────────────────────────────────

def phase_two() -> None:
    log("== phase 2: the headset's output does not exist; the window is opened and closed; the tray must replace itself")
    quit_all()
    for path in log_files():                                # a clean start for the health lines
        try:
            path.unlink()
        except OSError:
            pass
    env = dict(ENV, ASM_TRAY_IDLE_RELEASE_S="60")
    env.pop("ASM_SOAK_CYCLE_S", None)
    write_config(MISSING)
    start_program(env)
    time.sleep(45)
    tray0, daemon0, engine0 = pid_of("tray"), pid_of("daemon"), pid_of("engine")
    log(f"   pids: tray {tray0}, daemon {daemon0}, engine {engine0}")
    first = engine_status()
    log("   engine device_looks at the start:", (first or {}).get("device_looks"))
    if not open_window(env):
        fail("phase 2: the window did not open")
        return
    time.sleep(20)
    w = asm_window()
    if w:
        user32.PostMessageW(w["hwnd"], 0x0010, 0, 0)        # WM_CLOSE: the user closes the window
        time.sleep(3)
        log("   the window after WM_CLOSE:", "still visible" if asm_window() else "closed")
    sampler = Sampler()
    replaced_after = None
    start = time.time()
    while time.time() - start < 240:                        # the idle release looks once a minute
        tray = pid_of("tray")
        if tray is not None and tray != tray0:
            replaced_after = time.time() - start
            break
        time.sleep(5)
    if replaced_after is None:
        fail("phase 2: the tray was not replaced within 4 minutes of closing the window")
    else:
        log(f"   the tray was replaced {replaced_after:.0f} s after the window was closed: pid {tray0} -> {pid_of('tray')}")
        time.sleep(15)
        if tray0 in asm_processes():
            fail("phase 2: the old tray is still running")
        if pid_of("daemon") != daemon0:
            fail("phase 2: the daemon was restarted with the tray")
        if pid_of("engine") != engine0:
            fail("phase 2: the audio engine was restarted with the tray")
        if asm_window():
            fail("phase 2: the replacement tray opened a window by itself")
        for pid, (role, proc) in asm_processes().items():
            if role == "tray":
                try:
                    log(f"   the new tray holds {proc.memory_full_info().uss / 2**20:.0f} MB (private working set)")
                except (psutil.Error, OSError):
                    pass
    looks_before = engine_status()
    sampler.run(150, "2")
    looks_after = engine_status()
    a = ((looks_before or {}).get("device_looks") or {})
    b = ((looks_after or {}).get("device_looks") or {})
    log("   engine device_looks over the last 150 s:", a, "->", b)
    if a and b:
        if b.get("probes", 0) - a.get("probes", 0) > 2:
            fail("phase 2: the engine started more than 2 probe processes in 150 s for a device that is not there")
        if b.get("quick_probes", 0) - a.get("quick_probes", 0) > 12:
            fail("phase 2: the engine looked for the missing device more than 12 times in 150 s")
    else:
        log("   (no device_looks in the engine status: an older engine)")
    cpu = sampler.report("phase 2 result")
    if cpu.get("engine", 0.0) > 8.0:
        fail(f"phase 2: an engine with nothing to play used {cpu['engine']:.1f}% of a core")
    judge_health("phase 2")
    dump_logs()


def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    if not EXE.exists():
        log("FAIL the program is not installed")
        return 1
    try:
        phase_one(minutes, {"tray": 10.0, "daemon": 6.0, "engine": 45.0, "helper": 20.0})
        phase_two()
    finally:
        subprocess.run([str(EXE), "--quit-all"], capture_output=True, timeout=120)
    log(f"== {len(FAILS)} problem(s)" + ("" if not FAILS else ": " + "; ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
