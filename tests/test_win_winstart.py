"""scripts/winstart: the quiet start of the daemon, and the tray in this very process."""
from __future__ import annotations

import subprocess
import sys
import types

import pytest

from arctis_sound_manager.scripts import winstart


@pytest.fixture
def run_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(winstart, "runtime_dir", lambda: tmp_path / "run")
    return tmp_path / "run"


def test_no_lock_file_means_no_daemon(run_dir):
    assert winstart.daemon_running() is False
    assert (run_dir / winstart.LOCK_NAME).exists()            # created on the way, harmless


def test_a_held_lock_means_a_daemon_is_running(run_dir):
    from arctis_sound_manager.file_lock import lock_nb
    run_dir.mkdir(parents=True)
    handle = open(run_dir / winstart.LOCK_NAME, "w")
    try:
        lock_nb(handle.fileno())
        # flock() is per open file description: the probe's own descriptor is a different one, so it is refused
        assert winstart.daemon_running() is True
    finally:
        handle.close()
    assert winstart.daemon_running() is False                  # released when the holder goes


def test_an_unusable_runtime_directory_is_not_a_crash(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(winstart, "runtime_dir", lambda: blocker / "sub")
    assert winstart.daemon_running() is False


def test_the_daemon_is_not_started_twice(run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(winstart, "log_dir", lambda: tmp_path / "logs")
    calls = []
    assert winstart.start_daemon(popen=lambda *a, **k: calls.append((a, k)), running=lambda: True) is True
    assert calls == []


def test_the_daemon_is_started_detached_and_logs_to_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(winstart, "log_dir", lambda: tmp_path / "logs")
    calls = []
    assert winstart.start_daemon(popen=lambda *a, **k: calls.append((a, k)), running=lambda: False) is True
    (cmd,), kwargs = calls[0]
    assert cmd[-2:] == ["-m", winstart.DAEMON_MODULE] or cmd[-1] == "--daemon"
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["stdout"].name.endswith("daemon.log")
    assert (tmp_path / "logs" / "daemon.log").exists()
    assert kwargs["env"]["PYTHONPATH"].split(":")[0]            # the child imports the same package we run
    assert kwargs["env"]["ARCTIS_LOG_LEVEL"]


def test_the_daemon_gets_the_windows_creation_flags_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(winstart, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(winstart.os, "name", "nt")
    for name, value in (("CREATE_NO_WINDOW", 0x08000000), ("DETACHED_PROCESS", 0x8), ("CREATE_NEW_PROCESS_GROUP", 0x200)):
        monkeypatch.setattr(subprocess, name, value, raising=False)
    calls = []
    winstart.start_daemon(popen=lambda *a, **k: calls.append(k), running=lambda: False)
    assert calls[0]["creationflags"] == 0x08000000 | 0x8 | 0x200
    assert "start_new_session" not in calls[0]


def test_a_daemon_that_cannot_be_started_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(winstart, "log_dir", lambda: tmp_path / "logs")

    def boom(*a, **k):
        raise OSError("no such file")
    assert winstart.start_daemon(popen=boom, running=lambda: False) is False


def test_an_unwritable_log_directory_does_not_stop_the_daemon(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(winstart, "log_dir", lambda: blocker / "logs")
    calls = []
    assert winstart.start_daemon(popen=lambda *a, **k: calls.append(k), running=lambda: False) is True
    assert calls[0]["stdout"] == subprocess.DEVNULL


def test_a_log_that_grew_too_large_starts_over(tmp_path):
    path = tmp_path / "x.log"
    path.write_bytes(b"a" * (winstart.LOG_LIMIT + 1))
    winstart.open_log("x.log", tmp_path).close()
    assert path.stat().st_size == 0
    path.write_text("keep")
    winstart.open_log("x.log", tmp_path).close()
    assert path.read_text() == "keep"


def test_without_stdout_the_output_goes_to_the_tray_log(tmp_path, monkeypatch):
    monkeypatch.setattr(winstart, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    winstart.attach_output()
    print("hello", file=sys.stderr)
    sys.stderr.flush()
    assert "hello" in (tmp_path / "tray.log").read_text()


def test_with_a_console_the_streams_are_left_alone(monkeypatch):
    out, err = sys.stdout, sys.stderr
    winstart.attach_output()
    assert sys.stdout is out and sys.stderr is err


def _fake_gui(monkeypatch, exit_code=None):
    seen = {}
    gui = types.ModuleType("arctis_sound_manager.scripts.gui")

    def main():
        seen["argv"] = list(sys.argv)
        if exit_code is not None:
            raise SystemExit(exit_code)
    gui.main = main
    monkeypatch.setitem(sys.modules, "arctis_sound_manager.scripts.gui", gui)
    monkeypatch.setattr("arctis_sound_manager.scripts.gui", gui, raising=False)
    return seen


def test_main_starts_the_daemon_and_then_the_tray(monkeypatch):
    order = []
    monkeypatch.setattr(winstart, "start_daemon", lambda: order.append("daemon"))
    monkeypatch.setattr(winstart, "attach_output", lambda: None)
    seen = _fake_gui(monkeypatch)
    assert winstart.main(["--background"]) == 0
    assert order == ["daemon"] and seen["argv"][1:] == ["--systray"]


def test_main_show_opens_the_window(monkeypatch):
    monkeypatch.setattr(winstart, "start_daemon", lambda: None)
    monkeypatch.setattr(winstart, "attach_output", lambda: None)
    seen = _fake_gui(monkeypatch)
    winstart.main(["--show"])
    assert seen["argv"][1:] == []


def test_main_flags_select_what_is_started(monkeypatch):
    started = []
    monkeypatch.setattr(winstart, "start_daemon", lambda: started.append("daemon"))
    monkeypatch.setattr(winstart, "attach_output", lambda: None)
    seen = _fake_gui(monkeypatch)
    assert winstart.main(["--no-tray"]) == 0 and started == ["daemon"] and "argv" not in seen
    started.clear()
    winstart.main(["--no-daemon"])
    assert started == [] and "argv" in seen


def test_main_returns_the_guis_exit_code(monkeypatch):
    monkeypatch.setattr(winstart, "start_daemon", lambda: None)
    monkeypatch.setattr(winstart, "attach_output", lambda: None)
    _fake_gui(monkeypatch, exit_code=4)
    assert winstart.main([]) == 4
