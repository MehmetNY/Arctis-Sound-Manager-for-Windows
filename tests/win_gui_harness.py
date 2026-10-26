"""Launch the REAL ``asm-gui`` main() as Windows will (offscreen Qt), against a running
``win_daemon_harness.py --serve`` process sharing ``WIN_HARNESS_TMP``.

Prints one ``RESULT:<json>`` line and exits. Not a test module (see test_win_gui_smoke.py).
"""
import importlib.abc
import json
import re
import os
import sys


class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"pyudev", "pulsectl", "pwd", "grp", "gi"}:
            raise ModuleNotFoundError(f"No module named {name!r} (simulated Windows)")


sys.meta_path.insert(0, _Block())

import win_posix_absence  # noqa: E402  (tests/ is sys.path[0] when run as a script)

REMOVED_POSIX_NAMES = win_posix_absence.apply()
tmp = os.environ["WIN_HARNESS_TMP"]
os.environ.update(XDG_CONFIG_HOME=f"{tmp}/cfg", XDG_RUNTIME_DIR=f"{tmp}/run", HOME=tmp)
# Windows sets none of these. QT_QPA_PLATFORM in particular: with it unset, asm-gui's own
# startup checks assume an X11 desktop unless told otherwise (that is how a Windows user got
# "libxcb-cursor0 is missing"). It is switched to offscreen only after those checks have run.
for _name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
    os.environ.pop(_name, None)

import arctis_sound_manager.constants as C  # noqa: E402

C.IS_WINDOWS = True

from arctis_sound_manager.win import install_dbus_shim, install_usb_shim  # noqa: E402

install_usb_shim(force=True)
install_dbus_shim(force=True)

SHOT = os.environ.get("GUI_SHOT", f"{tmp}/gui_home.png")
import logging  # noqa: E402

from arctis_sound_manager.gui import chatmix_osd as CO  # noqa: E402
from arctis_sound_manager.gui import systray_app as S  # noqa: E402

# What the DAC-wheel OSD watcher reports: ``None`` is its "new baseline" signal, which it only
# emits once it has connected, introspected the daemon and subscribed to StatusChanged.
WHEEL_EVENTS: list = []
WHEEL_ERRORS: list[str] = []


class _WatcherLog(logging.Handler):
    def emit(self, record):
        if "DAC wheel OSD watcher" in record.getMessage():
            WHEEL_ERRORS.append(record.getMessage())


_watch_logger = logging.getLogger(CO.__name__)
_watch_logger.setLevel(logging.DEBUG)
_watch_logger.addHandler(_WatcherLog())

_orig_watcher_start = CO.ChatMixWatcher.start


def _watcher_start(self):
    self.wheel_changed.connect(lambda value: WHEEL_EVENTS.append(value))
    _orig_watcher_start(self)


CO.ChatMixWatcher.start = _watcher_start

_orig_open = S.QSystrayApp.open_main_window


