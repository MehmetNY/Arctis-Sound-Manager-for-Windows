# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""
test_restore_channel_streams.py — apps go back to their channel on reconnect.

When the dongle goes away the Arctis_* sinks disappear and every app falls
back to another output. Only apps pinned in routing_overrides.json used to
come back; a game left on Game by default stayed on the monitor (#343).
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from arctis_sound_manager import pw_utils


class _Pulse:
    def __init__(self, sinks, inputs):
        self.sinks, self.inputs, self.moves = sinks, inputs, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sink_list(self):
        return self.sinks

    def sink_input_list(self):
        return self.inputs

    def sink_input_move(self, si, sink):
        self.moves.append((si, sink))


def _sink(i, name):
    return SimpleNamespace(index=i, name=name)


def _input(i, app, binary, sink):
    return SimpleNamespace(index=i, sink=sink, proplist={
        "application.name": app, "application.process.binary": binary})


@pytest.fixture
def pulse(monkeypatch, tmp_path):
    monkeypatch.setattr(pw_utils, "OVERRIDES_FILE", tmp_path / "none.json")
    holder = {}

    def make(sinks, inputs):
        holder["p"] = _Pulse(sinks, inputs)
        monkeypatch.setitem(sys.modules, "pulsectl",
                            SimpleNamespace(Pulse=lambda *_: holder["p"]))
        return holder["p"]
    return make


def test_snapshot_keeps_only_apps_on_a_channel(pulse):
    pulse([_sink(1, "Arctis_Game"), _sink(2, "alsa_output.hdmi")],
          [_input(10, "THE FINALS", "Discovery.exe", 1),
           _input(11, "Firefox", "firefox", 2),
           _input(12, "loop", "pw-loopback", 1)])
    assert pw_utils.snapshot_channel_streams() == {"THE FINALS": "Arctis_Game"}


def test_restore_moves_back_only_apps_outside_asm(pulse):
    p = pulse([_sink(5, "Arctis_Game"), _sink(6, "Arctis_Media"), _sink(2, "alsa_output.hdmi")],
              [_input(10, "THE FINALS", "Discovery.exe", 2),
               _input(11, "Spotify", "spotify", 6)])
    moved = pw_utils.restore_channel_streams(
        {"THE FINALS": "Arctis_Game", "Spotify": "Arctis_Game"}, timeout_s=0)
    assert moved == 1 and p.moves == [(10, 5)]


def test_a_pinned_app_is_left_to_the_overrides(pulse, tmp_path):
    (tmp_path / "none.json").write_text('{"THE FINALS": "Arctis_Media"}')
    p = pulse([_sink(5, "Arctis_Game"), _sink(2, "alsa_output.hdmi")],
              [_input(10, "THE FINALS", "Discovery.exe", 2)])
    assert pw_utils.restore_channel_streams({"THE FINALS": "Arctis_Game"}, timeout_s=0) == 0
    assert p.moves == []
