"""The engine seen as a PulseAudio server (GUI) and as a PulseAudioManager (daemon)."""
from __future__ import annotations

import pytest

from arctis_sound_manager import pactl
from arctis_sound_manager.win.audio.winpulse import (CHANNEL_NODE, HEADSET_SINK_NAME, MASTER_NODE, NODE_CHANNEL, WinPulse,
                                                     WindowsAudioManager)


class FakeBridge:
    def __init__(self, status=None):
        self.status_value = status
        self.calls: list[tuple] = []

    def status(self, max_age=None):
        return self.status_value

    def running(self):
        return bool(self.status_value and self.status_value.get("running"))

    def volume(self, channel, pct): self.calls.append(("volume", channel, pct))
    def mute(self, channel, muted): self.calls.append(("mute", channel, muted))
    def chatmix(self, c, h, channels=None): self.calls.append(("chatmix", c, h, channels))


def engine_status(**over):
    base = {"running": True,
            "endpoints": {"in:game": {"state": "running"}, "in:chat": {"state": "running"},
                          "in:media": {"state": "waiting"}, "out:headset": {"state": "running"}},
            "volumes": {"game": 70.0, "chat": 100.0, "media": 25.0, "aux": 100.0},
            "muted": {"game": False, "chat": True, "media": False, "aux": False},
            "chatmix": {"channels_pct": 40.0, "chat_pct": 100.0, "channels": ["game", "media"]}}
    base.update(over)
    return base


def test_the_sinks_are_the_configured_channels_with_their_linux_node_names():
    pulse = WinPulse(FakeBridge(engine_status()))
    sinks = pulse.sink_list()
    assert [s.name for s in sinks] == ["Arctis_Game", "Arctis_Chat", "Arctis_Media", HEADSET_SINK_NAME]   # aux has no cable
    game, chat, media, _headset = sinks
    assert game.volume.value_flat == pytest.approx(0.70) and media.volume.value_flat == pytest.approx(0.25)
    assert (game.state, media.state) == ("running", "idle")
    assert chat.mute == 1 and game.mute == 0
    assert game.proplist["node.name"] == "Arctis_Game" and len({s.index for s in sinks}) == 4


def test_without_a_running_engine_there_are_no_sinks():
    assert WinPulse(FakeBridge(None)).sink_list() == []
    assert WinPulse(FakeBridge(engine_status(running=False))).sink_list() == []
    assert WinPulse(FakeBridge(None)).mix_levels() == {"game": None, "chat": None, "media": None, "aux": None}


def test_setting_a_sink_volume_goes_to_the_engine_as_a_percentage():
    bridge = FakeBridge(engine_status())
    pulse = WinPulse(bridge)
    game = pulse.sink_list()[0]
    pulse.volume_set_all_chans(game, 0.42)
    pulse.volume_set_all_chans(game, 7.0)                         # clamped
    pulse.volume_set_all_chans(game, -1)
    assert bridge.calls == [("volume", "game", pytest.approx(42.0)), ("volume", "game", 100.0), ("volume", "game", 0.0)]
    pulse.mute(game, True)
    assert bridge.calls[-1] == ("mute", "game", True)


def test_a_sink_found_by_name_only_is_still_resolved():
    class Named:
        name = "Arctis_Chat"
    bridge = FakeBridge(engine_status())
    WinPulse(bridge).volume_set_all_chans(Named(), 0.5)
    assert bridge.calls == [("volume", "chat", 50.0)]
    bridge.calls.clear()

    class Other:
        name = "alsa_output.something"
    WinPulse(bridge).volume_set_all_chans(Other(), 0.5)
    assert bridge.calls == []


def test_no_applications_and_no_microphone_are_reported():
    pulse = WinPulse(FakeBridge(engine_status()))
    assert pulse.sink_input_list() == [] and pulse.source_list() == [] and pulse.source_output_list() == []
    assert pulse.server_info().default_source_name == ""
    pulse.sink_input_move(1, 2); pulse.event_listen(); pulse.close()                   # harmless no-ops


def test_chatmix_levels_follow_the_dial_channels():
    pulse = WinPulse(FakeBridge(engine_status()))
    assert pulse.mix_levels() == {"game": 40, "chat": 100, "media": 40, "aux": None}   # media rides the dial too; aux has no cable
    other = WinPulse(FakeBridge(engine_status(chatmix={"channels_pct": 40, "chat_pct": 80, "channels": ["game"]})))
    assert other.mix_levels() == {"game": 40, "chat": 80, "media": 100, "aux": None}   # off the dial: back to 100


def test_pactl_defers_the_mix_to_the_engine_adapter():
    bridge = FakeBridge(engine_status())
    pulse = WinPulse(bridge)
    assert pactl.mix_stream_levels(pulse)["game"] == 40
    pactl.apply_mix_to_streams(pulse, 30, 90, ["game", "aux", "chat", "nonsense"])
    assert bridge.calls == [("chatmix", 30, 90, ["game", "aux"])]


def test_pactl_still_drives_real_looking_pulse_objects_the_old_way():
    """A MagicMock has every attribute; the hook must look at the class, so Linux behaviour is untouched."""
    from unittest.mock import MagicMock
    pulse = MagicMock()
    pulse.sink_input_list.return_value = []
    pactl.apply_mix_to_streams(pulse, 30, 90, ["game"])
    pulse.apply_mix.assert_not_called()
    pulse.sink_input_list.assert_called()
    assert pactl.mix_stream_levels(pulse) == {"game": None, "chat": None, "media": None, "aux": None}


