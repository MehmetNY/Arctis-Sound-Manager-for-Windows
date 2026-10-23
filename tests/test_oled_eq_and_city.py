# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""What the station screen says about the EQ and the weather (#330).

Reported on a Nova Pro Omni: with the on-device Custom EQ playing, the screen
kept naming the channel's Sonar preset; and "show city name" did nothing,
because a city too long to sit beside the temperature went on a line below the
end of the drawn buffer.
"""
from __future__ import annotations

import pytest

from arctis_sound_manager.oled_manager import OledManager
from arctis_sound_manager.oled_renderer import OledRenderer
from arctis_sound_manager.weather_service import WeatherData
from tests.test_oled_battery_states import _Core


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    monkeypatch.setattr(OledManager, "set_brightness", lambda *a, **k: None)


@pytest.fixture
def presets(monkeypatch, tmp_path):
    (tmp_path / ".sonar_preset_game").write_text("Flat")
    (tmp_path / ".sonar_preset_chat").write_text("Voice Boost")
    monkeypatch.setattr("arctis_sound_manager.oled_manager._CFG", tmp_path)


def _manager(hardware_eq: bool, sonar: bool) -> OledManager:
    core = _Core({"raw": 1})
    core.hardware_eq = hardware_eq
    core.eq_mode_sonar = sonar
    return OledManager(core)


def test_custom_mode_names_the_on_device_eq(presets):
    manager = _manager(hardware_eq=True, sonar=False)

    assert manager._displayed_eq("game") == ("EQ", "Custom")
    assert manager._hardware_eq_active() is True


def test_sonar_mode_names_the_channel_preset(presets):
    manager = _manager(hardware_eq=True, sonar=True)

    assert manager._displayed_eq("game") == ("Game", "Flat")


def test_a_headset_without_hardware_eq_is_always_sonar(presets):
    """No on-device EQ means nothing but Sonar can be playing, whatever the
    mode file says."""
    manager = _manager(hardware_eq=False, sonar=False)

    assert manager._displayed_eq("game") == ("Game", "Flat")
    assert manager._hardware_eq_active() is False


def _weather(city: str) -> WeatherData:
    return WeatherData(temp=23.4, unit_label="°F", condition="Cloudy",
                       icon_id=1, city=city)


def _render(city: str, show_city: bool):
    image, _ = OledRenderer().render_status_image(
        battery_percent=80, charging=False, time_str="14:05",
        active_profile="Default", eq_preset="Flat",
        weather=_weather(city), show_weather_city=show_city)
    return image


def test_a_long_city_name_is_inside_the_image():
    """'Fort Worth' does not fit beside '23.4°F': it goes on its own line, and
    that line must be part of the buffer, or it is never on screen."""
    with_city = _render("Fort Worth", show_city=True)
    without = _render("Fort Worth", show_city=False)

    assert with_city.height > without.height
    # Something is drawn in the rows the city line added.
    assert with_city.crop((0, without.height, with_city.width, with_city.height)).getbbox()


def test_the_city_toggle_changes_the_image():
    assert _render("Paris", True).tobytes() != _render("Paris", False).tobytes()
    assert _render("Fort Worth", True).tobytes() != _render("Fort Worth", False).tobytes()


def test_elite_family_draws_below_the_status_strip():
    """GG draws 128x52 at y=12 on the Elite and the Omni; the base's own
    status strip above must be left alone, or the Omni redraws its whole
    screen over ASM's whenever its charging indicator changes (#330)."""
    from pathlib import Path

    from ruamel.yaml import YAML

    from arctis_sound_manager.config import DeviceConfiguration
    from arctis_sound_manager.oled_protocol import OledProtocol

    devices = Path(__file__).parent.parent / "src" / "arctis_sound_manager" / "devices"
    for name in ("nova_elite.yaml", "nova_pro_omni.yaml"):
        oled = DeviceConfiguration(YAML(typ="safe").load(devices / name)).oled
        assert (oled.width, oled.height, oled.y_offset) == (128, 52, 12), name
        proto = OledProtocol(report_id=1, width=128, height=52,
                             frame_report_size=1036, y_offset=12)
        packets = proto.build_frame_packets(bytes(16 * 52), 128, 52)
        # Two halves, as GG's sub-payload split: x, y=12, w=64, h padded to 56.
        assert [p[:6] for p in packets] == [[1, 0x93, 0, 12, 64, 56],
                                            [1, 0x93, 64, 12, 64, 56]]
