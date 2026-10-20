# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""
test_hardware_eq_encoding.py — the on-device parametric EQ wire format.

Every expectation here is a value SteelSeries' own device specification states
outright, so this stands in for hardware we don't have: the gain encoding
(0 dB = 0x00, 12 dB = 0x78, -12 dB = 0x88), the Q encoding (2.5 → 0x09C4 sent
low byte first), and the three-frame sequence the device needs before it
applies anything.
"""
from __future__ import annotations

import pytest

from arctis_sound_manager.hardware_eq import (CONNECTION_BLUETOOTH,
                                              CONNECTION_WIRELESS,
                                              DEFAULT_BAND_FREQUENCIES,
                                              FILTER_PEAK, EqBand,
                                              bands_from_gains, encode_band,
                                              encode_parametric_eq)


# ── Band encoding ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gain_db,expected", [
    (0.0, 0x00),
    (12.0, 0x78),    # spec: "12.0dB = 0x78"
    (-12.0, 0x88),   # spec: "-12.0dB = 0x88"
    (6.0, 0x3C),
    (-6.0, 0xC4),
    (0.5, 0x05),     # decidecibels, so half a dB is representable
])
def test_gain_is_signed_decidecibels(gain_db, expected):
    encoded = encode_band(EqBand(frequency=1000, gain_db=gain_db))
    assert encoded[3] == expected


@pytest.mark.parametrize("q,expected", [
    (2.5, bytes([0xC4, 0x09])),   # spec: "2.5 = 0x09C4", stored little-endian
    (1.5, bytes([0xDC, 0x05])),   # spec: "1.5 = 0x05DC"
    (0.8, bytes([0x20, 0x03])),   # spec: "0.8 = 0x0320"
])
def test_q_factor_is_thousandths_little_endian(q, expected):
    encoded = encode_band(EqBand(frequency=1000, gain_db=0, q=q))
    assert encoded[4:6] == expected


def test_band_is_six_bytes_in_order():
    encoded = encode_band(EqBand(frequency=1000, gain_db=0.0, q=1.41))
    assert len(encoded) == 6
    assert int.from_bytes(encoded[0:2], "little") == 1000
    assert encoded[2] == FILTER_PEAK


def test_values_are_clamped_to_what_the_field_holds():
    """A slider can't push the device outside its declared ranges."""
    encoded = encode_band(EqBand(frequency=99999, gain_db=99.0, q=99.0))
    assert int.from_bytes(encoded[0:2], "little") == 20000
    assert int.from_bytes(encoded[3:4], "little", signed=True) == 127
    assert int.from_bytes(encoded[4:6], "little") == 10000


# ── Frame sequence ───────────────────────────────────────────────────────────

def test_frames_start_with_the_opcode_not_a_report_id():
    """The `report_id` field the specs declare is the HID report number.

    It travels in the SET_REPORT wValue (CoreEngine.send_command, from the
    profile's `command_report_id`), never as payload. Emitting it shifted every
    frame one byte right, the headset read opcode 0x00 and dropped the curve in
    silence on every parametric family (#146). A real Nova 7P Gen 2 replies to
    the readback queries, which have never carried the byte.
    """
    frames = encode_parametric_eq(bands_from_gains([0.0] * 10), name="Flat",
                                  commit_command=0x27)

    assert [frame[0] for frame in frames] == [0xA7, 0x33, 0x27]
    assert all(frame[0] != 0x00 for frame in frames)


def test_frames_come_in_the_order_the_device_expects():
    frames = encode_parametric_eq(bands_from_gains([0.0] * 10), name="Flat",
                                  commit_command=0x27)

    assert len(frames) == 3
    assert frames[0][0] == 0xA7, "first frame carries the name"
    assert frames[1][0] == 0x33, "second frame carries the bands"
    assert frames[2][0] == 0x27, "third frame commits"