def test_the_daemon_manager_sends_chatmix_and_volumes_to_the_engine(monkeypatch):
    bridge = FakeBridge(engine_status())
    manager = WindowsAudioManager(bridge)
    monkeypatch.setattr(WindowsAudioManager, "_chatmix_channels", lambda self: ["game", "media"])
    manager.set_mix(55, 100)
    manager.set_mix(250, 300)                                     # the dial never exceeds 100; be safe anyway
    assert bridge.calls == [("chatmix", 55, 100, ["game", "media"]), ("chatmix", 100, 100, ["game", "media"])]
    bridge.calls.clear()
    assert manager.set_sink_volume_by_node("Arctis_Media", 33)
    assert bridge.calls == [("volume", "media", 33)]
    assert not manager.set_sink_volume_by_node("Arctis_Nothing", 33)
    bridge.status_value = None
    assert not manager.set_sink_volume_by_node("Arctis_Game", 33)                      # engine down: caller retries later


def test_the_daemon_manager_answers_nothing_for_pulseaudio_only_questions():
    manager = WindowsAudioManager(FakeBridge())
    assert manager.get_arctis_sinks() == [] and manager.get_default_device() is None
    assert manager.get_arctis_sinks_classified() == (None, None) and not manager.has_source("x")
    manager.redirect_audio("whatever")
    manager.set_default_source("whatever")


def test_the_chatmix_channels_come_from_the_settings(monkeypatch):
    from arctis_sound_manager.settings import GeneralSettings

    class FakeSettings:
        def chatmix_channels_or_default(self):
            return ["game", "aux"]
    monkeypatch.setattr(GeneralSettings, "read_from_file", classmethod(lambda cls: FakeSettings()))
    assert WindowsAudioManager(FakeBridge())._chatmix_channels() == ["game", "aux"]
    monkeypatch.setattr(GeneralSettings, "read_from_file", classmethod(lambda cls: (_ for _ in ()).throw(OSError("x"))))
    assert WindowsAudioManager(FakeBridge())._chatmix_channels() == ["game"]


def test_node_tables_are_inverse():
    assert {NODE_CHANNEL[n]: n for n in NODE_CHANNEL} == CHANNEL_NODE


def test_the_manager_instance_on_windows_is_the_engine_manager(monkeypatch):
    monkeypatch.setattr(pactl, "IS_WINDOWS", True)
    monkeypatch.setattr(pactl.PulseAudioManager, "_instance", None)
    try:
        manager = pactl.PulseAudioManager.get_instance()
        assert isinstance(manager, WindowsAudioManager)
    finally:
        pactl.PulseAudioManager._instance = None


# ── the headset's master level ──────────────────────────────────────────────

def test_the_headset_output_is_a_sink_the_home_page_takes_for_the_steelseries_one():
    from arctis_sound_manager.gui.home_page import _channel_sink, _headset_sink
    from arctis_sound_manager.pw_utils import is_external_output_sink
    sinks = WinPulse(FakeBridge(engine_status(volumes={"master": 35.0, "game": 70.0}))).sink_list()
    master = _headset_sink(sinks)
    assert master is not None and master.name == HEADSET_SINK_NAME and master.channel == "master"
    assert master.volume.value_flat == pytest.approx(0.35) and master.state == "running"
    assert not any(is_external_output_sink(s) for s in sinks)                # never offered as "another output device"
    assert _channel_sink(sinks, "Arctis_Game").name == "Arctis_Game"         # and the channels did not move


def test_there_is_no_master_without_a_headset_output():
    status = engine_status()
    del status["endpoints"]["out:headset"]
    assert HEADSET_SINK_NAME not in [s.name for s in WinPulse(FakeBridge(status)).sink_list()]
    waiting = engine_status()
    waiting["endpoints"]["out:headset"] = {"state": "waiting"}
    master = WinPulse(FakeBridge(waiting)).sink_list()[-1]
    assert master.name == HEADSET_SINK_NAME and master.state == "idle"


def test_the_master_defaults_to_full_and_follows_mute():
    sinks = WinPulse(FakeBridge(engine_status())).sink_list()
    assert sinks[-1].volume.value_flat == 1.0 and sinks[-1].mute == 0
    muted = WinPulse(FakeBridge(engine_status(muted={"master": True}))).sink_list()[-1]
    assert muted.mute == 1


def test_setting_the_master_volume_and_mute_goes_to_the_engine():
    bridge = FakeBridge(engine_status())
    pulse = WinPulse(bridge)
    master = pulse.sink_list()[-1]
    pulse.volume_set_all_chans(master, 0.5)
    pulse.mute(master, True)
    assert bridge.calls == [("volume", "master", 50.0), ("mute", "master", True)]


def test_a_master_found_by_its_saved_node_name_is_resolved():
    class Named:
        name = MASTER_NODE
    bridge = FakeBridge(engine_status())
    WinPulse(bridge).volume_set_all_chans(Named(), 0.25)
    assert bridge.calls == [("volume", "master", 25.0)]


def test_the_daemon_restores_the_saved_master_level():
    bridge = FakeBridge(engine_status())
    manager = WindowsAudioManager(bridge)
    assert manager.set_sink_volume_by_node(MASTER_NODE, 60)
    assert bridge.calls == [("volume", "master", 60)]
