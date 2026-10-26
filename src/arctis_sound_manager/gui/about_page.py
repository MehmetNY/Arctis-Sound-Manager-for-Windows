# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""About page: what this program is, where it lives, who built what it stands on, and how to say thanks.

The text follows the app's language (English by default, Turkish when selected); links and the donation page come
from :mod:`arctis_sound_manager.branding`.
"""
from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

import arctis_sound_manager.gui.theme as _theme
from arctis_sound_manager import branding
from arctis_sound_manager.gui.components import DividerLine
from arctis_sound_manager.i18n import I18n

# Each section: heading, body (URLs in it become links), and optionally buttons: (label, url, style) where style is
# "link" (quiet) or "donate" (the highlighted one).
ABOUT_CONTENT: dict[str, dict] = {
    "en": {
        "title": "About",
        "subtitle": "Arctis Sound Manager for Windows",
        "sections": [
            {
                "heading": "What this is",
                "body": (
                    "Arctis Sound Manager (ASM) for Windows is a free, open-source alternative to SteelSeries GG "
                    "for SteelSeries Arctis headsets and base stations: channels per application (Game, Chat, "
                    "Media, Aux), an equalizer with presets of your own, spatial audio, microphone processing "
                    "with noise cancelling, ChatMix and control of the DAC's screen.\n\n"
                    "It runs quietly in the background, needs no account and collects no data. The only network "
                    "request it makes is the check for a new release on GitHub."
                ),
            },
            {
                "heading": "Reach me",
                "body": (
                    "The source code, the releases and the bug reports live here: " + branding.REPO_URL + "\n\n"
                    "Questions, ideas and problems are welcome - open an issue there and it reaches me."
                ),
                "buttons": [("GitHub repository", branding.REPO_URL, "link"),
                            ("Report an issue", branding.ISSUES_URL, "link")],
            },
            {
                "heading": "Make it your own",
                "body": (
                    "ASM is free software (GPL-3.0-or-later). Fork it and shape it however you like - the "
                    "license only asks that you keep it open in the same way."
                ),
            },
            {
                "heading": "Thank you",
                "body": (
                    "This program stands on the shoulders of two projects, and the biggest thanks belong to "
                    "them:\n\n"
                    "• Linux-Arctis-Manager by Giacomo Furlan (elegos) - " + branding.UPSTREAM_LAM_URL + "\n"
                    "• Arctis-Sound-Manager by loteran - " + branding.UPSTREAM_ASM_URL + "\n\n"
                    "The headset support, the interface and the equalizer come from their work; this Windows "
                    "version is built on it."
                ),
                "buttons": [("Linux-Arctis-Manager", branding.UPSTREAM_LAM_URL, "link"),
                            ("Arctis-Sound-Manager", branding.UPSTREAM_ASM_URL, "link")],
            },
            {
                "heading": "Components",
                "body": (
                    "• VB-CABLE virtual audio device by VB-Audio Software - donationware; it carries the processed "
                    "microphone to other programs. Please support VB-Audio too: https://vb-audio.com/Cable/\n"
                    "• RNNoise (Mozilla / Xiph.Org, BSD-3-Clause) - the noise cancelling\n"
                    "• HeSuVi impulse responses (Matt Gore, MIT) - spatial audio\n\n"
                    "The full notices are in THIRD-PARTY-NOTICES.txt in the installation folder."
                ),
            },
            {
                "heading": "Buy me a coffee",
                "body": (
                    "If you would like to say thank you - or buy me a coffee - you can donate any amount you "
                    "like with the button below. Thank you!"
                ),
                "buttons": [("☕ Donate", branding.DONATION_URL, "donate")],
            },
        ],
    },
    "tr": {
        "title": "Hakkında",
        "subtitle": "Arctis Sound Manager — Windows sürümü",
        "sections": [
            {
                "heading": "Bu program ne?",
                "body": (
                    "Arctis Sound Manager (ASM) Windows sürümü, SteelSeries Arctis kulaklıkları ve taban "
                    "istasyonları için SteelSeries GG'ye ücretsiz ve açık kaynaklı bir alternatiftir: uygulama "
                    "başına kanallar (Oyun, Sohbet, Medya, Aux), kendi ön ayarlarınla ekolayzır, uzamsal ses, "
                    "gürültü engelleme dahil mikrofon işleme, ChatMix ve DAC ekranının denetimi.\n\n"
                    "Arka planda sessizce çalışır, hesap istemez ve veri toplamaz. Yaptığı tek ağ isteği, "
                    "GitHub'da yeni sürüm olup olmadığına bakmaktır."
                ),
            },
            {
                "heading": "Bana ulaşın",
                "body": (
                    "Kaynak kodu, sürümler ve hata bildirimleri burada: " + branding.REPO_URL + "\n\n"
                    "Soru, fikir ve sorunlarınız için oradan bir 'issue' açarak bana ulaşabilirsiniz."
                ),
                "buttons": [("GitHub deposu", branding.REPO_URL, "link"),
                            ("Sorun bildir", branding.ISSUES_URL, "link")],
            },
            {
                "heading": "Kendinize göre şekillendirin",
                "body": (
                    "ASM özgür yazılımdır (GPL-3.0-or-later). Dilediğiniz gibi çatallayıp (fork) kendi "
                    "ihtiyaçlarınıza göre şekillendirebilirsiniz; lisans yalnızca onu aynı şekilde açık "
                    "tutmanızı ister."
                ),
            },
            {
                "heading": "Teşekkür",
                "body": (
                    "Bu program iki emektar projenin omuzları üzerinde duruyor ve en büyük teşekkürü onlar "
                    "hak ediyor:\n\n"
                    "• Giacomo Furlan'ın (elegos) Linux-Arctis-Manager'ı - " + branding.UPSTREAM_LAM_URL + "\n"
                    "• loteran'ın Arctis-Sound-Manager'ı - " + branding.UPSTREAM_ASM_URL + "\n\n"
                    "Kulaklık desteği, arayüz ve ekolayzır onların emeğidir; bu Windows sürümü onların "
                    "üzerine kuruldu."
                ),
                "buttons": [("Linux-Arctis-Manager", branding.UPSTREAM_LAM_URL, "link"),
                            ("Arctis-Sound-Manager", branding.UPSTREAM_ASM_URL, "link")],
            },
            {
                "heading": "Bileşenler",
                "body": (
                    "• VB-Audio Software'in VB-CABLE sanal ses aygıtı - bağışa dayalıdır; işlenmiş mikrofonu "
                    "diğer programlara taşır. Lütfen VB-Audio'ya da destek olun: https://vb-audio.com/Cable/\n"
                    "• RNNoise (Mozilla / Xiph.Org, BSD-3-Clause) - gürültü engelleme\n"
                    "• HeSuVi dürtü yanıtları (Matt Gore, MIT) - uzamsal ses\n\n"
                    "Tüm bildirimler kurulum klasöründeki THIRD-PARTY-NOTICES.txt dosyasındadır."
                ),
            },
            {
                "heading": "Bana bir kahve ısmarlayın",
                "body": (
                    "Teşekkür etmek ya da bana bir kahve ısmarlamak isterseniz, aşağıdaki bağış düğmesiyle "
                    "istediğiniz miktarda bağış yapabilirsiniz. Çok teşekkürler!"
                ),
                "buttons": [("☕ Bağış yap", branding.DONATION_URL, "donate")],
            },
        ],
    },
}


def about_content(lang: str) -> dict:
    return ABOUT_CONTENT.get(lang, ABOUT_CONTENT["en"])


def linkify(body: str, accent: str) -> str:
    """Plain text -> the HTML the labels show: escaped, line breaks kept, URLs clickable."""
    escaped = html.escape(body, quote=False).replace("\n", "<br>")
    return re.sub(r'(https?://[^\s<]+)', rf'<a href="\1" style="color: {accent};">\1</a>', escaped)


def open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


class AboutPage(QWidget):
    _current_lang: str = "en"

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(36, 28, 36, 0)
        header_layout.setSpacing(6)
        self._title_label = QLabel("About")
        header_layout.addWidget(self._title_label)
        self._subtitle_label = QLabel("")
        header_layout.addWidget(self._subtitle_label)
        header_layout.addSpacing(16)
        outer.addWidget(header)
        self._header = header
        outer.addWidget(DividerLine())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll = scroll
        self._content_widget = QWidget()
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._content_layout.setContentsMargins(36, 24, 36, 36)
        self._content_layout.setSpacing(0)
        scroll.setWidget(self._content_widget)
        outer.addWidget(scroll, stretch=1)

        self._set_language(I18n.current_lang())
        I18n.on_language_changed(lambda: self._set_language(I18n.current_lang()))
        self.apply_theme()

    # ── theme ───────────────────────────────────────────────────────────────

    def apply_theme(self, t=None) -> None:
        bg = _theme.c("BG_MAIN")
        self.setStyleSheet(f"background-color: {bg};")
        self._header.setStyleSheet(f"background-color: {bg};")
        self._scroll.setStyleSheet(f"QScrollArea {{ background-color: {bg}; border: none; }}")
        self._content_widget.setStyleSheet(f"background-color: {bg};")
        self._title_label.setStyleSheet(
            f"color: {_theme.c('TEXT_PRIMARY')}; font-size: 28pt; font-weight: bold; background: transparent;")
        self._subtitle_label.setStyleSheet(
            f"color: {_theme.c('TEXT_SECONDARY')}; font-size: 11pt; background: transparent;")
        self._set_language(self._current_lang)               # the cards carry colours: rebuild them

    # ── content ─────────────────────────────────────────────────────────────

    def _set_language(self, lang: str) -> None:
        data = about_content(lang)
        self._current_lang = lang if lang in ABOUT_CONTENT else "en"
        self._title_label.setText(data["title"])
        self._subtitle_label.setText(data["subtitle"])
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)                       # gone now, not whenever the event loop gets to it
                widget.deleteLater()
        for section in data["sections"]:
            self._add_section(section)

    def _add_section(self, section: dict) -> None:
        card = QWidget()
        card.setObjectName("aboutCard")
        card.setStyleSheet(
            f"QWidget#aboutCard {{ background-color: {_theme.c('BG_CARD')}; border-radius: 10px; "
            f"border: 1px solid {_theme.c('BORDER')}; }}")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        heading = QLabel(section["heading"])
        heading.setStyleSheet(
            f"color: {_theme.c('TEXT_PRIMARY')}; font-size: 14pt; font-weight: bold; background: transparent; "
            "border: none; padding: 0;")
        layout.addWidget(heading)

        rule = QWidget()
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background-color: {_theme.c('BORDER')}; border: none;")
        layout.addWidget(rule)

        body = QLabel(linkify(section["body"], _theme.c("ACCENT")))
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        body.setStyleSheet(
            f"color: {_theme.c('TEXT_SECONDARY')}; font-size: 11pt; background: transparent; border: none; "
            "padding: 0;")
        layout.addWidget(body)

        buttons = section.get("buttons")
        if buttons:
            row = QHBoxLayout()
            row.setSpacing(10)
            for label, url, style in buttons:
                row.addWidget(self._button(label, url, style))
            row.addStretch(1)
            layout.addSpacing(4)
            layout.addLayout(row)

        self._content_layout.addWidget(card)
        self._content_layout.addSpacing(12)

    def _button(self, label: str, url: str, style: str) -> QPushButton:
        button = QPushButton(label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(url)
        if style == "donate":
            button.setStyleSheet(
                "QPushButton { background-color: #F2A900; color: #1b1b1b; border: none; border-radius: 6px; "
                "padding: 9px 22px; font-size: 11pt; font-weight: bold; }"
                "QPushButton:hover { background-color: #FFC233; }")
        else:
            button.setStyleSheet(
                f"QPushButton {{ background-color: {_theme.c('BG_BUTTON')}; color: {_theme.c('TEXT_PRIMARY')}; "
                f"border: none; border-radius: 6px; padding: 8px 16px; font-size: 10pt; }}"
                f"QPushButton:hover {{ background-color: {_theme.c('BG_BUTTON_HOVER')}; }}")
        button.clicked.connect(lambda _checked=False, target=url: open_url(target))
        return button