def test_commit_frame_is_absent_unless_asked_for():
    """The Nova 5 declares no commit opcode; omission must not invent one."""
    frames = encode_parametric_eq(bands_from_gains([0.0] * 10),
                                  name_command=0xA5)

    assert len(frames) == 2
    assert [f[0] for f in frames] == [0xA5, 0x33]


def test_name_frame_carries_connection_slot_and_ascii_name():
    frames = encode_parametric_eq(bands_from_gains([0.0] * 10), name="Flat")
    name_frame = frames[0]

    assert name_frame[1] == CONNECTION_WIRELESS
    assert name_frame[2] == 0x01  # custom preset slot
    assert bytes(name_frame[3:]) == b"Flat"


def test_name_frame_matches_the_layout_the_headset_reports_back():
    """read_eq_preset_name's reply is [0xA6, connection, preset_type, name].

    The write is the same shape one opcode over, so the captured reply in
    tests/test_hardware_eq_readback.py doubles as a check on this frame.
    """
    frames = encode_parametric_eq(bands_from_gains([0.0] * 10), name="Custom")

    assert frames[0][:3] == [0xA7, CONNECTION_WIRELESS, 0x01]


def test_band_frame_holds_ten_encoded_bands():
    frames = encode_parametric_eq(bands_from_gains([0.0] * 10))
    band_frame = frames[1]

    assert band_frame[1] == CONNECTION_WIRELESS
    assert len(band_frame) - 2 == 10 * 6


def test_band_frame_fits_the_64_byte_report():
    """Two header bytes plus sixty of curve — the size the headset echoes back.

    With the report id in the payload this was 63 bytes of a 64-byte report,
    which still padded cleanly: nothing about the length gave the extra byte
    away.
    """
    frames = encode_parametric_eq(bands_from_gains([0.0] * 10))

    assert len(frames[1]) == 62
    assert len(frames[1]) <= 64


def test_gains_land_in_the_band_frame_in_order():
    gains = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    frames = encode_parametric_eq(bands_from_gains(gains))
    payload = frames[1][2:]

    for index, gain in enumerate(gains):
        assert payload[index * 6 + 3] == int(gain * 10)


def test_bluetooth_curve_targets_its_own_connection():
    frames = encode_parametric_eq(bands_from_gains([0.0] * 10),
                                  connection=CONNECTION_BLUETOOTH)
    assert frames[0][1] == CONNECTION_BLUETOOTH
    assert frames[1][1] == CONNECTION_BLUETOOTH


def test_default_frequencies_match_the_sliders():
    bands = bands_from_gains([0.0] * 10)
    assert tuple(b.frequency for b in bands) == DEFAULT_BAND_FREQUENCIES


def test_wrong_band_count_is_rejected():
    with pytest.raises(ValueError):
        encode_parametric_eq(bands_from_gains([0.0] * 9))


def test_non_ascii_name_does_not_break_the_frame():
    frames = encode_parametric_eq(bands_from_gains([0.0] * 10), name="Café ✨")
    assert all(0 <= b <= 255 for b in frames[0])


def test_band_frame_omits_the_connection_when_the_family_does():
    """Nova 5 / GameBuds: an extra byte here shifts the whole curve."""
    with_conn = encode_parametric_eq(bands_from_gains([0.0] * 10))
    without = encode_parametric_eq(bands_from_gains([0.0] * 10),
                                   bands_carry_connection=False)

    assert len(with_conn[1]) - len(without[1]) == 1
    assert without[1][:1] == [0x33]
    assert with_conn[1][:2] == [0x33, CONNECTION_WIRELESS]
    # The bands themselves are identical, only their offset differs.
    assert without[1][1:] == with_conn[1][2:]


# ── Nova Elite (parametric_eq 0x1B, base_arctis_nova_elite_tx.device) ───────

