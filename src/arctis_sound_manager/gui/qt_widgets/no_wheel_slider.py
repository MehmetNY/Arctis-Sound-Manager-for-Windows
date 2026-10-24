# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

from PySide6.QtWidgets import QSlider, QSpinBox


class NoWheelSlider(QSlider):
    """QSlider that leaves the wheel to the page it sits on.

    A stock slider takes the wheel whenever the pointer is over it, and each notch is a real
    `valueChanged`. On a scrolling settings page, scrolling past "screen timeout" therefore
    changes the screen timeout - and sends it to the headset - without a click. The same goes
    for `NoWheelSpinBox`. Dragging, clicking the groove and the keyboard still work.
    """

    def wheelEvent(self, event):
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    """QSpinBox that leaves the wheel to the page it sits on (see NoWheelSlider)."""

    def wheelEvent(self, event):
        event.ignore()
