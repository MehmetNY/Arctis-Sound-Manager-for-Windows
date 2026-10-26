# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""Only Help gives up height, and giving it up has to free real space.

Adding Clips made a seventh full-height sidebar button, and the column then ran
past the bottom block: the ASM logo underneath it was clipped. Help is the one
that can afford a smaller icon — least travelled of the seven, last in the
column.

The part worth pinning is that the height follows the icon. Shrinking the glyph
inside a fixed-height button would draw a smaller picture and free nothing at
all, which looks like the change did not work.
"""
from __future__ import annotations

import pytest


def test_a_smaller_icon_makes_a_shorter_button():
    """The whole point: the column gets the space back."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    from arctis_sound_manager.gui.components import (HELP_ICON, HOME_ICON,
                                                     SidebarButton,
                                                     _DEFAULT_ICON_SIZE)

    standard = SidebarButton(svg_path=HOME_ICON, label="Channels")
    smaller = SidebarButton(svg_path=HELP_ICON, label="Help",
                            icon_size=_DEFAULT_ICON_SIZE - 14)

    assert smaller.height() == standard.height() - 14
    assert smaller.width() == standard.width(), "only the height gives way"


def test_the_default_button_is_unchanged():
    """Every other button keeps exactly what it had."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    from arctis_sound_manager.gui.components import (HOME_ICON, SidebarButton,
                                                     _DEFAULT_BUTTON_HEIGHT,
                                                     _DEFAULT_ICON_SIZE)

    btn = SidebarButton(svg_path=HOME_ICON, label="Channels")
    assert btn.height() == _DEFAULT_BUTTON_HEIGHT
    assert btn._icon_size == _DEFAULT_ICON_SIZE


def test_help_is_the_only_one_shrunk():
    """Read from the sidebar definition itself, so adding a page cannot quietly
    shrink another icon along the way."""
    import re
    from pathlib import Path

    import arctis_sound_manager.gui.main_app as main_app

    source = Path(main_app.__file__).read_text()
    block = re.search(r"top_pages_def = \[(.*?)\n        \]", source, re.S)
    assert block, "the sidebar definition moved — this test needs updating"

    rows = [ln for ln in block.group(1).splitlines() if ln.strip().startswith("(")]
    assert len(rows) == 7, f"expected 7 sidebar pages, found {len(rows)}"

    shrunk = [ln for ln in rows if "_SIDEBAR_HELP_ICON_SIZE" in ln]
    assert len(shrunk) == 1
    assert "HELP_ICON" in shrunk[0]
    assert main_app._SIDEBAR_HELP_ICON_SIZE < main_app._SIDEBAR_ICON_SIZE


def test_the_sidebar_still_fits_the_default_window():
    """The invariant Clips broke, stated as a budget.

    The column is not scrollable and the bottom block (logo, Ko-fi, version)
    does not compress, so anything the buttons take is taken from the logo —
    which is how a seventh page silently clipped it. Only the deterministic
    part is measured here: button heights, spacing and margins. The rest of
    the window's 990px is the bottom block's budget, and it needs about 115.
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    import re
    from pathlib import Path

    import arctis_sound_manager.gui.main_app as main_app
    from arctis_sound_manager.gui.components import HOME_ICON, SidebarButton

    source = Path(main_app.__file__).read_text()
    spacing = main_app._SIDEBAR_SPACING          # the Linux value here: the Windows build scrolls what does not fit
    margins = re.search(r"sidebar_layout\.setContentsMargins\(\d+, (\d+), \d+, (\d+)\)", source)
    vertical_margins = int(margins.group(1)) + int(margins.group(2))

    block = re.search(r"top_pages_def = \[(.*?)\n        \]", source, re.S).group(1)
    rows = [ln for ln in block.splitlines() if ln.strip().startswith("(")]
    sizes = [main_app._SIDEBAR_HELP_ICON_SIZE if "HELP_ICON" in ln
             else main_app._SIDEBAR_ICON_SIZE for ln in rows]
    total = sum(SidebarButton(svg_path=HOME_ICON, label="x", icon_size=s).height()
                for s in sizes)

    # pages + the stretch + the bottom block are the laid-out items
    total += spacing * (len(rows) + 1) + vertical_margins

    WINDOW = 990          # main_app: window.resize(min(1400, …), min(990, …))
    BOTTOM_BLOCK = 115    # logo 55 + support + Ko-fi + version + spacings
    assert total <= WINDOW - BOTTOM_BLOCK, (
        f"the sidebar needs {total}px, leaving {WINDOW - total}px for a bottom "
        f"block that wants {BOTTOM_BLOCK}px — the ASM logo will be clipped")
