# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""What an ASM process costs, written to its own log, and a guard against memory that grows without bound.

ASM runs as several processes (the tray with the window, the daemon, the audio engine, the helper). When one of them
holds more and more memory over hours, "it grows" is not enough to find the cause. Each process therefore writes one
line to its log every few minutes::

    health: engine pid 8264 private 47.2 MB (peak ws 120.5 MB) handles 212 gdi 0 user 0 threads 17 python-objects 48211 cpu 11.4% up 2h05m

and a warning when the growth is steady (``health: private memory grows by about 180 MB/h``). With ``ASM_DIAG=1`` in the
environment the line is followed by the Python object types that grew since the first sample; with ``ASM_DIAG=2`` also by
the source lines that allocated the memory (tracemalloc; slower). ``ASM_HEALTH_INTERVAL`` sets the seconds between
samples (default 300).

A process that does reach a hard limit (:class:`HealthMonitor` ``limit_mb``) calls ``on_limit`` once; the tray uses it to
restart itself when its window is closed, so a leak that is still unknown costs a restart and not the whole PC's memory.
"""
from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from collections import Counter, deque
from typing import Callable

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300.0
GROWTH_WINDOW_S = 1800.0                 # growth is judged over (at least) this long ...
GROWTH_MIN_SAMPLES = 6
GROWTH_WARN_MB_PER_H = 150.0             # ... and reported above this slope ...
GROWTH_WARN_MIN_MB = 250.0               # ... once the process is at least this big
GROWTH_WARN_EVERY_S = 1800.0
LIMIT_SAMPLES = 2                        # samples in a row over the limit before on_limit is called
HOT_THREADS_FROM_PCT = 3.0               # name the busiest threads in the line once the process uses this much of a core


# ── measuring ────────────────────────────────────────────────────────────────

def _windows_stats(pid: int | None) -> dict:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    user.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user.GetGuiResources.restype = wintypes.DWORD

    own = pid is None or pid == os.getpid()
    handle = kernel.GetCurrentProcess() if own else kernel.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return {}
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        out: dict = {}
        if kernel.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            out["private_mb"] = counters.PrivateUsage / 2**20
            out["ws_mb"] = counters.WorkingSetSize / 2**20
            out["peak_ws_mb"] = counters.PeakWorkingSetSize / 2**20
        count = wintypes.DWORD()
        if kernel.GetProcessHandleCount(handle, ctypes.byref(count)):
            out["handles"] = int(count.value)
        out["gdi"] = int(user.GetGuiResources(handle, 0))
        out["user"] = int(user.GetGuiResources(handle, 1))
        return out
    finally:
        if not own:
            kernel.CloseHandle(handle)


def _linux_stats() -> dict:
    out: dict = {}
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("VmRSS", "VmHWM", "VmData"):
                    mb = int(rest.split()[0]) / 1024
                    out[{"VmRSS": "ws_mb", "VmHWM": "peak_ws_mb", "VmData": "private_mb"}[key]] = mb
        out["handles"] = len(os.listdir("/proc/self/fd"))
    except (OSError, ValueError):
        pass
    return out


def _windows_thread_cpu() -> dict[int, float]:
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                    ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def seconds(ft) -> float:
        return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

    out: dict[int, float] = {}
    snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)               # TH32CS_SNAPTHREAD
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return out
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(ThreadEntry)
        own = os.getpid()
        ok = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == own:
                handle = kernel.OpenThread(0x0800, False, entry.th32ThreadID)   # THREAD_QUERY_LIMITED_INFORMATION
                if handle:
                    created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
                    if kernel.GetThreadTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                             ctypes.byref(kernel_time), ctypes.byref(user_time)):
                        out[int(entry.th32ThreadID)] = seconds(kernel_time) + seconds(user_time)
                    kernel.CloseHandle(handle)
            entry.dwSize = ctypes.sizeof(ThreadEntry)
            ok = kernel.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return out


def _linux_thread_cpu() -> dict[int, float]:
    out: dict[int, float] = {}
    try:
        ticks = os.sysconf("SC_CLK_TCK")
        for name in os.listdir("/proc/self/task"):
            try:
                with open(f"/proc/self/task/{name}/stat", encoding="ascii", errors="replace") as fh:
                    fields = fh.read().rsplit(")", 1)[1].split()
                out[int(name)] = (int(fields[11]) + int(fields[12])) / ticks      # utime + stime
            except (OSError, ValueError, IndexError):
                continue
    except (OSError, ValueError, AttributeError):
        pass
    return out


def thread_cpu_seconds() -> dict[int, float]:
    """CPU seconds (user + kernel) used so far by every thread of this process, by native thread id. Never raises."""
    try:
        return _windows_thread_cpu() if sys.platform == "win32" else _linux_thread_cpu()
    except Exception:                                           # noqa: BLE001 — a measurement must never hurt the process
        _log.debug("could not read the thread times", exc_info=True)
        return {}


def thread_names() -> dict[int, str]:
    """Native thread id -> the name of the Python thread (threads Python does not know - PortAudio's, COM's, Qt's - are absent)."""
    return {t.native_id: t.name for t in threading.enumerate() if getattr(t, "native_id", None)}


def busiest_threads(before: dict[int, float], after: dict[int, float], elapsed: float, names: dict[int, str],
                    top: int = 3, minimum_pct: float = 1.0) -> list[tuple[str, float]]:
    """``[(thread name, percent of one core)]`` over *elapsed* seconds, busiest first. Threads Python did not start are
    called ``native-<id>``; a thread that did not exist at the first reading is counted from zero."""
    if elapsed <= 0:
        return []
    rows = []
    for tid, now in after.items():
        pct = (now - before.get(tid, 0.0)) / elapsed * 100.0
        if pct >= minimum_pct:
            rows.append((names.get(tid) or f"native-{tid}", pct))
    rows.sort(key=lambda row: -row[1])
    return rows[:top]


def process_stats(pid: int | None = None) -> dict:
    """Memory (``private_mb`` = committed private bytes, ``ws_mb``, ``peak_ws_mb``), ``handles``, and on Windows
    ``gdi`` / ``user`` objects, of this process or (Windows) of *pid*. Missing values are left out; never raises."""
    try:
        if sys.platform == "win32":
            stats = _windows_stats(pid)
        elif pid is None or pid == os.getpid():
            stats = _linux_stats()
        else:
            stats = {}
    except Exception:                                           # noqa: BLE001 — a measurement must never hurt the process
        _log.debug("could not read the process statistics", exc_info=True)
        stats = {}
    if pid is None or pid == os.getpid():
        stats["threads"] = threading.active_count()
    return stats


def diag_from_file() -> str:
    """``1`` or ``2`` when ``%LOCALAPPDATA%\\arctis_manager\\diag.txt`` starts with it (the installed program has no way to
    set an environment variable for itself); empty otherwise."""
    base = os.environ.get("LOCALAPPDATA")
    path = os.path.join(base, "arctis_manager", "diag.txt") if base else os.path.expanduser("~/.config/arctis_manager/diag.txt")
    try:
        with open(path, encoding="ascii", errors="ignore") as fh:
            first = fh.read(8).strip()[:1]
    except OSError:
        return ""
    return first if first in ("1", "2") else ""


def python_objects() -> int:
    """How many memory blocks Python has allocated (the number that grows when objects pile up).

    Counting ``len(gc.get_objects())`` instead builds a list of every tracked object with the interpreter lock held,
    which stalls the audio callbacks of the engine for a few milliseconds each time; this is O(1)."""
    try:
        return sys.getallocatedblocks()
    except AttributeError:                                  # not CPython
        return len(gc.get_objects())


def type_counts() -> Counter:
    return Counter(type(obj).__name__ for obj in gc.get_objects())


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m" if hours else f"{rest // 60}m{rest % 60:02d}s"


def slope_mb_per_hour(samples) -> float:
    """Least-squares slope of ``(seconds, MB)`` samples, in MB per hour (0 when there are too few)."""
    points = list(samples)
    if len(points) < 2:
        return 0.0
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var <= 0:
        return 0.0
    return sum((t - mean_t) * (v - mean_v) for t, v in points) / var * 3600.0


# ── the monitor ──────────────────────────────────────────────────────────────

class HealthMonitor:
    """A background thread that samples the process every *interval* seconds and logs it."""

    def __init__(self, role: str, logger: logging.Logger | None = None, interval: float | None = None,
                 limit_mb: float | None = None, on_limit: Callable[[dict], None] | None = None,
                 extra: Callable[[], dict] | None = None, stats: Callable[[], dict] = process_stats,
                 clock: Callable[[], float] = time.monotonic, diag: str | None = None,
                 thread_cpu: Callable[[], dict] = thread_cpu_seconds, names: Callable[[], dict] = thread_names):
        self.role = role
        self._log = logger or _log
        if self._log.getEffectiveLevel() > logging.INFO:         # the lines are the point: never lose them to a quiet root
            self._log.setLevel(logging.INFO)
        env_interval = os.environ.get("ASM_HEALTH_INTERVAL")
        try:
            self.interval = float(interval if interval is not None else (env_interval or DEFAULT_INTERVAL))
        except ValueError:
            self.interval = DEFAULT_INTERVAL
        self.interval = max(1.0, self.interval)
        self.limit_mb = limit_mb
        self._on_limit = on_limit
        self._extra = extra
        self._stats = stats
        self._clock = clock
        self.diag = (os.environ.get("ASM_DIAG") or diag_from_file()) if diag is None else diag
        self._started = clock()
        self._cpu0 = (time.process_time(), self._started)
        self._thread_cpu = thread_cpu
        self._names = names
        self._threads0 = (self._thread_cpu(), self._started)
        self._history: deque = deque(maxlen=max(GROWTH_MIN_SAMPLES * 2, int(GROWTH_WINDOW_S / self.interval) + 2))
        self._over = 0
        self._limit_called_at = -1e18
        self._warned_at = -1e18
        self._types0: Counter | None = None
        self._types_last: Counter | None = None
        self._trace0 = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples = 0
        self.last: dict = {}

    # ── thread ──────────────────────────────────────────────────────────────

    def start(self) -> "HealthMonitor":
        if self._thread is not None:
            return self
        if self.diag == "2":
            import tracemalloc
            if not tracemalloc.is_tracing():
                tracemalloc.start(10)
        self._thread = threading.Thread(target=self._run, name=f"asm-health-{self.role}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            from arctis_sound_manager.win import buildinfo
            self._log.info("health: %s pid %d runs build %s", self.role, os.getpid(), buildinfo.build_id())
        except Exception:                                       # noqa: BLE001
            pass
        try:
            self.sample()                                       # a first line at start-up: the baseline
        except Exception:                                       # noqa: BLE001
            self._log.debug("health sample failed", exc_info=True)
        while not self._stop.wait(self.interval):
            try:
                self.sample()
            except Exception:                                   # noqa: BLE001 — never take the process down
                self._log.debug("health sample failed", exc_info=True)

    # ── one sample ──────────────────────────────────────────────────────────

    def sample(self) -> dict:
        now = self._clock()
        stats = dict(self._stats())
        cpu_now = time.process_time()
        cpu0, t0 = self._cpu0
        stats["cpu_pct"] = (cpu_now - cpu0) / max(1e-6, now - t0) * 100.0 if now > t0 else 0.0
        self._cpu0 = (cpu_now, now)
        threads_now = self._thread_cpu()
        before, t_before = self._threads0
        self._threads0 = (threads_now, now)
        if stats["cpu_pct"] >= HOT_THREADS_FROM_PCT or self.diag:
            hot = busiest_threads(before, threads_now, now - t_before, self._names())
            if hot:
                stats["hot_threads"] = ", ".join(f"{name} {pct:.0f}%" for name, pct in hot)
        stats["objects"] = python_objects()
        stats["uptime_s"] = now - self._started
        extra: dict = {}
        if self._extra is not None:
            try:
                extra = dict(self._extra() or {})
            except Exception:                                   # noqa: BLE001
                self._log.debug("health extra failed", exc_info=True)
        self.samples += 1
        self.last = {**stats, **extra}
        self._log.info("health: %s", self.describe(stats, extra))
        private = stats.get("private_mb")
        if private is not None:
            self._history.append((now, private))
            self._check_growth(now, private)
            self._check_limit(now, private, stats)
        if self.diag:
            self._log_diag()
        return self.last

    def describe(self, stats: dict, extra: dict | None = None) -> str:
        parts = [f"{self.role} pid {os.getpid()}"]
        if "private_mb" in stats:
            parts.append(f"private {stats['private_mb']:.1f} MB")
        if "ws_mb" in stats:
            parts.append(f"ws {stats['ws_mb']:.1f} MB (peak {stats.get('peak_ws_mb', 0):.1f})")
        for key, label in (("handles", "handles"), ("gdi", "gdi"), ("user", "user"), ("threads", "threads"),
                           ("objects", "python-objects")):
            if key in stats:
                parts.append(f"{label} {stats[key]}")
        parts.append(f"cpu {stats.get('cpu_pct', 0.0):.1f}%")
        if stats.get("hot_threads"):
            parts.append(f"busiest threads: {stats['hot_threads']}")
        parts.append(f"up {_fmt_duration(stats.get('uptime_s', 0.0))}")
        for key, value in (extra or {}).items():
            parts.append(f"{key} {value:.1f}" if isinstance(value, float) else f"{key} {value}")
        return ", ".join(parts)

    # ── judgement ───────────────────────────────────────────────────────────

    def growth_mb_per_hour(self) -> float | None:
        """The slope over the recent window, or ``None`` before there are enough samples."""
        if len(self._history) < GROWTH_MIN_SAMPLES:
            return None
        newest = self._history[-1][0]
        window = [(t, v) for t, v in self._history if newest - t <= GROWTH_WINDOW_S]
        if len(window) < GROWTH_MIN_SAMPLES:
            return None
        return slope_mb_per_hour(window)

    def _check_growth(self, now: float, private: float) -> None:
        rate = self.growth_mb_per_hour()
        if rate is None or rate < GROWTH_WARN_MB_PER_H or private < GROWTH_WARN_MIN_MB:
            return
        if now - self._warned_at < GROWTH_WARN_EVERY_S:
            return
        self._warned_at = now
        self._log.warning("health: private memory of %s grows by about %.0f MB per hour (now %.0f MB)", self.role, rate, private)
        if self.diag:
            self._log_diag(force_report=True)

    def _check_limit(self, now: float, private: float, stats: dict) -> None:
        if self.limit_mb is None or self._on_limit is None:
            return
        self._over = self._over + 1 if private > self.limit_mb else 0
        if self._over < LIMIT_SAMPLES or now - self._limit_called_at < 3600.0:
            return
        self._limit_called_at = now
        self._log.warning("health: %s holds %.0f MB, over its limit of %.0f MB", self.role, private, self.limit_mb)
        try:
            self._on_limit(dict(stats))
        except Exception:                                       # noqa: BLE001
            self._log.exception("health: the limit action failed")

    # ── diagnostics (ASM_DIAG) ───────────────────────────────────────────────

    def _log_diag(self, force_report: bool = False) -> None:
        counts = type_counts()
        if self._types0 is None:
            self._types0 = counts
        if self._types_last is not None or force_report:
            base = self._types_last or self._types0
            grown = sorted(((counts[k] - base.get(k, 0), k) for k in counts), reverse=True)[:8]
            grown = [(d, k) for d, k in grown if d > 0]
            if grown:
                self._log.info("diag: objects grown since the last sample: %s", ", ".join(f"+{d} {k}" for d, k in grown))
            total = sorted(((counts[k] - self._types0.get(k, 0), k) for k in counts), reverse=True)[:6]
            total = [(d, k) for d, k in total if d > 0]
            if total:
                self._log.info("diag: objects grown since the first sample: %s", ", ".join(f"+{d} {k}" for d, k in total))
        self._types_last = counts
        if self.diag == "2":
            self._log_trace()

    def _log_trace(self) -> None:
        import tracemalloc
        if not tracemalloc.is_tracing():
            return
        snap = tracemalloc.take_snapshot()
        if self._trace0 is None:
            self._trace0 = snap
            return
        for stat in snap.compare_to(self._trace0, "lineno")[:6]:
            if stat.size_diff > 0:
                self._log.info("diag: allocated since the first sample: %s", stat)


def start_for(role: str, logger: logging.Logger | None = None, **kwargs) -> HealthMonitor | None:
    """Start the monitor of this process on Windows (``ASM_NO_HEALTH=1`` switches it off); ``None`` elsewhere."""
    if sys.platform != "win32" and not os.environ.get("ASM_HEALTH_ANYWHERE"):
        return None
    if os.environ.get("ASM_NO_HEALTH"):
        return None
    try:
        return HealthMonitor(role, logger, **kwargs).start()
    except Exception:                                           # noqa: BLE001
        _log.debug("could not start the health monitor", exc_info=True)
        return None
