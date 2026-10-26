# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""For the CI soak test only: with ``ASM_SOAK_CYCLE_S=<seconds>`` set, the window steps through its pages by itself.

A window that sits on its first page for fifteen minutes proves little about a window somebody uses for three hours.
Nothing here runs unless that variable is set, which only ``ci/soak.py`` does.
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import QTimer

log = logging.getLogger("health")


def _seconds() -> float | None:
    try:
        value = float(os.environ.get("ASM_SOAK_CYCLE_S", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def install(main_app) -> QTimer | None:
    """Start the page cycle on *main_app* (a ``QMainApp``); ``None`` when it is not asked for."""
    seconds = _seconds()
    if seconds is None:
        return None
    from arctis_sound_manager.gui import main_app as module
    pages = [page for page in (module.PAGE_HOME, module.PAGE_EQUALIZER, module.PAGE_HEADSET, module.PAGE_DAC,
                               module.PAGE_SETTINGS, module.PAGE_HELP, module.PAGE_ABOUT) if page >= 0]
    state = {"i": 0}

    def step() -> None:
        state["i"] = (state["i"] + 1) % len(pages)
        try:
            main_app._switch_page(pages[state["i"]])
        except Exception:  # noqa: BLE001 — a page that cannot be shown is a finding, not a crash
            log.exception("soak: could not show page %s", pages[state["i"]])

    timer = QTimer(main_app)
    timer.setInterval(int(seconds * 1000))
    timer.timeout.connect(step)
    timer.start()
    log.info("soak: the window steps through %d pages every %.0f s", len(pages), seconds)
    return timer
