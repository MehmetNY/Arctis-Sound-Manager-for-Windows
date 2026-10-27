"""Does asking Windows "which HID devices are there?" leak? Run on a real Windows machine.

With no headset on the PC the daemon used to ask once per device profile every three seconds, for as long as the PC
stayed on (a user's log shows 8365 such rounds in one session, about 25 enumerations each, and the program had grown to
2 GB by then). This script measures the call itself, which is the only native piece involved:

    python ci/hid_leak.py

It prints the process's private bytes and handle count while it calls ``hid.enumerate`` thousands of times, once for
every device and once for the vendor filter the daemon uses, and the growth per call over the second half of each
phase. Exit code 0 unless a phase grows by more than ``LIMIT_KB_PER_CALL`` per call.
"""
from __future__ import annotations

import os
import sys
import time

LIMIT_KB_PER_CALL = 1.0
CALLS = int(os.environ.get("HID_LEAK_CALLS", "4000"))
EVERY = 500
MAX_PHASE_S = 150.0


def log(*parts) -> None:
    print(*parts, flush=True)


def sample() -> tuple[float, int]:
    import psutil
    proc = psutil.Process()
    info = proc.memory_info()
    private = getattr(info, "private", info.vms) / 2 ** 20
    handles = proc.num_handles() if hasattr(proc, "num_handles") else 0
    return private, handles


def slope_kb_per_call(points: list[tuple[int, float]]) -> float:
    half = points[len(points) // 2:]
    if len(half) < 2:
        return 0.0
    n = len(half)
    mx = sum(x for x, _ in half) / n
    my = sum(y for _, y in half) / n
    var = sum((x - mx) ** 2 for x, _ in half)
    return 0.0 if var == 0 else sum((x - mx) * (y - my) for x, y in half) / var * 1024.0


def phase(title: str, call) -> float:
    log(f"== {title}")
    points: list[tuple[int, float]] = []
    started = time.time()
    done = 0
    for i in range(CALLS):
        entries = call()
        done = i + 1
        if i % EVERY == 0:
            private, handles = sample()
            points.append((i, private))
            log(f"   call {i:5d}: private {private:7.1f} MB, handles {handles:5d}, devices seen {len(entries)}, "
                f"{(time.time() - started) / (i + 1) * 1000:.1f} ms per call")
        if time.time() - started > MAX_PHASE_S:
            log(f"   stopping after {done} calls ({MAX_PHASE_S:.0f} s)")
            break
    private, handles = sample()
    points.append((done, private))
    slope = slope_kb_per_call(points)
    log(f"   {title}: {points[0][1]:.1f} MB -> {points[-1][1]:.1f} MB over {done} calls; growth {slope:+.2f} KB per call "
        f"(second half), handles now {handles}")
    return slope


def main() -> int:
    try:
        import hid  # type: ignore[import-not-found]
    except ImportError as exc:
        log("SKIP no hid module:", repr(exc))
        return 0
    log("hid module:", getattr(hid, "__file__", "?"), "version", getattr(hid, "__version__", "?"))
    problems = []
    for title, call in (
        ("hid.enumerate() - every HID device of the machine", lambda: hid.enumerate()),
        ("hid.enumerate(0x1038, 0) - what the daemon asks (no SteelSeries device on this machine)",
         lambda: hid.enumerate(0x1038, 0)),
    ):
        slope = phase(title, call)
        if slope > LIMIT_KB_PER_CALL:
            problems.append(f"{title} grows by {slope:.2f} KB per call")
    log("== " + ("FAIL " + "; ".join(problems) if problems else "no leak in the enumeration"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
