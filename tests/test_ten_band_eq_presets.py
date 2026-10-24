# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The Nova Elite's microphone and Bluetooth EQ presets went out as 0x2E and
0x2F, opcodes no SteelSeries spec gives this family (the Nova Pro's, by
analogy), so picking one did nothing. GG selects them by writing the whole
preset — ten_band_eq_mic (0x1D) / ten_band_eq_bt (0x1F): slot, alias, name,
ten gains — with the curves from its engine database. The Omni gets the same
two controls, and its own Walkie Talkie slot.

The layout is checked against what the base stations return for the same
frames (read_microphone_eq_settings 0x1C, read_bluetooth_eq_settings 0x1E),
from the Elite and Omni probes posted on #331.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from ruamel.yaml import YAML

from arctis_sound_manager.config import DeviceConfiguration
from arctis_sound_manager.core import CoreEngine
from arctis_sound_manager.hardware_eq import (BLUETOOTH_PRESETS, MIC_PRESETS,
                                              encode_ten_band_eq)

ROOT = Path(__file__).parent
DEVICES_DIR = ROOT.parent / "src" / "arctis_sound_manager" / "devices"


def _probe(name: str) -> dict[str, list[int]]:
    raw = json.loads((ROOT / "data" / f"{name}_probe_331.json").read_text())
    return {k: [int(b, 16) for b in v.split()] for k, v in raw.items()}


def _preset(presets, name):
    return next(p for p in presets if p.name == name)


def test_a_mic_preset_frame_matches_what_the_base_reads_back():
    """Elite, microphone on Flat: read back as 1c 00 FLAT… Flat… 10 zeros."""
    reply = _probe("elite")["read_microphone_eq_settings"]
    frame = encode_ten_band_eq(0x1D, _preset(MIC_PRESETS, "Flat"))

    assert frame[:2] == [0x01, 0x1D]
    # The reply has no report id and answers with the read opcode (0x1C).
    assert frame[2:] == reply[1:79]


def test_a_bluetooth_preset_frame_matches_the_header_the_base_reads_back():
    """Elite, Bluetooth on Bass Boost: slot 1, BASS, Bass Boost. The gains it
    reads back are the firmware's own curve, not GG's, so only the header is
    compared."""
    reply = _probe("elite")["read_bluetooth_eq_settings"]
    frame = encode_ten_band_eq(0x1F, _preset(BLUETOOTH_PRESETS, "Bass Boost"))

    assert frame[2:70] == reply[1:69]


def test_gains_are_signed_tenths_of_a_db():
    walkie = encode_ten_band_eq(0x1D, _preset(MIC_PRESETS, "Walkie Talkie"))
    assert walkie[2] == 9
    assert walkie[-10:] == [0x88, 0x88, 0x88, 0x88, 0x32, 0x3C, 0x3C, 0x32, 0x88, 0x88]


@pytest.mark.parametrize("profile, setting, value, command, slot", [
    ("nova_elite", "equalizer_mic", 0x06, 0x1D, 6),
    ("nova_elite", "equalizer_bluetooth", 0x02, 0x1F, 2),
    ("nova_pro_omni", "equalizer_mic", 0x09, 0x1D, 9),
    ("nova_pro_omni", "equalizer_bluetooth", 0x03, 0x1F, 3),
])
def test_picking_a_preset_sends_the_feature_report(profile, setting, value, command, slot):
    engine = MagicMock()
    engine.device_config = DeviceConfiguration(YAML(typ="safe").load(DEVICES_DIR / f"{profile}.yaml"))
    engine._send_setting.side_effect = lambda c, v: CoreEngine._send_setting(engine, c, v)
    engine._send_ten_band_preset.side_effect = (
        lambda n, t, v: CoreEngine._send_ten_band_preset(engine, n, t, v))

    CoreEngine.on_setting_changed(engine, setting, value)

    engine.send_command.assert_not_called()
    frame, size = engine.send_feature_report.call_args.args
    assert (frame[:3], size) == ([0x01, command, slot], 1036)


def test_the_elite_no_longer_offers_controls_with_no_command():
    """Wireless mode and the base's volume: the spec has no write for either,
    and GG shows neither for this family. They stay readouts."""
    cfg = DeviceConfiguration(YAML(typ="safe").load(DEVICES_DIR / "nova_elite.yaml"))
    names = {c.name for section in cfg.settings.values() for c in section}

    assert not names & {"wireless_mode", "station_volume"}
