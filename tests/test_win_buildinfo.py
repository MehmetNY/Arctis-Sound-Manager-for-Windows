"""Every process names its build when it starts, so that a log sent in says which build it came from."""
from __future__ import annotations

import logging

from arctis_sound_manager.win import buildinfo, health


def test_the_build_id_is_read_from_the_file_the_installer_build_writes(tmp_path, monkeypatch):
    path = tmp_path / "build_id.txt"
    path.write_text("abc1234 (2026-10-27 01:30 UTC)\n", encoding="utf-8")
    monkeypatch.setattr(buildinfo, "_file", lambda: path)
    monkeypatch.setattr(buildinfo, "_cached", None)
    assert buildinfo.build_id() == "abc1234 (2026-10-27 01:30 UTC)"


def test_a_source_checkout_has_no_file_and_says_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(buildinfo, "_file", lambda: tmp_path / "missing.txt")
    monkeypatch.setattr(buildinfo, "_cached", None)
    assert buildinfo.build_id() == "unknown"


def test_the_health_monitor_says_which_build_runs_before_its_first_sample(monkeypatch, caplog):
    monkeypatch.setattr(buildinfo, "_cached", "abc1234 (2026-10-27 01:30 UTC)")
    log = logging.getLogger("health-test")
    monitor = health.HealthMonitor("daemon", log, interval=3600)
    monkeypatch.setattr(monitor, "sample", lambda: None)
    monitor._stop.set()
    with caplog.at_level(logging.INFO, logger="health-test"):
        monitor._run()
    assert any("daemon pid" in r.getMessage() and "runs build abc1234" in r.getMessage() for r in caplog.records)
