# Copyright (C) 2022 Giacomo Furlan (elegos) — original work
# Copyright (C) 2026 loteran — modifications
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Main application window — ArctisSonar GUI visual style.
"""
import logging

from PySide6.QtCore import Qt, QTimer, QUrl, Slot
from pathlib import Path

from PySide6.QtGui import QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from arctis_sound_manager import branding
from arctis_sound_manager.constants import IS_WINDOWS
from arctis_sound_manager.gui.base_app import QBaseDesktopApp
from arctis_sound_manager.gui.components import (
    ABOUT_ICON,
    CLIPS_ICON,
    EQUALIZER_ICON,
    GAMEDAC_ICON,
    HDMI_ICON,
    HEADPHONE_ICON,
    HELP_ICON,
    HOME_ICON,
    SETTINGS_ICON,
    SidebarButton,
)
from arctis_sound_manager.gui.dbus_wrapper import DbusWrapper
from arctis_sound_manager.gui.dac_page import DacPage
from arctis_sound_manager.gui.device_page import DevicePage
from arctis_sound_manager.gui.headset_page import HeadsetPage
if not IS_WINDOWS:   # Clips is not part of the Windows build
    from arctis_sound_manager.gui.clips_page import ClipsPage
from arctis_sound_manager.gui.about_page import AboutPage
from arctis_sound_manager.gui.help_page import HelpPage
from arctis_sound_manager.gui.equalizer_page import EqualizerPage
from arctis_sound_manager.gui.home_page import HomePage
from arctis_sound_manager.gui.main_app_proto_widget import QMainAppProtoWidget
from arctis_sound_manager.gui.theme import c as _theme_color
from arctis_sound_manager.gui.theme import (
    ACCENT,
    APP_QSS,
    BG_MAIN,
    BORDER,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    THEMES,
    build_qss,
    set_active_theme,
    get_theme,
    set_preview_colors,
    PREVIEW_THEME_ID,
)
from arctis_sound_manager.gui.theme_editor_page import ThemeEditorPage
from arctis_sound_manager.gui.ui_utils import get_icon_pixmap
from arctis_sound_manager.i18n import I18n
from arctis_sound_manager.settings import GeneralSettings


# ── Main application window ───────────────────────────────────────────────────

# Page indices, shared by the sidebar buttons and the stack — the two must stay
# in step. They were bare numbers scattered across the file, so inserting a page
# silently sent the theme editor and its "back" action to the wrong widgets.
PAGE_HOME = 0
PAGE_EQUALIZER = 1
PAGE_HEADSET = 2
PAGE_DAC = 3
if IS_WINDOWS:
    # Clips does not exist on Windows: no sidebar entry, no page. The pages after it move up
    # one place so sidebar index and stack index stay the same number, as everywhere else here.
    PAGE_CLIPS = -1
    PAGE_SETTINGS = 4
    PAGE_HELP = 5
    PAGE_ABOUT = 6                  # under Help (the Windows build only)
    PAGE_THEME_EDITOR = 7
else:
    PAGE_CLIPS = 4
    PAGE_SETTINGS = 5
    PAGE_HELP = 6
    PAGE_ABOUT = -1
    PAGE_THEME_EDITOR = 7


#: Sidebar icon sizes. Help is deliberately smaller — see the note on
#: top_pages_def in _build_window.
_SIDEBAR_ICON_SIZE = 44
_SIDEBAR_HELP_ICON_SIZE = 30
_SIDEBAR_SPACING = 6
if IS_WINDOWS:
    # Seven entries (About under Help) plus the logo block must still fit a laptop screen's window
    # (768 px high screens give ~720): a little smaller icons and tighter gaps; what still does not fit scrolls.
    _SIDEBAR_ICON_SIZE = 38
    _SIDEBAR_HELP_ICON_SIZE = 26
    _SIDEBAR_SPACING = 3


class QMainApp(QBaseDesktopApp):
    app: QApplication
    main_window: QMainAppProtoWidget

    def __init__(self, app: QApplication, log_level: int):
        super().__init__(parent=app)

        self.logger = logging.getLogger("QMainApp")
        self.logger.setLevel(log_level)

        self.app = app
        self.settings: dict = {}
        self.status: dict = {}

        # Load general settings (needed for theme before building window)
        self._general_settings = GeneralSettings.read_from_file()

        # Activate saved theme in the theme module so c() is correct from the start
        set_active_theme(self._general_settings.theme)
        # Apply global dark stylesheet with saved theme
        app.setStyleSheet(build_qss(self._general_settings.theme))

        # D-Bus wrapper
        self.dbus_wrapper = DbusWrapper()
        self.dbus_wrapper.sig_settings.connect(self.on_settings_received)
        self.dbus_wrapper.sig_status.connect(self.on_status_received)
        DbusWrapper.show_splash()

        # Build window
        self.main_window = self._build_window()

        # Initialise theme-before-edit tracker
        self._theme_before_edit: str = "steelseries"

        # Wire theme change signal
        self._device_page.sig_theme_changed.connect(self._apply_theme)
        self._device_page.sig_theme_create.connect(self._open_theme_editor_new)
        self._device_page.sig_theme_edit.connect(self._open_theme_editor_edit)
        self._theme_editor_page.sig_preview.connect(self._on_theme_preview)
        self._theme_editor_page.sig_saved.connect(self._on_theme_saved)
        self._theme_editor_page.sig_cancelled.connect(self._on_theme_editor_cancelled)

        # Wire D-Bus signals to pages
        self.dbus_wrapper.sig_status.connect(self._home_page.update_status)
        self.dbus_wrapper.sig_status.connect(self._headset_page.update_status)
        self.dbus_wrapper.sig_status.connect(self._device_page.update_status)
        self.dbus_wrapper.sig_status.connect(self._dac_page.update_status)
        self.dbus_wrapper.sig_settings.connect(self._home_page.update_settings)
        self.dbus_wrapper.sig_settings.connect(self._headset_page.update_settings)
        self.dbus_wrapper.sig_settings.connect(self._equalizer_page.update_settings)
        self.dbus_wrapper.sig_settings.connect(self._dac_page.update_settings)
        self.dbus_wrapper.sig_settings.connect(self._device_page.update_settings)

        # DAC tab hidden by default until device confirms it has a DAC
        self._sidebar_buttons[PAGE_DAC].setVisible(False)

        # Start on home page
        self._switch_page(PAGE_HOME)

        # Check for updates (non-blocking background thread)
        from arctis_sound_manager.update_checker import UpdateCheckWorker
        from arctis_sound_manager.utils import project_version
        self._update_worker = UpdateCheckWorker(project_version())
        self._update_worker.result.connect(self._home_page.on_update_available)
        self._update_worker.start()
        self._device_page.sig_update_result.connect(self._home_page.on_update_available)

        # Check for new/updated translation files (non-blocking)
        # (not on Windows: the translations ship with the program, and the original project's files would
        # replace the Windows-specific texts with ones that lack them)
        if not IS_WINDOWS:
            from arctis_sound_manager.lang_updater import LangUpdateWorker
            self._lang_worker = LangUpdateWorker()
            self._lang_worker.langs_updated.connect(self._device_page.rebuild_lang_combo)
            self._lang_worker.start()

        # Wire profile bar
        self._home_page.profile_bar.sig_apply.connect(self._on_apply_profile)
        self._home_page.profile_bar.sig_changed.connect(self._on_profiles_changed)

        # A preset picked on a Channels card: show it on the Equalizer page too
        self._home_page.sig_eq_preset_applied.connect(
            self._equalizer_page._sonar_page.notify_external_preset_change)

        # The cards' output buttons drive the Equalizer page's own selectors,
        # so a device picked on either page shows on both
        sonar = self._equalizer_page._sonar_page
        self._home_page.set_output_pickers({
            "game": sonar._game_output_selector,
            "chat": sonar._chat_output_selector,
            "media": sonar._media_output_selector,
            "aux": sonar._aux_output_selector,
            "output": sonar._output_selector,
        })

        self.destroyed.connect(self.sig_stop)
        self.main_window.visibilityChanged.connect(self._on_visibility_changed)

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _apply_theme(self, theme_name: str, save: bool = True) -> None:
        # Update the active theme state first so c() returns the right colors
        # during all subsequent restyle calls.
        set_active_theme(theme_name)
        t = get_theme(theme_name)
        self.app.setStyleSheet(build_qss(theme_name))
        for btn in self._sidebar_buttons:
            btn.update_colors(t)
        # Propagate to every page that implements apply_theme
        for page in (
            self._home_page,
            self._equalizer_page,
            self._headset_page,
            self._dac_page,
            self._device_page,
            self._help_page,
            self._about_page,
            self._theme_editor_page,
            getattr(self, "_clips_page", None),
        ):
            if hasattr(page, "apply_theme"):
                page.apply_theme(t)
        # Propagate to the profile bar (lives in home_page, outside the stack layout)
        if hasattr(self._home_page, "profile_bar"):
            self._home_page.profile_bar.apply_theme(t)
        self._switch_page(self._stack.currentIndex())
        if save and theme_name != PREVIEW_THEME_ID:
            self._general_settings.theme = theme_name
            self._general_settings.write_to_file()

    # ── Visibility ────────────────────────────────────────────────────────────

    def _on_visibility_changed(self, visible: bool):
        if visible:
            self.logger.debug("App is visible — starting D-Bus polling")
            self.dbus_wrapper.request_settings()
            self.dbus_wrapper.request_status()
        else:
            self.logger.debug("App is hidden — stopping D-Bus polling")
            self.dbus_wrapper.stop()

    # ── Window construction ───────────────────────────────────────────────────

    def _build_window(self) -> QMainAppProtoWidget:
        window = QMainAppProtoWidget()
        # The pages are children of this widget, while the controller is a
        # QObject beside it — so a page that needs to ask the controller for
        # something (the Settings toggle asking the sidebar to show Clips) has
        # no parent chain to walk. One link from the window closes that gap;
        # a page reaches it with `self.window().main_app`.
        window.main_app = self
        window.setWindowFlags(Qt.WindowType.Window)
        window.setWindowTitle("Arctis Sound Manager")
        window.setWindowIcon(QIcon(get_icon_pixmap()))
        window.setMinimumSize(900, 650)

        available = window.screen().availableGeometry()
        window.resize(
            min(1400, available.width()),
            min(990, available.height()),
        )

        root_layout = QHBoxLayout(window)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Sidebar ───────────────────────────────────────────────────────────
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(150)
        sidebar_layout = QVBoxLayout(sidebar)
        # Seven pages plus the bottom block do not fit a 990px window at the
        # original 16px margins and 8px spacing: the column came out 26px
        # too tall and the ASM logo was clipped. A smaller Help icon gave
        # back 14 of those; the remaining 12 come from here rather than from
        # shrinking a second icon, because 2px between buttons and 4px at
        # the ends is not something anyone can see, and a mismatched icon is.
        sidebar_layout.setContentsMargins(15, 12, 15, 12)
        sidebar_layout.setSpacing(_SIDEBAR_SPACING)

        # Resolve current theme accent color for icon
        current_theme = THEMES.get(self._general_settings.theme, THEMES["steelseries"])
        current_accent = current_theme["ACCENT"]

        # Top navigation buttons: Channels, Equalizer, Headset, DAC, Clips,
        # Settings, Help. The fourth field is the icon size; only Help asks
        # for a smaller one. Adding Clips made a seventh full-height button,
        # and the column then ran past the bottom block — the ASM logo was
        # clipped. Help is the one that can afford it: it is the least
        # travelled of the seven and the last in the column.
        top_pages_def = [
            (HOME_ICON,      I18n.translate('ui', 'channels'),  current_accent, _SIDEBAR_ICON_SIZE),
            (EQUALIZER_ICON, I18n.translate('ui', 'equalizer'), current_accent, _SIDEBAR_ICON_SIZE),
            (HEADPHONE_ICON, I18n.translate('ui', 'headset'),   current_accent, _SIDEBAR_ICON_SIZE),
            (GAMEDAC_ICON,   I18n.translate('ui', 'dac'),       current_accent, _SIDEBAR_ICON_SIZE),
            (CLIPS_ICON,     I18n.translate('ui', 'clips'),     current_accent, _SIDEBAR_ICON_SIZE),
            (SETTINGS_ICON,  I18n.translate('ui', 'settings'),  current_accent, _SIDEBAR_ICON_SIZE),
            (HELP_ICON,      I18n.translate('ui', 'help'),      current_accent, _SIDEBAR_HELP_ICON_SIZE),
        ]

        if IS_WINDOWS:   # Clips is not part of the Windows build: no entry, not even a hidden one
            top_pages_def = [entry for entry in top_pages_def if entry[0] != CLIPS_ICON]
            # About sits right under Help, with the same small icon
            top_pages_def.append(
                (ABOUT_ICON, I18n.translate('ui', 'about'), current_accent, _SIDEBAR_HELP_ICON_SIZE))

        # Where the buttons go. On Windows they sit in a scroll area of their own: a laptop screen (1080p at 150 %
        # scaling is only 720 px high) cannot show seven buttons, the logo and the donation block at once, and
        # without this the About button ran into the logo. When the window is tall enough nothing scrolls and
        # nothing looks different; when it is not, the buttons scroll and the logo block stays put.
        nav_layout = sidebar_layout
        if IS_WINDOWS:
            nav_host = QWidget()
            nav_host.setStyleSheet("background: transparent;")
            nav_layout = QVBoxLayout(nav_host)
            nav_layout.setContentsMargins(12, 0, 12, 0)
            nav_layout.setSpacing(_SIDEBAR_SPACING)
            nav_scroll = QScrollArea()
            nav_scroll.setObjectName("sidebarNav")
            nav_scroll.setWidgetResizable(True)
            nav_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            nav_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            nav_scroll.setMinimumHeight(160)
            nav_scroll.setStyleSheet(
                "QScrollArea#sidebarNav { background: transparent; border: none; }"
                "QScrollArea#sidebarNav > QWidget > QWidget { background: transparent; }"
                "QScrollArea#sidebarNav QScrollBar:vertical { width: 6px; background: transparent; margin: 0; }"
                "QScrollArea#sidebarNav QScrollBar::handle:vertical { background: #55606e; border-radius: 3px;"
                " min-height: 24px; }"
                "QScrollArea#sidebarNav QScrollBar::add-line:vertical,"
                "QScrollArea#sidebarNav QScrollBar::sub-line:vertical { height: 0; }"
                "QScrollArea#sidebarNav QScrollBar::add-page:vertical,"
                "QScrollArea#sidebarNav QScrollBar::sub-page:vertical { background: transparent; }")
            nav_scroll.setWidget(nav_host)
            # the scroll area spans the sidebar's full width: its margins moved into the host
            sidebar_layout.setContentsMargins(0, 12, 0, 12)
            sidebar_layout.addWidget(nav_scroll, 1)
            self._sidebar_nav = nav_scroll

        self._sidebar_buttons: list[SidebarButton] = []
        for svg_path, label, color_active, icon_size in top_pages_def:
            btn = SidebarButton(
                svg_path=svg_path,
                label=label,
                icon_color_inactive=current_theme["TEXT_SECONDARY"],
                icon_color_active=color_active,
                parent=sidebar,
                icon_size=icon_size,
            )
            idx = len(self._sidebar_buttons)
            btn.clicked.connect(lambda checked=False, i=idx: self._switch_page(i))
            nav_layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignHCenter)
            self._sidebar_buttons.append(btn)

        if IS_WINDOWS:
            nav_layout.addStretch(1)
        else:
            sidebar_layout.addStretch(1)

        # Bottom block: logo + github + kofi + version, groupés serré
        _bottom = QWidget()
        _bottom.setStyleSheet("background: transparent;")
        _bottom_layout = QVBoxLayout(_bottom)
        _bottom_layout.setContentsMargins(0, 0, 0, 0)
        _bottom_layout.setSpacing(3)
        _bottom_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        # ASM logo cliquable → repo GitHub (on Windows: this build's own repository)
        _logo_path = Path(__file__).parent / 'images' / 'asm_logo.png'
        _logo_px = QPixmap(str(_logo_path))
        _repo_url = branding.REPO_URL if IS_WINDOWS else branding.UPSTREAM_ASM_URL
        if not _logo_px.isNull():
            logo_lbl = QLabel()
            logo_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            logo_lbl.setStyleSheet("background: transparent;")
            logo_lbl.setPixmap(_logo_px.scaledToWidth(55, Qt.TransformationMode.SmoothTransformation))
            logo_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            logo_lbl.setToolTip("Arctis Sound Manager on GitHub")
            logo_lbl.mousePressEvent = lambda _: QDesktopServices.openUrl(QUrl(_repo_url))
            _bottom_layout.addWidget(logo_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Support label
        support_lbl = QLabel(I18n.translate('ui', 'support_me') if IS_WINDOWS else "You like it ? Support me !")
        support_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        support_lbl.setStyleSheet("color: #aaaaaa; font-size: 7pt; background: transparent;")
        _bottom_layout.addWidget(support_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Donation button — compact. On Windows it opens this build's own donation page (BoomFi); the original's
        # Ko-fi button stays on the other systems.
        if IS_WINDOWS:
            donate_btn = QPushButton(I18n.translate('ui', 'donate_button'))
            donate_btn.setObjectName("donateBtn")
            donate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            donate_btn.setToolTip(I18n.translate('ui', 'donate_tooltip'))
            donate_btn.setStyleSheet("""
                QPushButton#donateBtn {
                    background-color: #F2A900;
                    color: #1b1b1b;
                    border: none;
                    border-radius: 4px;
                    padding: 4px 10px;
                    font-size: 9pt;
                    font-weight: bold;
                }
                QPushButton#donateBtn:hover { background-color: #FFC233; }
            """)
            donate_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(branding.DONATION_URL)))
            self._donate_button = donate_btn
            _bottom_layout.addWidget(donate_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        else:
            kofi_btn = QPushButton("☕ Ko-fi")
            kofi_btn.setObjectName("kofiBtn")
            kofi_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            kofi_btn.setToolTip("Support me on Ko-fi")
            kofi_btn.setStyleSheet("""
                QPushButton#kofiBtn {
                    background-color: #FF5E5B;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 4px 10px;
                    font-size: 9pt;
                    font-weight: bold;
                }
                QPushButton#kofiBtn:hover { background-color: #e04f4c; }
            """)
            kofi_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://ko-fi.com/W7W31VXIVC")))
            _bottom_layout.addWidget(kofi_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Version label
        from arctis_sound_manager.utils import project_version
        ver_label = QLabel(f"v{project_version()}")
        ver_label.setObjectName("versionLabel")
        ver_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        _bottom_layout.addWidget(ver_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        if IS_WINDOWS:                                  # the block keeps the margins the full-width scroll area dropped
            _bottom_wrap = QWidget()
            _bottom_wrap.setStyleSheet("background: transparent;")
            _bottom_wrap_layout = QVBoxLayout(_bottom_wrap)
            _bottom_wrap_layout.setContentsMargins(15, 0, 15, 0)
            _bottom_wrap_layout.addWidget(_bottom)
            sidebar_layout.addWidget(_bottom_wrap)
        else:
            sidebar_layout.addWidget(_bottom)

        root_layout.addWidget(sidebar)

        # ── Content area ──────────────────────────────────────────────────────
        content_wrapper = QWidget()
        content_layout = QVBoxLayout(content_wrapper)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Stacked pages
        self._stack = QStackedWidget()

        self._home_page      = HomePage()
        self._equalizer_page = EqualizerPage()
        self._headset_page   = HeadsetPage()
        self._dac_page       = DacPage()
        self._device_page    = DevicePage()
        # The Video tab is always present. When Clips is not on, it shows the
        # install screen (what it does, which packages it needs, and a one-click
        # install) instead of the recorder.
        self._clips_page = None if IS_WINDOWS else self._build_clips_page()
        self._help_page      = HelpPage()
        self._about_page     = AboutPage() if IS_WINDOWS else None

        self._theme_editor_page = ThemeEditorPage()

        if IS_WINDOWS:
            # Everything that shapes what the headset plays or picks up lives on the Headset page; the
            # Equalizer page keeps the equaliser (Sonar) only.
            self._headset_page.adopt_processing(self._equalizer_page._sonar_page.detach_processing_cards())

        self._stack.addWidget(self._home_page)        # index 0 → Home
        self._stack.addWidget(self._equalizer_page)   # index 1 → Equalizer
        self._stack.addWidget(self._headset_page)     # index 2 → Headset
        self._stack.addWidget(self._dac_page)         # index 3 → DAC
        if not IS_WINDOWS:
            self._stack.addWidget(self._clips_page)   # index 4 → Clips
        # From here on the numbers are PAGE_SETTINGS, PAGE_HELP and PAGE_THEME_EDITOR (one lower on
        # Windows, where there is no Clips page).
        self._stack.addWidget(self._device_page)      # Settings
        self._stack.addWidget(self._help_page)        # Help
        if IS_WINDOWS:
            self._stack.addWidget(self._about_page)   # About (Windows build, under Help)
        self._stack.addWidget(self._theme_editor_page) # Theme Editor

        content_layout.addWidget(self._stack)
        root_layout.addWidget(content_wrapper, stretch=1)

        # After the stack exists, not with the sidebar buttons: this reads the
        # current page to move off Clips when the feature is off, and the
        # sidebar is built long before there is a stack to ask.
        self.apply_clips_visibility()

        return window

    # ── Page switching ────────────────────────────────────────────────────────

    def _switch_page(self, index: int):
        self._stack.setCurrentIndex(index)
        # The theme editor has no button of its own; it lights up Settings.
        active_idx = PAGE_SETTINGS if index == PAGE_THEME_EDITOR else index
        for i, btn in enumerate(self._sidebar_buttons):
            btn.set_active(i == active_idx)
            if i == active_idx and getattr(self, "_sidebar_nav", None) is not None:
                self._sidebar_nav.ensureWidgetVisible(btn)      # a short window scrolls to the page it opened

    def apply_clips_visibility(self) -> None:
        """Make sure the Video tab is present, and showing the right face.

        The entry is never hidden. When Clips is off the tab *is* the
        explanation and the way to turn it on, so there is no state in which
        hiding it would be right — and hiding it was how the feature stayed
        invisible to everyone who did not already know it existed.

        The button is kept in place rather than removed for the same reason it
        always was: every sidebar index is also its stack index, so dropping an
        entry would silently renumber Settings and Help. Keeping the page costs
        nothing — ClipsPage builds no capture in its constructor and no clip
        module imports GStreamer at module level, which is what lets a machine
        without any of it still build this window.
        """
        if IS_WINDOWS:
            # Clips records through PipeWire and the Wayland/X11 screen-cast portals; none of
            # that exists on Windows. The tab, its page and its code path are not built at all.
            return
        buttons = getattr(self, "_sidebar_buttons", None) or []
        if PAGE_CLIPS < len(buttons):
            buttons[PAGE_CLIPS].setVisible(True)

        # Clips can be switched on or off from the tab itself while the window
        # is open, and the tab has to follow without a restart.
        self._sync_clips_page()

    def _build_clips_page(self):
        """The recorder when Clips is on and usable, the install screen when it
        is not, wired so either can hand over to the other."""
        if IS_WINDOWS:
            return None

        from arctis_sound_manager.gui import clips_setup
        from arctis_sound_manager.gui.clips_install_page import ClipsInstallPage

        if clips_setup.clips_active():
            page = ClipsPage()
            disabled = getattr(page, "clips_disabled", None)
            if disabled is not None:
                disabled.connect(self._sync_clips_page)
            return page

        page = ClipsInstallPage()
        page.clips_installed.connect(self._sync_clips_page)
        return page

    def _sync_clips_page(self) -> None:
        """Put the face the tab is showing back in step with the feature.

        Called after an install, an uninstall, or the Settings toggle. Swapping
        in place is what keeps this from needing a restart; if the recorder
        cannot be built in this process — a fresh `gi` import failing mid-run
        after the packages landed — the install screen stays up and asks for a
        restart rather than taking the window down with it.
        """
        if IS_WINDOWS:
            return
        stack = getattr(self, "_stack", None)
        old = getattr(self, "_clips_page", None)
        if stack is None or old is None:
            return

        from arctis_sound_manager.gui import clips_setup
        from arctis_sound_manager.gui.clips_install_page import ClipsInstallPage

        wants_recorder = clips_setup.clips_active()
        showing_recorder = not isinstance(old, ClipsInstallPage)
        if wants_recorder == showing_recorder:
            return

        # Read before the swap: removing the old page renumbers the stack, so
        # asking afterwards cannot tell "the user was looking at Clips" from
        # "the indices moved under them".
        was_on_clips = stack.currentWidget() is old

        try:
            new_page = self._build_clips_page()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "clips is on but the recorder could not be built in-process: %s", exc)
            status = getattr(old, "_status", None)
            if status is not None:
                status.setText(I18n.translate("ui", "clips_install_restart"))
            return

        # Hand back what the recorder holds outside this process before the
        # widget goes: the ScreenCast portal session and the compositor's global
        # shortcut both outlive a deleted QWidget, so switching Clips off and on
        # again used to leave the old session and keybinding behind — a second
        # capture then contends with a recorder nobody can see. deleteLater()
        # alone never reached shutdown().
        stop = getattr(old, "shutdown", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001
                self.logger.debug("clips page did not shut down cleanly",
                                  exc_info=True)

        idx = stack.indexOf(old)
        if idx < 0:
            idx = PAGE_CLIPS
        stack.insertWidget(idx, new_page)
        stack.removeWidget(old)
        old.deleteLater()
        self._clips_page = new_page
        if hasattr(new_page, "apply_theme"):
            try:
                new_page.apply_theme()
            except Exception:  # noqa: BLE001
                pass
        # Turning it on from the tab lands on the recorder; turning it off
        # leaves whoever did it from Settings where they were.
        if was_on_clips or wants_recorder:
            self._switch_page(PAGE_CLIPS)

    def restart_on_new_code(self, reason: str) -> None:
        """Replace this process with one running the code now on disk.

        Reached through the tray (QSystrayApp.restart_on_new_code), from its
        staleness poll or from the package scriptlet knocking on the
        single-instance socket (`asm-gui --restart`). Restart
        whatever is open: the banner used to wait for a click, and a tray
        running yesterday's code next to daemons already on today's is
        exactly the half-upgraded state an upgrade exists to end — the
        capture and the shortcut in particular are the tray's, and stayed on
        the old code for the whole session. Release what must not be
        inherited across the exec — the capture's portal session, the
        encoder — and come back on the code now on disk, same pid, same
        tray slot.
        """
        self.logger.info("%s — restarting on the new code", reason)
        shutdown = getattr(getattr(self, "_clips_page", None), "shutdown", None)
        if shutdown is not None:
            try:
                shutdown()
            except Exception:  # noqa: BLE001
                self.logger.debug("clips page shutdown failed", exc_info=True)
        try:
            self.main_window.close()
        except Exception:  # noqa: BLE001
            pass
        from arctis_sound_manager.runtime_staleness import restart_gui
        restart_gui()

    # ── Theme editor ──────────────────────────────────────────────────────────

    def _open_theme_editor_new(self) -> None:
        self._theme_before_edit = self._general_settings.theme
        self._theme_editor_page.open_for_new(base_theme_id=self._general_settings.theme)
        self._switch_page(PAGE_THEME_EDITOR)

    def _open_theme_editor_edit(self, theme_id: str) -> None:
        self._theme_before_edit = self._general_settings.theme
        self._theme_editor_page.open_for_edit(theme_id)
        self._switch_page(PAGE_THEME_EDITOR)

    def _on_theme_preview(self, colors: dict) -> None:
        set_preview_colors(colors)
        self._apply_theme(PREVIEW_THEME_ID, save=False)

    def _on_theme_saved(self, theme_id: str) -> None:
        set_preview_colors(None)
        self._device_page.refresh_theme_combo(theme_id)
        self._apply_theme(theme_id)   # save=True → persiste
        self._switch_page(PAGE_SETTINGS)

    def _on_theme_editor_cancelled(self) -> None:
        set_preview_colors(None)
        self._apply_theme(self._theme_before_edit, save=False)
        self._switch_page(PAGE_SETTINGS)

    # ── Public API (called by systray_app) ────────────────────────────────────

    def start_sync(self):
        self.logger.info("Starting Main Window app.")
        self.main_window.show()
        self.app.exec()

    async def start(self):
        self.start_sync()

    # ── D-Bus signal handlers ─────────────────────────────────────────────────

    def on_settings_received(self, settings: dict):
        if settings == self.settings:
            return
        self.settings = settings
        has_dac = settings.get('has_dac', False)
        self._sidebar_buttons[PAGE_DAC].setVisible(has_dac)
        if not has_dac and self._stack.currentIndex() == PAGE_DAC:
            self._switch_page(PAGE_HOME)

        # Headsets with no on-device equaliser can't do anything with the
        # custom band sliders; only offer the switch to those that can (#146).
        if 'has_hardware_eq' in settings:
            self._equalizer_page.set_hardware_eq_available(
                bool(settings['has_hardware_eq']))
        if 'hardware_eq_range_db' in settings:
            self._equalizer_page.set_hardware_eq_range(
                int(settings['hardware_eq_range_db']))

        # Daemon flagged a USB EACCES on the currently-attached device. The
        # rules file might be valid (so the startup dialog at gui.py:142
        # didn't fire) but they weren't applied to this device because it
        # was plugged in before they took effect. Offer a one-click reload.
        if settings.get('permission_error') and not getattr(self, '_perm_dialog_shown', False):
            self._perm_dialog_shown = True
            from arctis_sound_manager.gui.udev_dialog import UdevRulesDialog
            from PySide6.QtWidgets import QDialog
            dlg = UdevRulesDialog(parent=self.main_window, mode='reload')
            # Kept on the instance so the branch below can dismiss it: the
            # daemon goes on retrying while this is open, so udev landing late
            # (or the user fixing it from another window) can make the dialog
            # obsolete while the user is still reading it. Asking someone to
            # act on a problem that no longer exists is its own bug.
            self._perm_dialog = dlg
            try:
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    self.dbus_wrapper.reload_configs()
                    self._perm_dialog_shown = False
            finally:
                self._perm_dialog = None
        elif not settings.get('permission_error'):
            self._perm_dialog_shown = False
            dlg = getattr(self, '_perm_dialog', None)
            if dlg is not None:
                self.logger.info(
                    "USB access recovered while the permissions dialog was "
                    "open — closing it, there is nothing left to fix."
                )
                dlg.reject()

    def on_status_received(self, status: dict):
        if status == self.status:
            return
        self.status = status

    @Slot(object)
    def _on_apply_profile(self, profile) -> None:
        from arctis_sound_manager.profile_manager import apply_profile
        apply_profile(profile)
        self._home_page.reload_channel_visibility()
        # Trigger EQ re-apply (single pipewire restart for all 3 channels)
        self._equalizer_page._sonar_page.apply_all_from_files()

    @Slot()
    def _on_profiles_changed(self) -> None:
        pass  # reserved for systray refresh

    @Slot()
    def sig_stop(self):
        if getattr(self, "_stopping", False):
            return
        self._stopping = True
        self.dbus_wrapper.stop()
        self.logger.debug("Received shutdown signal, shutting down.")
        # Release the clip capture before the interpreter starts tearing
        # objects down. Left to finalisation, the GStreamer pipeline and the
        # portal session died with the process — the GUI itself SEGV'd in
        # _gi on the way out, and the compositor's end of the screencast was
        # cut mid-frame, which is what took plasmashell down with it on
        # every Exit.
        shutdown = getattr(getattr(self, "_clips_page", None), "shutdown", None)
        if shutdown is not None:
            try:
                shutdown()
            except Exception:  # noqa: BLE001 — quitting must not depend on it
                self.logger.debug("clips page shutdown failed", exc_info=True)
        self.app.quit()
