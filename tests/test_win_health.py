# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""The health monitor: a log line per sample, a warning for steady growth, a one-shot action at a hard limit — and the
way a tray replaces itself without stopping the daemon."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

import pytest

from arctis_sound_manager.win import health, selfrestart


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make(stats, *, clock=None, **kwargs):
    """A monitor that reads *stats()* (a callable returning a dict) and a fake clock; no thread is started."""
    clock = clock or Clock()
    kwargs.setdefault("diag", "")
    kwargs.setdefault("interval", 300.0)
    monitor = health.HealthMonitor("test", logging.getLogger("health-test"), stats=stats, clock=clock, **kwargs)
    return monitor, clock


def growing(start=100.0, per_hour=300.0, step=300.0, clock=None):
    """Stats whose private memory rises *per_hour* MB each hour, sampled every *step* seconds."""
    state = {"t": 0.0}

    def read():
        value = start + per_hour * state["t"] / 3600.0
        state["t"] += step
        return {"private_mb": value, "handles": 100}
    return read


# ── measuring ────────────────────────────────────────────────────────────────

def test_process_stats_describe_this_process():
    stats = health.process_stats()
    assert stats["private_mb"] > 5 and stats["ws_mb"] > 5
    assert stats["handles"] > 0
    assert stats["threads"] >= 1


def test_process_stats_of_another_pid_are_empty_or_complete_but_never_raise():
    assert isinstance(health.process_stats(2 ** 22 + 12345), dict)


def test_slope_of_a_straight_line():
    samples = [(t, 100.0 + t * 100.0 / 3600.0) for t in range(0, 3600, 300)]
    assert health.slope_mb_per_hour(samples) == pytest.approx(100.0, rel=1e-6)


def test_slope_of_nothing_or_one_point_or_a_flat_line_is_zero():
    assert health.slope_mb_per_hour([]) == 0.0
    assert health.slope_mb_per_hour([(1.0, 50.0)]) == 0.0
    assert health.slope_mb_per_hour([(t, 70.0) for t in range(0, 3000, 300)]) == pytest.approx(0.0, abs=1e-9)


def test_diag_flag_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert health.diag_from_file() == ""
    (tmp_path / "arctis_manager").mkdir()
    (tmp_path / "arctis_manager" / "diag.txt").write_text("2\n")
    assert health.diag_from_file() == "2"
    (tmp_path / "arctis_manager" / "diag.txt").write_text("hello")
    assert health.diag_from_file() == ""


# ── the line ─────────────────────────────────────────────────────────────────

def test_a_sample_writes_one_readable_line(caplog):
    monitor, _ = make(lambda: {"private_mb": 47.25, "ws_mb": 30.0, "peak_ws_mb": 51.0, "handles": 212, "gdi": 7, "user": 3,
                               "threads": 17}, extra=lambda: {"qt_widgets": 412, "helper_private_mb": 12.5})
    with caplog.at_level(logging.INFO, logger="health-test"):
        monitor.sample()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("health:")]
    assert len(lines) == 1
    line = lines[0]
    for piece in ("test pid", "private 47.2 MB", "handles 212", "gdi 7", "user 3", "threads 17", "python-objects",
                  "cpu ", "up 0m00s", "qt_widgets 412", "helper_private_mb 12.5"):
        assert piece in line, (piece, line)


def test_a_broken_extra_or_stats_source_does_not_stop_sampling(caplog):
    def boom():
        raise RuntimeError("x")
    monitor, _ = make(lambda: {"private_mb": 10.0}, extra=boom)
    monitor.sample()
    assert monitor.last["private_mb"] == 10.0


def test_uptime_is_formatted():
    monitor, clock = make(lambda: {"private_mb": 10.0})
    clock.advance(2 * 3600 + 5 * 60)
    assert "up 2h05m" in monitor.describe({"uptime_s": clock() - monitor._started})


# ── growth ───────────────────────────────────────────────────────────────────

