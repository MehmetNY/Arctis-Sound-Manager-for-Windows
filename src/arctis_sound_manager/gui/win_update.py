# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The "update now" flow of the Windows build: ask, download, run the installer, end the program.

One function, shared by the Home page banner and the Settings button. The installer (``ASM-Setup-<version>.exe``
of the newest published release) closes every running ASM process, replaces the files and opens the program
again; Windows asks for the administrator's permission once, because installing into Program Files needs it.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget

from arctis_sound_manager.i18n import I18n

log = logging.getLogger(__name__)


def _text(key: str, **values: str) -> str:
    text = I18n.translate("ui", key).replace("\\n", "\n")          # the language files keep a line break as \n
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    return text


def run_windows_update(parent: QWidget, version: str) -> None:
    """Confirm with the person, then update. Returns at once; the worker lives on ``parent``."""
    from arctis_sound_manager.update_checker import WinUpdateInstallWorker
    from arctis_sound_manager.utils import project_version

    if getattr(parent, "_win_update_worker", None) is not None:
        return                                                       # one at a time
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(_text("win_update_title"))
    box.setText(_text("win_update_confirm", version=version))
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.Yes)
    if box.exec() != QMessageBox.StandardButton.Yes:
        return

    progress = QProgressDialog(_text("win_update_downloading", version=version), _text("win_update_cancel"),
                               0, 0, parent)
    progress.setWindowTitle(_text("win_update_title"))
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(0)
    worker = WinUpdateInstallWorker(project_version())
    parent._win_update_worker = worker                               # keeps the thread alive

    def on_progress(done: int, total: int) -> None:
        if total > 0:
            progress.setMaximum(total)
            progress.setValue(min(done, total))

    def on_done(ok: bool, message: str) -> None:
        progress.close()
        parent._win_update_worker = None
        if ok:
            # the installer is running (and has the administrator's permission): get out of its way
            log.info("Update to %s: installer started, quitting", message)
            app = QApplication.instance()
            if app is not None:
                app.quit()
            return
        if message == "cancelled":
            return
        QMessageBox.warning(parent, _text("win_update_title"), _text("win_update_failed", error=message))

    worker.progress.connect(on_progress)
    worker.done.connect(on_done)
    progress.canceled.connect(worker.cancel)
    worker.start()
    progress.show()
