# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""The on-demand profile of an audio thread: nothing happens until its file appears, then a short report in the log."""
from __future__ import annotations

import logging
import math

import pytest

from arctis_sound_manager.win import profiling


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _no_profile_left_running():
    yield
    if profiling._active is not None:
        profiling._active.abort()
    profiling._active = None


def trigger(tmp_path, clock, name="playback"):
    return profiling.ProfileTrigger(name, tmp_path, clock, logging.getLogger("profile-test"))


def work():
    return sum(math.sqrt(i) for i in range(20000))


def test_nothing_happens_without_the_file(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="profile-test")
    clock = Clock()
    t = trigger(tmp_path, clock)
    for _ in range(50):
        clock.now += 1.0
        t.poll()
    assert not t.running and "profile" not in caplog.text


def test_the_directory_is_looked_at_once_a_second_not_on_every_call(tmp_path, monkeypatch):
    clock = Clock()
    t = trigger(tmp_path, clock)
    checks = []
    monkeypatch.setattr(profiling.Path, "is_file", lambda self: checks.append(1) or False)
    for _ in range(1000):                       # a thousand blocks within one second
        t.poll()
    assert len(checks) == 1


def test_the_file_starts_a_profile_and_is_removed(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="profile-test")
    clock = Clock()
    t = trigger(tmp_path, clock)
    (tmp_path / "profile-playback.txt").write_text("5")
    t.poll()
    assert t.running
    assert not (tmp_path / "profile-playback.txt").exists()
    assert "5 s starting" in caplog.text


def test_after_its_time_the_report_names_the_functions_that_used_the_time(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="profile-test")
    clock = Clock()
    t = trigger(tmp_path, clock)
    (tmp_path / "profile-playback.txt").write_text("5")
    t.poll()
    for _ in range(20):
        work()
    clock.now += 4.0
    t.poll()
    assert t.running                            # not yet
    clock.now += 2.0
    t.poll()
    assert not t.running
    assert "own time" in caplog.text and "cumulative time" in caplog.text
    assert "work" in caplog.text and "calls" in caplog.text
    assert profiling._active is None


def test_no_number_means_twenty_seconds_and_nonsense_too(tmp_path):
    for content, expected in (("", 20.0), ("soon", 20.0), ("1", 2.0), ("100000", 120.0)):
        clock = Clock()
        t = trigger(tmp_path, clock)
        (tmp_path / "profile-playback.txt").write_text(content)
        t.poll()
        assert t._seconds == expected, content
        clock.now += 500
        t.poll()                                # finish, so the next one can start


def test_only_one_profile_at_a_time(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="profile-test")
    clock = Clock()
    first, second = trigger(tmp_path, clock, "playback"), trigger(tmp_path, clock, "microphone")
    (tmp_path / "profile-playback.txt").write_text("5")
    (tmp_path / "profile-microphone.txt").write_text("5")
    first.poll()
    second.poll()
    assert first.running and not second.running
    assert "another profile is running" in caplog.text
    clock.now += 10
    first.poll()
    clock.now += 2
    second.poll()                               # the file is gone: nothing to do
    assert not second.running


def test_the_pipeline_loop_asks_for_the_trigger_on_every_round():
    from arctis_sound_manager.win.audio import runtime
    import inspect
    assert "trigger.poll()" in inspect.getsource(runtime.Pipeline.run)


def test_short_names_keep_the_last_two_path_parts():
    assert profiling._short(("C:\\app\\arctis_sound_manager\\win\\audio\\graph.py", 197, "process")) == "audio/graph.py:197(process)"
    assert profiling._short(("<built-in>", 0, "len")) == "<built-in>:0(len)"
    assert profiling._short(("~", 0, "<method 'any'>")) == "~:0(<method 'any'>)"
