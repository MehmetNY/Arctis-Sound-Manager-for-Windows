"""Thread boosting: the call sequence and its tolerance of failure, against a fake Win32 API."""
from __future__ import annotations

import sys

from arctis_sound_manager.win.audio.realtime import (AVRT_PRIORITY_HIGH, THREAD_PRIORITY_TIME_CRITICAL, ThreadBoost,
                                                     TimerResolution)


class FakeApi:
    def __init__(self, mmcss=True, priority=True, raises=False):
        self.calls = []
        self.mmcss, self.priority, self.raises = mmcss, priority, raises

    def mmcss_enter(self, task):
        self.calls.append(("enter", task))
        if self.raises:
            raise OSError("avrt missing")
        return 1234 if self.mmcss else None

    def mmcss_leave(self, handle):
        self.calls.append(("leave", handle))

    def set_thread_priority(self, level):
        self.calls.append(("priority", level))
        if self.raises:
            raise OSError("denied")
        return self.priority

    def time_begin(self, ms):
        self.calls.append(("begin", ms))
        if self.raises:
            raise OSError

    def time_end(self, ms):
        self.calls.append(("end", ms))


def test_boost_registers_with_mmcss_sets_priority_and_leaves_again():
    api = FakeApi()
    with ThreadBoost(api=api) as boost:
        assert boost.applied == ["MMCSS Pro Audio", "time-critical priority"]
    assert api.calls == [("enter", "Pro Audio"), ("priority", THREAD_PRIORITY_TIME_CRITICAL), ("leave", 1234)]


def test_a_refused_mmcss_does_not_stop_the_priority_or_the_exit():
    api = FakeApi(mmcss=False)
    with ThreadBoost(api=api) as boost:
        assert boost.applied == ["time-critical priority"]
    assert ("leave", 1234) not in api.calls and api.calls[-1][0] == "priority"


def test_failures_are_swallowed():
    api = FakeApi(raises=True)
    with ThreadBoost(api=api) as boost:
        assert boost.applied == []


def test_timer_resolution_is_restored():
    api = FakeApi()
    with TimerResolution(1, api=api):
        pass
    assert api.calls == [("begin", 1), ("end", 1)]
    broken = FakeApi(raises=True)
    with TimerResolution(1, api=broken):
        pass
    assert ("end", 1) not in broken.calls                       # it never began, so it does not end


def test_everything_is_a_no_op_off_windows():
    assert sys.platform != "win32"
    with ThreadBoost() as boost, TimerResolution():
        assert boost.applied == []
    assert AVRT_PRIORITY_HIGH == 2