def test_steady_growth_is_reported_at_most_every_half_hour(caplog):
    clock = Clock()
    monitor, _ = make(growing(start=300.0, per_hour=400.0), clock=clock)
    with caplog.at_level(logging.WARNING, logger="health-test"):
        for _ in range(8):                                    # 35 minutes of samples, every five minutes
            monitor.sample()
            clock.advance(300.0)
        warnings = [r.getMessage() for r in caplog.records if "grows by about" in r.getMessage()]
        assert len(warnings) == 1
        assert "test" in warnings[0]
        assert monitor.growth_mb_per_hour() == pytest.approx(400.0, rel=0.05)
        for _ in range(4):                                    # and a second one half an hour after the first
            monitor.sample()
            clock.advance(300.0)
    warnings = [r.getMessage() for r in caplog.records if "grows by about" in r.getMessage()]
    assert len(warnings) == 2


def test_a_flat_process_is_never_reported(caplog):
    clock = Clock()
    monitor, _ = make(lambda: {"private_mb": 800.0}, clock=clock)
    with caplog.at_level(logging.WARNING, logger="health-test"):
        for _ in range(30):
            monitor.sample()
            clock.advance(300.0)
    assert not [r for r in caplog.records if "grows" in r.getMessage()]


def test_growth_of_a_small_process_is_not_worth_a_warning(caplog):
    clock = Clock()
    monitor, _ = make(growing(start=20.0, per_hour=170.0), clock=clock)    # 20 → ~100 MB over the window: still tiny
    with caplog.at_level(logging.WARNING, logger="health-test"):
        for _ in range(7):
            monitor.sample()
            clock.advance(300.0)
    assert not [r for r in caplog.records if "grows" in r.getMessage()]


def test_too_few_samples_give_no_verdict():
    monitor, clock = make(growing())
    for _ in range(3):
        monitor.sample()
        clock.advance(300.0)
    assert monitor.growth_mb_per_hour() is None


# ── the limit ────────────────────────────────────────────────────────────────

def test_the_limit_action_needs_two_samples_in_a_row_and_runs_once_per_hour():
    calls = []
    level = {"mb": 500.0}
    monitor, clock = make(lambda: {"private_mb": level["mb"]}, limit_mb=1000.0, on_limit=calls.append)
    monitor.sample()
    level["mb"] = 1500.0
    monitor.sample()
    assert calls == []                                       # one sample over the line is not enough
    clock.advance(300.0)
    monitor.sample()
    assert len(calls) == 1 and calls[0]["private_mb"] == 1500.0
    for _ in range(5):                                       # still over, but it has been asked already
        clock.advance(300.0)
        monitor.sample()
    assert len(calls) == 1
    clock.advance(3600.0)
    monitor.sample()
    assert len(calls) == 2


def test_a_dip_under_the_limit_resets_the_count():
    calls = []
    values = iter([1500.0, 600.0, 1500.0, 600.0, 1500.0])
    monitor, clock = make(lambda: {"private_mb": next(values)}, limit_mb=1000.0, on_limit=calls.append)
    for _ in range(5):
        monitor.sample()
        clock.advance(300.0)
    assert calls == []


def test_a_failing_limit_action_is_logged_not_raised(caplog):
    def boom(stats):
        raise RuntimeError("no")
    monitor, clock = make(lambda: {"private_mb": 2000.0}, limit_mb=1000.0, on_limit=boom)
    with caplog.at_level(logging.WARNING, logger="health-test"):
        monitor.sample()
        clock.advance(300.0)
        monitor.sample()
    assert any("limit action failed" in r.getMessage() for r in caplog.records)


def test_no_limit_means_no_action():
    calls = []
    monitor, clock = make(lambda: {"private_mb": 9e9}, limit_mb=None, on_limit=calls.append)
    for _ in range(4):
        monitor.sample()
        clock.advance(300.0)
    assert calls == []


# ── diagnostics ──────────────────────────────────────────────────────────────

def test_diag_names_the_object_types_that_grew(caplog):
    class Leaked:
        pass
    keep = []
    monitor, clock = make(lambda: {"private_mb": 10.0}, diag="1")
    with caplog.at_level(logging.INFO, logger="health-test"):
        monitor.sample()
        keep.extend(Leaked() for _ in range(500))
        clock.advance(300.0)
        monitor.sample()
    said = " ".join(r.getMessage() for r in caplog.records if r.getMessage().startswith("diag:"))
    assert "Leaked" in said and "+5" in said, said
    assert keep


