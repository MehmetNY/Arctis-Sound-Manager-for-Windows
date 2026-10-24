# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Scrolling a settings page must not change a setting.

A stock QSlider/QSpinBox takes the wheel while the pointer is over it and every notch is a real
valueChanged that is sent to the headset. Scrolling down the DAC page past "screen timeout" changed
the screen timeout - one of the ways the DAC settings "changed themselves".
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QSlider, QSpinBox

from arctis_sound_manager.gui.qt_widgets.no_wheel_slider import NoWheelSlider, NoWheelSpinBox
from arctis_sound_manager.settings import GeneralSettings


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wheel(widget, delta=120):
    event = QWheelEvent(QPointF(5, 5), widget.mapToGlobal(QPointF(5, 5)), QPoint(0, 0), QPoint(0, delta),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                        False)
    QApplication.sendEvent(widget, event)
    return event


def test_a_stock_slider_does_take_the_wheel_which_is_the_problem(qapp):
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 300)
    slider.setSingleStep(10)
    slider.setValue(30)
    slider.show()
    _wheel(slider)
    assert slider.value() != 30


@pytest.mark.parametrize("cls, make", [
    (NoWheelSlider, lambda c: c(Qt.Orientation.Horizontal)),
    (NoWheelSpinBox, lambda c: c()),
])
def test_the_no_wheel_controls_ignore_the_wheel(qapp, cls, make):
    widget = make(cls)
    widget.setRange(0, 300)
    widget.setValue(30)
    widget.show()
    event = _wheel(widget)
    assert widget.value() == 30
    assert not event.isAccepted(), "the event has to be handed on, so the page behind it scrolls"


def test_dragging_and_the_keyboard_still_work(qapp):
    slider = NoWheelSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 300)
    slider.setSingleStep(10)
    slider.setValue(30)
    slider.triggerAction(QSlider.SliderAction.SliderSingleStepAdd)   # what an arrow key does
    assert slider.value() == 40
    spin = NoWheelSpinBox()
    spin.setRange(7, 30)
    spin.setValue(10)
    spin.stepUp()
    assert spin.value() == 11


def test_every_slider_of_the_dac_settings_panel_is_a_no_wheel_slider(qapp):
    """Through the real path: the daemon's GetSettings payload -> DacPage -> its QSettingsWidget."""
    from arctis_sound_manager.gui.dac_page import DacPage

    gs = GeneralSettings()
    page = DacPage()
    page.update_settings({
        "dac_settings_config": {c.name: c.to_dict() for c in gs.dac_settings_config},
        "dac": {c.name: getattr(gs, c.name) for c in gs.dac_settings_config},
    })

    sliders = page._dac_widget.findChildren(QSlider)
    assert sliders, "the panel built no sliders: the test is not looking at the right thing"
    assert all(isinstance(s, NoWheelSlider) for s in sliders), [type(s).__name__ for s in sliders]
    timeout = next(s for s in sliders if s.maximum() == 300)       # oled_screen_timeout
    timeout.show()
    before = timeout.value()
    _wheel(timeout)
    assert timeout.value() == before


def test_the_font_size_boxes_of_the_dac_page_are_no_wheel_spin_boxes(qapp):
    from arctis_sound_manager.gui.dac_page import DacPage

    page = DacPage()
    boxes = page.findChildren(QSpinBox)
    assert boxes, "the DAC page built no spin boxes"
    assert all(isinstance(b, NoWheelSpinBox) for b in boxes), [type(b).__name__ for b in boxes]
