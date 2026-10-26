"""Reads the "health:" lines the ASM processes write to their own logs (src/arctis_sound_manager/win/health.py) and judges them.

Kept apart from ci/soak.py because it needs nothing but the standard library, so it is unit-tested on every platform
(tests/test_ci_soak_report.py)."""
from __future__ import annotations

import re

LINE = re.compile(r"health: (?P<role>[\w-]+) pid (?P<pid>\d+), (?P<rest>.*)$")
NUMBER = {"private": r"private ([\d.]+) MB", "ws": r"ws ([\d.]+) MB", "handles": r"handles (\d+)", "gdi": r"gdi (\d+)",
          "user": r"user (\d+)", "threads": r"threads (\d+)", "objects": r"python-objects (\d+)", "cpu": r"cpu ([\d.]+)%",
          "qt_widgets": r"qt_widgets (\d+)", "helper_private_mb": r"helper_private_mb ([\d.]+)",
          "helper_handles": r"helper_handles (\d+)"}
UPTIME = re.compile(r"up (?:(\d+)h(\d+)m|(\d+)m(\d+)s)")

#: what a healthy process may do over a soak run (checked on the part of the run after start-up has settled)
LIMITS = {"growth_mb_per_h": 120.0, "handles": 150, "gdi": 60, "user": 60}
SETTLE = 4            # the first quarter of the samples is start-up: kernels compile, the window is built


def parse_uptime(text: str) -> float | None:
    m = UPTIME.search(text)
    if not m:
        return None
    if m.group(1) is not None:
        return int(m.group(1)) * 3600.0 + int(m.group(2)) * 60.0
    return int(m.group(3)) * 60.0 + int(m.group(4))


def parse_line(line: str) -> dict | None:
    m = LINE.search(line)
    if not m:
        return None
    rest = m.group("rest")
    out: dict = {"role": m.group("role"), "pid": int(m.group("pid"))}
    for key, pattern in NUMBER.items():
        found = re.search(pattern, rest)
        if found:
            out[key] = float(found.group(1)) if "." in found.group(1) or key in ("private", "ws", "cpu") else int(found.group(1))
    up = parse_uptime(rest)
    if up is not None:
        out["up"] = up
    window = re.search(r"window (open|closed)", rest)
    if window:
        out["window"] = window.group(1)
    return out


def collect(texts) -> dict[tuple[str, int], list[dict]]:
    """``{(role, pid): samples in time order}`` from the text of any number of log files."""
    series: dict[tuple[str, int], list[dict]] = {}
    for text in texts:
        for line in text.splitlines():
            row = parse_line(line)
            if row is not None and "up" in row:
                series.setdefault((row["role"], row["pid"]), []).append(row)
    for rows in series.values():
        rows.sort(key=lambda r: r["up"])
    return series


def slope_per_hour(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of ``(seconds, value)`` in value units per hour."""
    n = len(points)
    if n < 2:
        return 0.0
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    var = sum((t - mt) ** 2 for t, _ in points)
    return 0.0 if var <= 0 else sum((t - mt) * (v - mv) for t, v in points) / var * 3600.0


def settled(rows: list[dict]) -> list[dict]:
    return rows[len(rows) // SETTLE:]


def growth(rows: list[dict], key: str = "private") -> float:
    """MB per hour over the second half of the run."""
    half = rows[len(rows) // 2:]
    return slope_per_hour([(r["up"], r[key]) for r in half if key in r])


def change(rows: list[dict], key: str) -> int | float | None:
    """How much *key* changed between the settled start of the run and its end."""
    values = [r[key] for r in settled(rows) if key in r]
    return values[-1] - values[0] if len(values) >= 2 else None


def table(series: dict[tuple[str, int], list[dict]]) -> str:
    head = (f"{'process':<8}{'pid':>7}{'samples':>8}{'private MB':>12}{'-> MB':>8}{'MB/h':>7}{'handles':>9}{'gdi':>6}{'user':>6}"
            f"{'threads':>9}{'objects':>9}{'widgets':>9}{'cpu avg':>9}")
    lines = [head]
    for (role, pid), rows in sorted(series.items()):
        first, last = settled(rows)[0], rows[-1]
        cpus = [r["cpu"] for r in settled(rows) if "cpu" in r]
        lines.append(
            f"{role:<8}{pid:>7}{len(rows):>8}{first.get('private', 0):>12.0f}{last.get('private', 0):>8.0f}"
            f"{growth(rows):>7.0f}{_delta(rows, 'handles'):>9}{_delta(rows, 'gdi'):>6}{_delta(rows, 'user'):>6}"
            f"{last.get('threads', '-'):>9}{_delta(rows, 'objects'):>9}{_delta(rows, 'qt_widgets'):>9}"
            f"{(sum(cpus) / len(cpus) if cpus else 0.0):>9.1f}")
    return "\n".join(lines)


def _delta(rows, key) -> str:
    d = change(rows, key)
    return "-" if d is None else f"{d:+d}" if isinstance(d, int) else f"{d:+.0f}"


def verdict(series: dict[tuple[str, int], list[dict]], limits: dict | None = None, min_samples: int = 6,
            min_span: float = 0.0) -> list[str]:
    """Every way the run broke the limits, as sentences (empty when it is healthy).

    A process that lived for less than *min_span* seconds is not judged: what it does in its first minutes (the window
    being built, the libraries loading) is a step, not growth."""
    limits = limits or LIMITS
    problems: list[str] = []
    for (role, pid), rows in sorted(series.items()):
        if len(rows) < min_samples or rows[-1]["up"] - rows[0]["up"] < min_span:
            continue
        g = growth(rows)
        if g > limits["growth_mb_per_h"]:
            problems.append(f"{role} (pid {pid}) grows by {g:.0f} MB per hour")
        for key in ("handles", "gdi", "user"):
            d = change(rows, key)
            if d is not None and d > limits[key]:
                problems.append(f"{role} (pid {pid}) gained {d:.0f} {key} during the run")
    return problems


def diag_lines(text: str) -> list[str]:
    """The 'diag:' lines (which Python object types grew, which source lines allocated) of one log, in order."""
    return [line.strip() for line in text.splitlines() if "diag:" in line]