def test_elite_frame_layout_follows_the_spec():
    """report_id, 0x1B, slot, alias (6), name (61), ten 6-byte bands."""
    from arctis_sound_manager.hardware_eq import (bands_from_gains,
                                                   encode_elite_parametric_eq)
    (frame,) = encode_elite_parametric_eq(bands_from_gains([6] + [0] * 8 + [-6]))
    assert len(frame) == 3 + 6 + 61 + 60
    assert frame[:3] == [0x01, 0x1B, 0x04]
    assert bytes(frame[3:9]) == b"Custom"
    assert bytes(frame[9:70]).rstrip(b"\x00") == b"Custom"
    # 31 Hz, peak, +6.0 dB (0x3C), Q 1.41 (1410 = 0x0582), little-endian.
    assert frame[70:76] == [0x1F, 0x00, 0x01, 0x3C, 0x82, 0x05]
    # 16 kHz, -6.0 dB as a signed byte (0xC4).
    assert frame[124:130] == [0x80, 0x3E, 0x01, 0xC4, 0x82, 0x05]


def test_elite_builtin_slot_comes_from_the_name():
    """preset_type 0 picks the onboard slot by name, as the spec's cond does."""
    from arctis_sound_manager.hardware_eq import (PRESET_BUILTIN, bands_from_gains,
                                                   encode_elite_parametric_eq)
    (frame,) = encode_elite_parametric_eq(bands_from_gains([0] * 10), name="Flat",
                                          preset_type=PRESET_BUILTIN)
    assert frame[2] == 0x00


def test_elite_readback_finds_the_echo_after_the_report_id():
    """A numbered feature report starts with its report id; the spec's struct
    starts at the 0x1A echo. Both layouts decode the same."""
    from arctis_sound_manager.hardware_eq import (bands_from_gains,
                                                   decode_elite_eq_settings,
                                                   encode_elite_parametric_eq)
    (frame,) = encode_elite_parametric_eq(bands_from_gains([6] + [0] * 8 + [-6]))
    body = [0x1A, 0x04] + frame[3:]
    for reply in (body + [0] * 900, [0x01] + body + [0] * 900):
        decoded = decode_elite_eq_settings(reply)
        assert decoded["slot"] == 4
        assert decoded["name"] == "Custom"
        assert decoded["bands"][0].gain_db == 6.0
        assert decoded["bands"][9].frequency == 16000


def test_elite_readback_without_echo_is_refused():
    import pytest
    from arctis_sound_manager.hardware_eq import decode_elite_eq_settings
    with pytest.raises(ValueError):
        decode_elite_eq_settings([0x01, 0x20] + [0] * 200)


def test_shelving_layout_matches_gg_frames():
    """GG's Bass Boost on an Omni (#327): low shelf, eight peaks, high shelf,
    32/64 Hz, Q 1.414 — byte for byte what its 0x1B frame carries."""
    from arctis_sound_manager.hardware_eq import (FILTER_HIGH_SHELF, FILTER_LOW_SHELF,
                                                  FILTER_PEAK, encode_band,
                                                  shelving_bands_from_gains)
    bands = shelving_bands_from_gains([3.5, 5.5, 4, 1, -1.5, -1.5, -1, -1, -1, -1])

    assert [b.filter_type for b in bands] == (
        [FILTER_LOW_SHELF] + [FILTER_PEAK] * 8 + [FILTER_HIGH_SHELF])
    assert [b.frequency for b in bands][:2] == [32, 64]
    assert list(encode_band(bands[0])) == [0x20, 0x00, 0x04, 0x23, 0x86, 0x05]
    assert list(encode_band(bands[9])) == [0x80, 0x3E, 0x05, 0xF6, 0x86, 0x05]


def test_elite_family_profiles_use_the_shelving_layout():
    from pathlib import Path
    from ruamel.yaml import YAML
    devices = Path(__file__).parent.parent / "src" / "arctis_sound_manager" / "devices"
    for name in ("nova_elite.yaml", "nova_pro_omni.yaml"):
        profile = YAML(typ="safe").load((devices / name).read_text())
        eq = profile["device"]["hardware_eq"] if "device" in profile else profile["hardware_eq"]
        assert eq["band_layout"] == "shelving", name
