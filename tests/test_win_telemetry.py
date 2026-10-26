"""The Windows build has no telemetry: it never asks, never stores a consent, never sends."""
from __future__ import annotations

import threading

import pytest

from arctis_sound_manager import telemetry


@pytest.fixture
def on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry, "IS_WINDOWS", True)
    monkeypatch.setattr(telemetry, "_TELEMETRY_FILE", tmp_path / "telemetry.yaml")
    return tmp_path


def test_consent_is_never_none_and_never_true_on_windows(on_windows):
    assert telemetry.get_consent() is False                       # False, not None: no consent dialog is queued
    telemetry.set_consent(True)
    assert telemetry.get_consent() is False
    assert not (on_windows / "telemetry.yaml").exists()


def test_nothing_is_sent_even_when_a_consent_file_says_yes(on_windows, monkeypatch):
    (on_windows / "telemetry.yaml").write_text("consent: true\n")
    monkeypatch.setattr(telemetry, "_do_send", lambda *a: pytest.fail("a report was sent"))
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self))
    telemetry.maybe_send("Arctis Nova Pro Wireless", "12e0")
    assert started == []


def test_other_systems_keep_the_original_behaviour(monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry, "IS_WINDOWS", False)
    monkeypatch.setattr(telemetry, "_TELEMETRY_FILE", tmp_path / "telemetry.yaml")
    assert telemetry.get_consent() is None
    telemetry.set_consent(True)
    assert telemetry.get_consent() is True
