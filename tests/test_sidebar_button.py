# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sidebar buttons: a page change must only touch the two buttons whose state changes, and never redraw an icon twice."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

from arctis_sound_manager.gui import components  # noqa: E402

SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><path fill="#fff" d="M0 0h24v24H0z"/></svg>'


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def buttons(qapp, tmp_path):
    svg = tmp_path / "icon.svg"
    svg.write_text(SVG, encoding="utf-8")
    return [components.SidebarButton(str(svg), f"b{i}") for i in range(7)]


def _count_sheets(btn, monkeypatch):
    calls = []
    for w in (btn, btn._label_widget):
        original = w.setStyleSheet
        monkeypatch.setattr(w, "setStyleSheet", lambda text, _o=original, _w=w: (calls.append(text), _o(text)))
    return calls


def test_an_unchanged_state_is_left_alone(buttons, monkeypatch):
    btn = buttons[0]
    btn.set_active(False)                       # first time: applies the style
    calls = _count_sheets(btn, monkeypatch)
    for _ in range(50):
        btn.set_active(False)
    assert calls == []
    btn.set_active(True)
    assert len(calls) == 2                      # the button and its label, once
    for _ in range(50):
        btn.set_active(True)
    assert len(calls) == 2


def test_a_page_change_touches_two_buttons_not_seven(buttons, monkeypatch):
    for i, b in enumerate(buttons):
        b.set_active(i == 0)
    all_calls = [_count_sheets(b, monkeypatch) for b in buttons]
    for i, b in enumerate(buttons):             # what _switch_page does
        b.set_active(i == 3)
    touched = [i for i, calls in enumerate(all_calls) if calls]
    assert touched == [0, 3]


def test_each_icon_colour_is_drawn_once(buttons, monkeypatch):
    btn = buttons[1]
    renders = []
    original = components.QSvgRenderer
    monkeypatch.setattr(components, "QSvgRenderer", lambda data: (renders.append(1), original(data))[1])
    for _ in range(20):
        btn.set_active(True)
        btn.set_active(False)
    assert len(renders) == 2                    # one per colour


def test_new_theme_colours_are_applied_and_the_icons_redrawn(buttons, monkeypatch):
    btn = buttons[2]
    btn.set_active(True)
    before = btn._icons.copy()
    btn.update_colors({"TEXT_SECONDARY": "#111111", "ACCENT": "#ff0000", "BG_BUTTON_HOVER": "#222222",
                       "BG_SIDEBAR_ACTIVE": "#333333", "TEXT_PRIMARY": "#eeeeee"})
    assert btn._active is True
    assert "#ff0000" in btn._icons and "#ff0000" not in before
    assert "#333333" in btn.styleSheet()