def test_diag_2_shows_where_memory_was_allocated(caplog):
    import tracemalloc
    monitor, clock = make(lambda: {"private_mb": 10.0}, diag="2")
    try:
        monitor.start = None                                  # the thread is not wanted; tracing is
        tracemalloc.start(5)
        with caplog.at_level(logging.INFO, logger="health-test"):
            monitor.sample()
            blob = [bytearray(1000) for _ in range(2000)]     # ~2 MB from this very line
            clock.advance(300.0)
            monitor.sample()
        said = " ".join(r.getMessage() for r in caplog.records if r.getMessage().startswith("diag: allocated"))
        assert "test_win_health.py" in said, said
        assert blob
    finally:
        tracemalloc.stop()


# ── the thread and start_for ─────────────────────────────────────────────────

def test_the_thread_samples_until_stopped():
    monitor = health.HealthMonitor("thread", logging.getLogger("health-test"), diag="")
    monitor.interval = 0.05
    monitor.start()
    deadline = time.monotonic() + 3
    while monitor.samples < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    monitor.stop()
    assert monitor.samples >= 3
    seen = monitor.samples
    time.sleep(0.3)
    assert monitor.samples <= seen + 1


def test_start_for_does_nothing_off_windows(monkeypatch):
    monkeypatch.delenv("ASM_HEALTH_ANYWHERE", raising=False)
    if sys.platform != "win32":
        assert health.start_for("x") is None


def test_start_for_can_be_forced_and_silenced(monkeypatch):
    monkeypatch.setenv("ASM_HEALTH_ANYWHERE", "1")
    monitor = health.start_for("x", logging.getLogger("health-test"), interval=60.0, diag="")
    try:
        assert monitor is not None
        assert monitor._thread is not None and monitor._thread.daemon
    finally:
        monitor.stop()
    monkeypatch.setenv("ASM_NO_HEALTH", "1")
    assert health.start_for("x") is None


def test_the_lines_survive_a_quiet_root_logger():
    log = logging.getLogger("health-quiet")
    log.setLevel(logging.NOTSET)
    root = logging.getLogger()
    before = root.level
    root.setLevel(logging.CRITICAL)
    try:
        health.HealthMonitor("q", log, diag="")
        assert log.getEffectiveLevel() <= logging.INFO
    finally:
        root.setLevel(before)


# ── replacing the tray ───────────────────────────────────────────────────────

