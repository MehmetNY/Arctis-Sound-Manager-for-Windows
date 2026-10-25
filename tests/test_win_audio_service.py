"""The daemon starts the audio engine when it is wanted and ready, and stops only an engine it started."""
from __future__ import annotations

import logging

import pytest

from arctis_sound_manager.win.audio import service
from arctis_sound_manager.win.audio.engine_config import EngineConfig, save_config

LOG = logging.getLogger("test")


class FakeClient:
    def __init__(self, up=False):
        self.up = up

    def available(self):
        return self.up


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(service, "_started_by_us", False)
    monkeypatch.setattr(service, "engine_enabled", lambda: True)


def ready_config(tmp_path):
    save_config(EngineConfig(inputs={"game": "CABLE-A Output", "chat": "", "media": "", "aux": ""},
                             outputs={"headset": "Arctis Nova Pro"}), tmp_path)


@pytest.fixture
def launched(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "start_engine", lambda cfg, **kw: calls.append(("start", cfg, kw)) or False)
    monkeypatch.setattr(service, "stop_engine", lambda cfg, **kw: calls.append(("stop", cfg)) or True)
    return calls


def test_a_ready_configuration_starts_the_engine_without_holding_up_the_daemon(tmp_path, monkeypatch, launched):
    ready_config(tmp_path)
    monkeypatch.setattr(service, "EngineClient", lambda cfg, timeout: FakeClient(False))
    service.start_engine_for_daemon(LOG, tmp_path)
    (kind, cfg, kw), = launched
    assert kind == "start" and cfg == tmp_path and kw["wait"] == 0.0
    assert service._started_by_us


def test_without_a_configuration_the_engine_is_still_started_and_sets_itself_up(tmp_path, monkeypatch, launched, caplog):
    monkeypatch.setattr(service, "EngineClient", lambda cfg, timeout: FakeClient(False))
    with caplog.at_level(logging.INFO):
        service.start_engine_for_daemon(LOG, tmp_path)
    (kind, _cfg, _kw), = launched
    assert kind == "start" and service._started_by_us
    assert "sets itself up" in caplog.text


def test_when_switched_off_nothing_is_started(tmp_path, monkeypatch, launched):
    ready_config(tmp_path)
    monkeypatch.setattr(service, "engine_enabled", lambda: False)
    assert not service.start_engine_for_daemon(LOG, tmp_path)
    assert launched == []


def test_an_engine_that_is_already_running_is_left_alone_and_not_stopped_later(tmp_path, monkeypatch, launched):
    ready_config(tmp_path)
    monkeypatch.setattr(service, "EngineClient", lambda cfg, timeout: FakeClient(True))
    assert service.start_engine_for_daemon(LOG, tmp_path)
    assert launched == [] and not service._started_by_us
    assert not service.stop_engine_for_daemon(LOG, tmp_path)
    assert launched == []


def test_the_engine_the_daemon_started_is_stopped_with_it(tmp_path, monkeypatch, launched):
    ready_config(tmp_path)
    monkeypatch.setattr(service, "EngineClient", lambda cfg, timeout: FakeClient(False))
    service.start_engine_for_daemon(LOG, tmp_path)
    assert service.stop_engine_for_daemon(LOG, tmp_path)
    assert launched[-1][0] == "stop" and not service._started_by_us
    assert not service.stop_engine_for_daemon(LOG, tmp_path)                 # only once


def test_the_setting_decides(monkeypatch):
    monkeypatch.undo()
    from arctis_sound_manager.settings import GeneralSettings

    class S:
        windows_audio_engine = False
    monkeypatch.setattr(GeneralSettings, "read_from_file", classmethod(lambda cls: S()))
    assert service.engine_enabled() is False
    monkeypatch.setattr(GeneralSettings, "read_from_file", classmethod(lambda cls: (_ for _ in ()).throw(OSError("x"))))
    assert service.engine_enabled() is True                                  # unreadable settings: the default (on)


def test_the_setting_exists_and_defaults_on():
    from arctis_sound_manager.settings import GeneralSettings
    assert GeneralSettings().windows_audio_engine is True


def test_the_daemons_spatial_apply_just_reloads_the_engine_on_windows(monkeypatch):
    from types import SimpleNamespace
    from arctis_sound_manager import core
    from arctis_sound_manager.win.audio import apply as win_apply
    reloads = []
    monkeypatch.setattr(core, "IS_WINDOWS", True)
    monkeypatch.setattr(win_apply, "reload_engine", lambda: reloads.append(1))
    fake_self = SimpleNamespace(_spatial_apply_lock=None, logger=LOG)             # nothing else may be touched
    core.CoreEngine.apply_spatial_audio(fake_self)
    assert reloads == [1]


def test_switching_the_engine_on_starts_it_when_it_is_ready(tmp_path, monkeypatch, launched):
    ready_config(tmp_path)
    monkeypatch.setattr(service, "EngineClient", lambda cfg, timeout: FakeClient(False))
    service.apply_engine_setting(LOG, True, tmp_path)
    assert [c[0] for c in launched] == ["start"] and service._started_by_us


def test_switching_it_on_without_a_configuration_still_starts_it_and_it_sets_itself_up(tmp_path, monkeypatch, launched):
    monkeypatch.setattr(service, "EngineClient", lambda cfg, timeout: FakeClient(False))
    service.apply_engine_setting(LOG, True, tmp_path)
    assert [c[0] for c in launched] == ["start"]


def test_switching_the_engine_off_stops_even_one_the_daemon_did_not_start(tmp_path, launched, caplog):
    assert not service._started_by_us                                         # somebody launched it by hand
    with caplog.at_level(logging.INFO):
        assert not service.apply_engine_setting(LOG, False, tmp_path)
    assert launched == [("stop", tmp_path)]
    assert "switched off" in caplog.text and "stopped" in caplog.text


def test_switching_it_off_forgets_that_the_daemon_started_it(tmp_path, monkeypatch, launched):
    ready_config(tmp_path)
    monkeypatch.setattr(service, "EngineClient", lambda cfg, timeout: FakeClient(False))
    service.apply_engine_setting(LOG, True, tmp_path)
    assert service._started_by_us
    service.apply_engine_setting(LOG, False, tmp_path)
    assert not service._started_by_us
    assert not service.stop_engine_for_daemon(LOG, tmp_path)                 # nothing left to stop at exit
    assert [c[0] for c in launched] == ["start", "stop"]


def test_switching_off_with_no_engine_running_is_quiet(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(service, "stop_engine", lambda cfg, **kw: False)
    with caplog.at_level(logging.INFO):
        assert not service.apply_engine_setting(LOG, False, tmp_path)
    assert "none was running" in caplog.text
