# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""
test_eq_page_range.py — the custom EQ sliders reach what the headset takes.

The Elite family's on-device curve goes to ±12 dB (#327); every other family
stops at ±10. The sliders and the scale beside them follow the headset, on
ASM's one scale of half-decibel steps with 20 = 0 dB.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_slider_range_follows_the_headset(qapp):
    from arctis_sound_manager.gui.equalizer_page import _EqSlider, _ScaleWidget

    slider = _EqSlider(0, "32")
    emitted = []
    slider.value_changed.connect(lambda i, raw: emitted.append(raw))

    slider.set_range_db(12)
    slider.set_raw_value(44)
    assert slider._slider.value() == 24 and slider._val_edit.text() == "+12.0"
    slider.set_raw_value(20)
    slider._val_edit.setText("15")
    slider._on_value_edited()
    assert emitted[-1] == 44                      # clamped to +12, not +15

    slider.set_range_db(10)
    assert slider._slider.maximum() == 20
    assert slider._slider.value() == 20           # +12 no longer fits

    scale = _ScaleWidget()
    scale.set_range_db(12)
    assert [t for _, t in scale._labels()] == ["+12", "+6", "0", "-6", "-12"]
    scale.set_range_db(10)
    assert [t for _, t in scale._labels()] == ["+10", "+5", "0", "-5", "-10"]
