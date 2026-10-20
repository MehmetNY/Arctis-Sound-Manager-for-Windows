# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""
test_output_device_list.py — no output may vanish from the device pickers.

`node.nick` and `node.description` are both optional PipeWire properties. A
sink missing them used to be dropped from the list silently, which is how an
ordinary output ends up "not in the list" — reported for Bluetooth speakers in
#134 and for desktop speakers in #146. Every sink that passes the filter must
come back with a usable id and a label.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from arctis_sound_manager.dbus_service import build_sink_options


def _sink(node_name: str, nick: str | None = None,
          description: str | None = None, pulse_desc: str = "") -> SimpleNamespace:
    proplist = {"node.name": node_name}
    if nick is not None:
        proplist["node.nick"] = nick
    if description is not None:
        proplist["node.description"] = description
    return SimpleNamespace(name=node_name, proplist=proplist, description=pulse_desc)


def _options(sinks: list, list_name: str) -> list[dict]:
    return build_sink_options(sinks, list_name)


@pytest.mark.parametrize("list_name", ["external_audio_devices", "pulse_audio_devices"])
def test_sink_without_nick_or_description_is_still_listed(list_name):
    """The exact shape that made desktop speakers disappear."""
    result = _options([_sink("alsa_output.pci-0000_0c_00.4.analog-stereo")], list_name)

    assert len(result) == 1, f"{list_name}: sink dropped for lack of metadata"
    assert result[0]["id"] == "alsa_output.pci-0000_0c_00.4.analog-stereo"
    assert result[0]["name"] == "alsa_output.pci-0000_0c_00.4.analog-stereo"


@pytest.mark.parametrize("list_name", ["external_audio_devices", "pulse_audio_devices"])
def test_pulsectl_description_is_used_when_pipewire_has_none(list_name):
    result = _options([_sink("alsa_output.pci-0000_0c_00.4.analog-stereo",
                              pulse_desc="Built-in Audio Analog Stereo")], list_name)

    assert result[0]["name"] == "Built-in Audio Analog Stereo"


@pytest.mark.parametrize("list_name", ["external_audio_devices", "pulse_audio_devices"])
def test_declared_metadata_still_wins(list_name):
    """The fallback must not override a name the device does provide."""
    result = _options([_sink("alsa_output.usb-Burr-Brown", nick="Burr-Brown",
                              description="USB Audio CODEC", pulse_desc="ignored")], list_name)

    assert result[0]["id"] == "Burr-Brown"
    assert result[0]["name"] == "USB Audio CODEC"


def test_external_list_still_filters_out_virtual_nodes():
    """Widening the fallback must not let ASM's own nodes into the list."""
    result = _options([
        _sink("alsa_output.pci-0000_0c_00.4.analog-stereo"),
        _sink("Arctis_Game"),
        _sink("effect_input.sonar-game-eq"),
    ], "external_audio_devices")

    assert [r["id"] for r in result] == ["alsa_output.pci-0000_0c_00.4.analog-stereo"]


def test_duplicate_ids_are_not_repeated():
    result = _options([_sink("alsa_output.one", nick="dup"),
                        _sink("alsa_output.one", nick="dup")], "external_audio_devices")

    assert len(result) == 1


@pytest.mark.parametrize("list_name", ["external_audio_devices", "pulse_audio_devices"])
def test_shared_nick_lists_every_sink_by_node_name(list_name):
    """Every output of one USB card is "USB Audio" (#339)."""
    names = ["alsa_output.usb-Generic_USB_Audio-00.HiFi__SPDIF__sink",
             "alsa_output.usb-Generic_USB_Audio-00.HiFi__Speaker__sink",
             "alsa_output.usb-Generic_USB_Audio-00.HiFi__Headphones__sink"]
    result = _options([_sink(n, nick="USB Audio", description=n.split("__")[1])
                       for n in names] + [_sink("alsa_output.pci", nick="Built-in")],
                      list_name)

    assert [r["id"] for r in result] == names + ["Built-in"]
    assert [r["name"] for r in result][:3] == ["SPDIF", "Speaker", "Headphones"]