def _open_then_inspect(self):
    _orig_open(self)
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLabel

    attempts = [0]

    def inspect():
        app = self._ensure_main_app()
        attempts[0] += 1
        if app is None and attempts[0] < 60:
            QTimer.singleShot(500, inspect)
            return
        w = app.main_window
        labels = [l.text() for l in w.findChildren(QLabel)]
        # the daemon is polled every ~2 s: wait until the headset has shown up (or give up)
        if not any("Nova Pro" in t for t in labels) and attempts[0] < 60:
            QTimer.singleShot(500, inspect)
            return
        w.resize(1100, 760)
        from arctis_sound_manager.gui import main_app as MA
        # Where do the sound-processing cards live, and did the equaliser stay where it was?
        sonar = app._equalizer_page._sonar_page
        owners = {"game": sonar._game_widget, "media": sonar._media_widget, "aux": sonar._aux_widget,
                  "chat": sonar._chat_widget, "micro": sonar._micro_widget}
        headset, equalizer = app._headset_page, app._equalizer_page
        cards_on_headset = {ch: headset.isAncestorOf(o._processing_card) for ch, o in owners.items()}
        eq_on_equalizer = {ch: equalizer.isAncestorOf(o._eq_widget) for ch, o in owners.items() if hasattr(o, "_eq_widget")}
        eq_on_headset = {ch: headset.isAncestorOf(o._eq_widget) for ch, o in owners.items() if hasattr(o, "_eq_widget")}
        presets_on_equalizer = {ch: equalizer.isAncestorOf(o._preset_bar) for ch, o in owners.items() if hasattr(o, "_preset_bar")}
        presets_on_headset = {ch: headset.isAncestorOf(o._preset_bar) for ch, o in owners.items() if hasattr(o, "_preset_bar")}
        placement = {"cards_on_headset": cards_on_headset, "eq_on_equalizer": eq_on_equalizer,
                     "eq_on_headset": eq_on_headset, "presets_on_equalizer": presets_on_equalizer,
                     "presets_on_headset": presets_on_headset}
        # About page: its text, its screenshot, and the donation button of the sidebar (what it opens)
        app._switch_page(MA.PAGE_ABOUT)
        about_labels = [l.text() for l in app._about_page.findChildren(QLabel)]
        about_shot = SHOT.replace(".png", "_about.png")
        about_saved = w.grab().save(about_shot)
        opened_urls = []
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl = staticmethod(lambda url: opened_urls.append(url.toString()) or True)
        app._donate_button.click()
        from PySide6.QtWidgets import QWidget as _QWidget
        sidebar_box = w.findChild(_QWidget, "sidebar")
        nav = app._sidebar_nav
        sidebar_needs = sidebar_box.layout().minimumSize().height()
        bar = nav.verticalScrollBar()
        original_h = w.height()
        w.resize(w.width(), 990)                                   # the window's default height on a roomy screen
        QApplication.processEvents()
        QApplication.processEvents()
        nav_at_default = {"window_h": w.height(), "bar_visible": bar.isVisible(), "scroll_range": bar.maximum()}
        w.resize(w.width(), 700)                                   # a laptop window
        QApplication.processEvents()
        QApplication.processEvents()
        nav_short = {"window_h": w.height(), "bar_range": bar.maximum(), "sidebar_h": sidebar_box.height(),
                     "sidebar_min": sidebar_box.minimumSizeHint().height()}
        about_btn = app._sidebar_buttons[-1]
        bottom = sidebar_box.layout().itemAt(sidebar_box.layout().count() - 1).widget()
        app._switch_page(MA.PAGE_ABOUT)
        QApplication.processEvents()
        # the About button is inside the visible part of the scroll area and does not overlap the logo block
        about_top = about_btn.mapTo(sidebar_box, about_btn.rect().topLeft()).y()
        about_bottom = about_top + about_btn.height()
        nav_short.update({"about_visible_area": nav.viewport().rect().height(),
                          "about_bottom_vs_block": bottom.y() - about_bottom,
                          "nav_bottom_vs_block": bottom.y() - (nav.y() + nav.height())})
        short_shot = SHOT.replace(".png", "_short.png")
        nav_short["saved"] = w.grab().save(short_shot)
        nav_short["shot"] = short_shot
        w.resize(w.width(), original_h)
        QApplication.processEvents()
        sidebar_items = []
        donate_label = app._donate_button.text()
        app._switch_page(MA.PAGE_HOME)
        help_labels = [l.text() for l in app._help_page.findChildren(QLabel)]
        from PySide6.QtWidgets import QAbstractButton
        sidebar_labels = [b._label_text for b in app._sidebar_buttons]
        # every piece of text anywhere in the window, hidden or not
        every_text = [x.text() for x in w.findChildren(QLabel)] + [x.text() for x in w.findChildren(QAbstractButton)]
        every_text += sidebar_labels
        image = w.grab().toImage()
        colors = {image.pixel(x, y) for x in range(0, image.width(), 37) for y in range(0, image.height(), 37)}
        print("RESULT:" + json.dumps({
            "title": w.windowTitle(),
            "saved": w.grab().save(SHOT),
            "distinct_colors": len(colors),
            "has_headset_name": any("Arctis Nova Pro Wireless" in t for t in labels),
            "has_battery": any("100%" in t for t in labels),
            "has_online": any(t.strip() == "Online" for t in labels),
            "shot": SHOT,
            "sidebar_labels": sidebar_labels,
            "stack_pages": app._stack.count(),
            "clips_page_object": app._clips_page is not None,
            "clip_text_anywhere": [t for t in every_text if re.search(r"\bclips?\b", t, re.I)],
            "page_numbers": [MA.PAGE_SETTINGS, MA.PAGE_HELP, MA.PAGE_ABOUT, MA.PAGE_THEME_EDITOR],
            "about_page_is_about": app._stack.widget(MA.PAGE_ABOUT) is app._about_page,
            "about_labels": about_labels, "about_shot": about_shot, "about_saved": about_saved,
            "sidebar_needs": sidebar_needs, "nav_at_default": nav_at_default, "nav_short": nav_short, "donation_opened": opened_urls, "donate_label": donate_label,
            "settings_page_is_settings": app._stack.widget(MA.PAGE_SETTINGS) is app._device_page,
            "help_page_is_help": app._stack.widget(MA.PAGE_HELP) is app._help_page,
            "theme_page_is_theme": app._stack.widget(MA.PAGE_THEME_EDITOR) is app._theme_editor_page,
            "placement": placement,
            "has_banner_object": hasattr(app, "_engine_banner"),
            "nag_text": [x for x in every_text if re.search(r"not set up|needs? virtual audio cables|kurulmad", x, re.I)],
            "help_has_clips": any(t.strip() == "Clips" for t in help_labels),
            "wheel_baselines": sum(1 for v in WHEEL_EVENTS if v is None),
            "wheel_errors": WHEEL_ERRORS,
            "xcb_help": XCB_HELP[0] if XCB_HELP else "NOT CALLED",
        }), flush=True)
        os._exit(0)

    QTimer.singleShot(2500, inspect)


S.QSystrayApp.open_main_window = _open_then_inspect

sys.argv = ["asm-gui", "-vv"]
from arctis_sound_manager.scripts import gui  # noqa: E402

XCB_HELP = []
_real_xcb_help = gui._xcb_cursor_help


def _xcb_help_then_offscreen(*args, **kwargs):
    result = _real_xcb_help(*args, **kwargs)     # the REAL check, in Windows' environment
    XCB_HELP.append(result)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"  # only now: this sandbox has no real display
    return result


gui._xcb_cursor_help = _xcb_help_then_offscreen
gui.main()
