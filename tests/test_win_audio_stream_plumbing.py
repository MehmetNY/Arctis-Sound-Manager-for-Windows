"""Streamer mode on Windows: the daemon and the stream-mix module hand over to the audio engine."""
from __future__ import annotations

import logging
import threading
from unittest.mock import patch

import pytest

from arctis_sound_manager import core, stream_mix
from arctis_sound_manager.core import CoreEngine
from arctis_sound_manager.settings import GeneralSettings


class FakeBridge:
    def __init__(self, fail=False):
        self.reloads = 0
        self.fail = fail

    def reload(self):
        if self.fail:
            raise RuntimeError("no bridge")
        self.reloads += 1


@pytest.fixture
def bridge(monkeypatch):
    fake = FakeBridge()
    monkeypatch.setattr("arctis_sound_manager.win.audio.bridge.shared_bridge", lambda: fake)
    return fake


# ── stream_mix.apply_stream_mix ──────────────────────────────────────────────

def test_on_windows_applying_the_stream_mix_asks_the_engine_to_reload(monkeypatch, bridge):
    monkeypatch.setattr(stream_mix, "IS_WINDOWS", True)
    with patch("arctis_sound_manager.pw_utils.set_filter_controls") as pipewire:
        assert stream_mix.apply_stream_mix() is True
    assert bridge.reloads == 1
    pipewire.assert_not_called()                                                 # there is no PipeWire graph to push into


def test_on_windows_a_failing_bridge_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(stream_mix, "IS_WINDOWS", True)
    monkeypatch.setattr("arctis_sound_manager.win.audio.bridge.shared_bridge", lambda: FakeBridge(fail=True))
    assert stream_mix.apply_stream_mix() is False


def test_on_linux_applying_the_stream_mix_still_goes_to_pipewire(monkeypatch, bridge):
    monkeypatch.setattr(stream_mix, "IS_WINDOWS", False)
    with patch("arctis_sound_manager.pw_utils.set_filter_controls", return_value=True) as pipewire:
        assert stream_mix.apply_stream_mix({"game": {"volume": 50, "muted": False}}) is True
    pipewire.assert_called_once()
    assert bridge.reloads == 0


# ── CoreEngine.apply_streamer_mode ───────────────────────────────────────────

def make_core():
    engine = CoreEngine.__new__(CoreEngine)
    engine.logger = logging.getLogger("test_win_audio_stream_plumbing")
    engine._streamer_apply_lock = threading.Lock()
    return engine


def test_on_windows_toggling_streamer_mode_reloads_the_engine_and_touches_no_filter_chain(monkeypatch, bridge):
    monkeypatch.setattr(core, "IS_WINDOWS", True)
    with patch("arctis_sound_manager.sonar_to_pipewire.sync_stream_mix_conf") as conf, \
         patch("arctis_sound_manager.sonar_to_pipewire._restart_filter_chain") as restart:
        make_core().apply_streamer_mode()
    assert bridge.reloads == 1
    conf.assert_not_called()
    restart.assert_not_called()


def test_on_windows_the_lock_is_released_afterwards(monkeypatch, bridge):
    monkeypatch.setattr(core, "IS_WINDOWS", True)
    engine = make_core()
    engine.apply_streamer_mode()
    engine.apply_streamer_mode()
    assert bridge.reloads == 2 and not engine._streamer_apply_lock.locked()


def test_on_windows_a_failing_bridge_does_not_raise(monkeypatch):
    monkeypatch.setattr(core, "IS_WINDOWS", True)
    monkeypatch.setattr("arctis_sound_manager.win.audio.bridge.shared_bridge", lambda: FakeBridge(fail=True))
    engine = make_core()
    engine.apply_streamer_mode()
    assert not engine._streamer_apply_lock.locked()


def test_on_linux_toggling_streamer_mode_still_rebuilds_the_filter_chain(monkeypatch, bridge, tmp_path):
    monkeypatch.setattr(core, "IS_WINDOWS", False)
    with patch("arctis_sound_manager.settings.SETTINGS_FOLDER", tmp_path), \
         patch("arctis_sound_manager.sonar_to_pipewire.sync_stream_mix_conf", return_value=True) as conf, \
         patch("arctis_sound_manager.sonar_to_pipewire._restart_filter_chain") as restart:
        gs = GeneralSettings()
        gs.streamer_mode = False
        gs.write_to_file()
        make_core().apply_streamer_mode()
    conf.assert_called_once_with(False)
    restart.assert_called_once()
    assert bridge.reloads == 0