def test_the_replacement_carries_the_pid_of_the_tray_it_replaces(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    seen = {}

    def popen(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return object()
    assert selfrestart.respawn(popen) is True
    assert seen["kwargs"]["env"][selfrestart.WAIT_ENV] == str(os.getpid())
    assert seen["cmd"][-1] == "--background"
    assert seen["kwargs"]["close_fds"] is True


def test_a_replacement_that_cannot_start_leaves_the_tray_running(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def popen(cmd, **kwargs):
        raise OSError("no")
    assert selfrestart.respawn(popen) is False


def test_the_frozen_program_is_started_with_its_one_argument(monkeypatch):
    from arctis_sound_manager.win import startup
    monkeypatch.setattr(startup, "frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\ASM\ArctisSoundManager.exe")
    assert selfrestart.replacement_command() == [r"C:\Program Files\ASM\ArctisSoundManager.exe", "--background"]


def test_nothing_to_wait_for_without_the_variable(monkeypatch):
    monkeypatch.delenv(selfrestart.WAIT_ENV, raising=False)
    assert selfrestart.wait_for_predecessor(alive=lambda pid: pytest.fail("must not look")) is True


def test_waiting_ends_when_the_old_tray_has_left_and_the_variable_is_consumed(monkeypatch):
    monkeypatch.setenv(selfrestart.WAIT_ENV, "4242")
    answers = iter([True, True, False])
    naps = []
    assert selfrestart.wait_for_predecessor(alive=lambda pid: next(answers), sleep=naps.append) is True
    assert len(naps) == 2
    assert selfrestart.WAIT_ENV not in os.environ              # the next restart must not wait for an old pid


def test_waiting_gives_up_after_the_timeout(monkeypatch):
    monkeypatch.setenv(selfrestart.WAIT_ENV, "4242")
    clock = Clock()

    def nap(seconds):
        clock.advance(seconds * 50)
    assert selfrestart.wait_for_predecessor(timeout=5.0, alive=lambda pid: True, sleep=nap, clock=clock) is False


def test_garbage_or_own_pid_in_the_variable_is_ignored(monkeypatch):
    for value in ("soon", "", str(os.getpid())):
        monkeypatch.setenv(selfrestart.WAIT_ENV, value)
        assert selfrestart.wait_for_predecessor(alive=lambda pid: pytest.fail("must not look")) is True


def test_pid_alive_knows_the_living_and_the_dead():
    assert selfrestart.pid_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not selfrestart.pid_alive(child.pid)
    assert not selfrestart.pid_alive(0)


def test_waiting_for_a_real_process():
    import threading
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
    threading.Thread(target=child.wait, daemon=True).start()     # not our child in real life: nobody is left as a zombie
    started = time.monotonic()
    os.environ[selfrestart.WAIT_ENV] = str(child.pid)
    try:
        assert selfrestart.wait_for_predecessor(timeout=10.0) is True
    finally:
        os.environ.pop(selfrestart.WAIT_ENV, None)
    assert 0.3 < time.monotonic() - started < 5.0


# ── the tray's side ──────────────────────────────────────────────────────────

class _Stub:
    """Just enough of the tray for its restart method."""

    def __init__(self, visible=False, due=True):
        self._memory_restart_due = due
        self._visible = visible
        self.hidden = False
        outer = self

        class Icon:
            def hide(self_inner):
                outer.hidden = True
        self.tray_icon = Icon()

    def _window_visible(self):
        return self._visible


@pytest.fixture
def tray_method(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from arctis_sound_manager.gui import systray_app
    exits = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(logging, "shutdown", lambda: None)
    monkeypatch.setattr(_Stub, "_replace_tray", systray_app.QSystrayApp._replace_tray, raising=False)
    return systray_app.QSystrayApp._restart_for_memory_if_idle, exits


def test_the_tray_waits_while_its_window_is_open(tray_method, monkeypatch):
    method, exits = tray_method
    monkeypatch.setattr(selfrestart, "respawn", lambda *a, **k: pytest.fail("the window is open"))
    stub = _Stub(visible=True)
    method(stub)
    assert stub._memory_restart_due is True and exits == []


def test_the_tray_replaces_itself_once_the_window_is_closed(tray_method, monkeypatch):
    method, exits = tray_method
    started = []
    monkeypatch.setattr(selfrestart, "respawn", lambda *a, **k: started.append(1) or True)
    stub = _Stub(visible=False)
    method(stub)
    assert started == [1] and stub.hidden and exits == [0] and stub._memory_restart_due is False


def test_the_tray_stays_when_the_replacement_cannot_start(tray_method, monkeypatch):
    method, exits = tray_method
    monkeypatch.setattr(selfrestart, "respawn", lambda *a, **k: False)
    stub = _Stub(visible=False)
    method(stub)
    assert exits == [] and not stub.hidden


def test_nothing_happens_unless_the_limit_was_hit(tray_method, monkeypatch):
    method, exits = tray_method
    monkeypatch.setattr(selfrestart, "respawn", lambda *a, **k: pytest.fail("no limit was hit"))
    method(_Stub(visible=False, due=False))
    assert exits == []


def test_the_memory_limit_setting(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from arctis_sound_manager.gui import systray_app
    monkeypatch.delenv("ASM_TRAY_MEMORY_LIMIT_MB", raising=False)
    assert systray_app._tray_memory_limit_mb() == 1200.0
    monkeypatch.setenv("ASM_TRAY_MEMORY_LIMIT_MB", "800")
    assert systray_app._tray_memory_limit_mb() == 800.0
    monkeypatch.setenv("ASM_TRAY_MEMORY_LIMIT_MB", "0")
    assert systray_app._tray_memory_limit_mb() is None
    monkeypatch.setenv("ASM_TRAY_MEMORY_LIMIT_MB", "lots")
    assert systray_app._tray_memory_limit_mb() == 1200.0


# ── a closed window is given back ────────────────────────────────────────────

class _IdleStub:
    """Just enough of the tray for ``_release_idle_window``."""

    def __init__(self, *, visible=False, built=True):
        self._visible = visible
        self._main_app = object() if built else None
        self.replaced: list[str] = []

    def _window_visible(self):
        return self._visible

    def _replace_tray(self, why):
        self.replaced.append(why)


@pytest.fixture
def idle(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from arctis_sound_manager.gui import systray_app
    clock = Clock()
    monkeypatch.setattr(systray_app, "monotonic", clock)
    monkeypatch.delenv("ASM_TRAY_IDLE_RELEASE_S", raising=False)
    return systray_app.QSystrayApp._release_idle_window, clock


def test_a_window_closed_for_ten_minutes_is_given_back(idle):
    method, clock = idle
    stub = _IdleStub()
    method(stub)                                             # the first look only notes when it was closed
    assert stub.replaced == []
    clock.advance(540)
    method(stub)
    assert stub.replaced == []
    clock.advance(70)
    method(stub)
    assert len(stub.replaced) == 1 and "closed for 10 minutes" in stub.replaced[0]


def test_an_open_window_is_never_given_back_and_reopening_starts_the_count_again(idle):
    method, clock = idle
    stub = _IdleStub()
    method(stub)
    clock.advance(500)
    stub._visible = True                                     # the user opens it again
    method(stub)
    clock.advance(5000)
    method(stub)
    assert stub.replaced == []
    stub._visible = False
    method(stub)
    clock.advance(300)
    method(stub)
    assert stub.replaced == []                               # counted from the new closing, not the old one


def test_a_tray_that_never_built_a_window_has_nothing_to_give_back(idle):
    method, clock = idle
    stub = _IdleStub(built=False)
    for _ in range(30):
        method(stub)
        clock.advance(120)
    assert stub.replaced == []


def test_the_idle_release_can_be_switched_off_or_shortened(idle, monkeypatch):
    method, clock = idle
    monkeypatch.setenv("ASM_TRAY_IDLE_RELEASE_S", "0")
    stub = _IdleStub()
    for _ in range(30):
        method(stub)
        clock.advance(600)
    assert stub.replaced == []
    monkeypatch.setenv("ASM_TRAY_IDLE_RELEASE_S", "60")
    other = _IdleStub()
    method(other)
    clock.advance(61)
    method(other)
    assert len(other.replaced) == 1


def test_the_idle_release_setting_parses(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from arctis_sound_manager.gui import systray_app
    monkeypatch.delenv("ASM_TRAY_IDLE_RELEASE_S", raising=False)
    assert systray_app._tray_idle_release_seconds() == 600.0
    monkeypatch.setenv("ASM_TRAY_IDLE_RELEASE_S", "oops")
    assert systray_app._tray_idle_release_seconds() == 600.0
    monkeypatch.setenv("ASM_TRAY_IDLE_RELEASE_S", "-5")
    assert systray_app._tray_idle_release_seconds() is None


def test_replacing_the_tray_starts_the_new_one_first_and_hides_the_icon(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from arctis_sound_manager.gui import systray_app
    order: list[str] = []
    monkeypatch.setattr(selfrestart, "respawn", lambda *a, **k: order.append("respawn") or True)
    monkeypatch.setattr(os, "_exit", lambda code: order.append(f"exit {code}"))
    monkeypatch.setattr(logging, "shutdown", lambda: order.append("flush logs"))

    class Icon:
        def hide(self):
            order.append("hide icon")

    stub = type("Stub", (), {"tray_icon": Icon()})()
    systray_app.QSystrayApp._replace_tray(stub, "test")
    assert order == ["respawn", "hide icon", "flush logs", "exit 0"]


# ── the CI soak hook ─────────────────────────────────────────────────────────

def test_the_soak_hook_does_nothing_unless_asked(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from arctis_sound_manager.gui import soak_hook
    monkeypatch.delenv("ASM_SOAK_CYCLE_S", raising=False)
    assert soak_hook.install(object()) is None
    monkeypatch.setenv("ASM_SOAK_CYCLE_S", "soon")
    assert soak_hook.install(object()) is None


def test_the_soak_hook_steps_through_the_pages(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication
    from arctis_sound_manager.gui import soak_hook
    app = QApplication.instance() or QApplication([])
    seen: list[int] = []

    class Window(QObject):
        def _switch_page(self, index):
            seen.append(index)

    window = Window()
    monkeypatch.setenv("ASM_SOAK_CYCLE_S", "0.01")
    timer = soak_hook.install(window)
    assert timer is not None
    end = time.monotonic() + 1.0
    while len(seen) < 10 and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
    timer.stop()
    assert len(seen) >= 8
    assert len(set(seen)) >= 5 and seen[0] != seen[1]


# ── a runaway engine is replaced ─────────────────────────────────────────────

def test_a_swollen_engine_starts_its_replacement_and_asks_itself_to_stop(monkeypatch, tmp_path):
    import threading
    from types import SimpleNamespace
    from arctis_sound_manager.win.audio import engine_main
    seen = {}
    monkeypatch.setattr(selfrestart, "respawn", lambda *a, **k: seen.update(k) or True)
    host = SimpleNamespace(shutdown_requested=threading.Event())
    engine_main.replace_engine(tmp_path, host, logging.getLogger("engine"), {"private_mb": 2000.0})
    assert host.shutdown_requested.is_set()
    assert seen["command"][-2:] == ["--config-dir", str(tmp_path)]
    assert seen["log_name"] is None
    assert "--engine" in seen["command"] or "arctis_sound_manager.win.audio.engine_main" in seen["command"]


def test_an_engine_whose_replacement_cannot_start_keeps_running(monkeypatch, tmp_path):
    import threading
    from types import SimpleNamespace
    from arctis_sound_manager.win.audio import engine_main
    monkeypatch.setattr(selfrestart, "respawn", lambda *a, **k: False)
    host = SimpleNamespace(shutdown_requested=threading.Event())
    engine_main.replace_engine(tmp_path, host, logging.getLogger("engine"), {"private_mb": 2000.0})
    assert not host.shutdown_requested.is_set()


def test_the_engine_memory_limit_setting(monkeypatch):
    from arctis_sound_manager.win.audio import engine_main
    monkeypatch.delenv("ASM_ENGINE_MEMORY_LIMIT_MB", raising=False)
    assert engine_main._engine_memory_limit() == engine_main.ENGINE_MEMORY_LIMIT_MB == 1500.0
    monkeypatch.setenv("ASM_ENGINE_MEMORY_LIMIT_MB", "0")
    assert engine_main._engine_memory_limit() is None
    monkeypatch.setenv("ASM_ENGINE_MEMORY_LIMIT_MB", "x")
    assert engine_main._engine_memory_limit() == 1500.0


def test_a_replacement_engine_waits_for_the_old_one_before_looking_for_a_running_engine(monkeypatch, tmp_path):
    from arctis_sound_manager.win.audio import engine_main
    order = []
    monkeypatch.setattr(selfrestart, "wait_for_predecessor", lambda *a, **k: order.append("waited") or True)

    class Client:
        def __init__(self, *a, **k):
            order.append("client")

        def available(self):
            order.append("available")
            return True

    monkeypatch.setattr(engine_main, "EngineClient", Client)
    assert engine_main.run_engine(tmp_path) == 0                  # "already running": returns at once
    assert order[:2] == ["waited", "client"]


def test_respawn_can_start_any_command_without_a_log(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    seen = {}

    def popen(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return object()
    assert selfrestart.respawn(popen, command=["engine.exe", "--engine"], log_name=None)
    assert seen["cmd"] == ["engine.exe", "--engine"]
    assert seen["kwargs"]["stdout"] == subprocess.DEVNULL
    assert seen["kwargs"]["env"][selfrestart.WAIT_ENV] == str(os.getpid())


# ── the restart the upgrade check asks for ───────────────────────────────────

def test_restart_gui_on_windows_starts_a_replacement_instead_of_exec(monkeypatch):
    from arctis_sound_manager import runtime_staleness as rs
    order = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(selfrestart, "respawn", lambda *a, **k: order.append("respawn") or True)
    monkeypatch.setattr(os, "_exit", lambda code: order.append(f"exit {code}"))
    monkeypatch.setattr(logging, "shutdown", lambda: order.append("flush logs"))
    monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("there is no exec on Windows"))
    rs.restart_gui()
    assert order == ["respawn", "flush logs", "exit 0"]


def test_restart_gui_on_windows_fails_loudly_when_the_replacement_cannot_start(monkeypatch):
    from arctis_sound_manager import runtime_staleness as rs
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(selfrestart, "respawn", lambda *a, **k: False)
    monkeypatch.setattr(os, "_exit", lambda code: pytest.fail("must not leave without a replacement"))
    with pytest.raises(OSError):
        rs.restart_gui()
