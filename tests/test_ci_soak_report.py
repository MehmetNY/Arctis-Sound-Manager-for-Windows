# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""The CI soak report: it must read the lines the processes write, and tell a leak from a process that is just big."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ci"))

import soak_report as sr  # noqa: E402


def line(role="tray", pid=100, private=50.0, up="5m00s", handles=200, gdi=10, user=5, threads=12, objects=40000, cpu=1.5,
         extra=""):
    return (f"health: {role} pid {pid}, private {private:.1f} MB, ws 30.0 MB (peak 51.0), handles {handles}, gdi {gdi}, "
            f"user {user}, threads {threads}, python-objects {objects}, cpu {cpu:.1f}%, up {up}{extra}")


def series_from(rows):
    return sr.collect(["\n".join(rows)])


def test_a_health_line_is_read():
    row = sr.parse_line("14:02:11 INFO health: tray pid 8264, " + line(role="tray", pid=8264).split(", ", 1)[1]
                        + ", qt_widgets 1591, window open")
    assert row["role"] == "tray" and row["pid"] == 8264
    assert row["private"] == 50.0 and row["handles"] == 200 and row["gdi"] == 10 and row["user"] == 5
    assert row["threads"] == 12 and row["objects"] == 40000 and row["cpu"] == 1.5
    assert row["qt_widgets"] == 1591 and row["window"] == "open"
    assert row["up"] == 300.0


def test_uptimes_in_both_formats():
    assert sr.parse_uptime("... up 5m30s") == 330.0
    assert sr.parse_uptime("... up 2h05m, x") == 7500.0
    assert sr.parse_uptime("nothing") is None


def test_the_engine_line_with_its_helper_extras():
    row = sr.parse_line(line(role="engine", extra=", helper_private_mb 12.5, helper_handles 140"))
    assert row["helper_private_mb"] == 12.5 and row["helper_handles"] == 140


def test_other_lines_are_ignored():
    assert sr.parse_line("INFO engine: ASM audio engine starting") is None
    assert sr.collect(["INFO something\nhealth: broken"]) == {}


def test_samples_are_grouped_by_process_and_ordered_by_uptime():
    text = "\n".join([line(pid=1, up="10m00s"), line(pid=2, role="daemon", up="5m00s"), line(pid=1, up="5m00s")])
    series = sr.collect([text])
    assert set(series) == {("tray", 1), ("daemon", 2)}
    assert [r["up"] for r in series[("tray", 1)]] == [300.0, 600.0]


def test_slope():
    assert abs(sr.slope_per_hour([(0.0, 10.0), (1800.0, 60.0), (3600.0, 110.0)]) - 100.0) < 1e-9
    assert sr.slope_per_hour([(5.0, 1.0)]) == 0.0


def _run(private_of, minutes=20, step=1, **kw):
    return series_from([line(private=private_of(m), up=f"{m}m00s", **{k: (v(m) if callable(v) else v) for k, v in kw.items()})
                        for m in range(step, minutes + 1, step)])


def test_a_flat_process_is_healthy():
    assert sr.verdict(_run(lambda m: 80.0)) == []


def test_a_leaking_process_is_named_with_its_rate():
    problems = sr.verdict(_run(lambda m: 80.0 + 4.0 * m, minutes=30))        # 4 MB a minute = 240 MB an hour
    assert len(problems) == 1 and "tray" in problems[0] and "240" in problems[0]


def test_start_up_growth_is_not_a_leak():
    # the window is built in the first minutes (+250 MB), then nothing happens
    problems = sr.verdict(_run(lambda m: 60.0 + (250.0 if m >= 3 else 0.0), minutes=30))
    assert problems == []


def test_handle_and_gdi_growth_is_reported_after_start_up():
    problems = sr.verdict(_run(lambda m: 80.0, minutes=30, handles=lambda m: 200 + 10 * m, gdi=lambda m: 10 + 5 * m))
    text = " ".join(problems)
    assert "handles" in text and "gdi" in text and "user" not in text


def test_a_process_with_too_few_samples_is_not_judged():
    assert sr.verdict(_run(lambda m: 80.0 + 100.0 * m, minutes=4)) == []


def test_a_process_that_lived_only_a_few_minutes_is_not_judged_when_asked_not_to():
    """A replacement tray lives three minutes in the soak and takes a 100 MB step once it starts working."""
    privates = [48.0, 49.7, 51.9, 53.0, 158.5, 158.4]
    short = series_from([line(private=p, up=f"{(60 + 30 * i) // 60}m{(60 + 30 * i) % 60:02d}s", handles=200 + (300 if p > 100 else 0))
                         for i, p in enumerate(privates)])
    assert sr.verdict(short, min_span=600.0) == []
    assert sr.verdict(short, min_span=0.0) != []                     # the same run is a "leak" without the span rule
    long = _run(lambda m: 80.0 + 4.0 * m, minutes=30)
    assert len(sr.verdict(long, min_span=600.0)) == 1                # a real leak over half an hour is still found


def test_the_table_has_one_row_per_process():
    text = "\n".join([line(pid=1, up="5m00s"), line(pid=1, up="10m00s"), line(role="engine", pid=2, up="5m00s"),
                      line(role="engine", pid=2, up="10m00s")])
    table = sr.table(sr.collect([text]))
    assert table.count("\n") == 2 and "tray" in table and "engine" in table


def test_diag_lines_are_picked_out():
    text = "x\n14:00 INFO health: diag: objects grown since the last sample: +5 Leaked\nthe end"
    assert sr.diag_lines(text) == ["14:00 INFO health: diag: objects grown since the last sample: +5 Leaked"]
