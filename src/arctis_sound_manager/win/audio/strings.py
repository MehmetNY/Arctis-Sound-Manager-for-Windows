# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The few texts of the Windows audio-engine screens (Settings card, Devices window).

Kept in code, English and Turkish, rather than in the translation files that are synchronised with the
translators' platform: these screens exist only on Windows. A language without an entry falls back to
English.
"""
from __future__ import annotations

_EN = {
    "state_ok": "Audio engine running - {profile} latency, about {latency} ms.",
    "state_starting": "The audio engine is starting...",
    "state_stopped": "The audio engine is not running. Switch it off and on again to restart it.",
    "state_off": "The audio engine is switched off: programs play straight to Windows' output, without ASM's processing.",
    "state_unconfigured": "No headset output was found yet. Connect the headset, or choose a device under Devices...",
    "state_problem": "Audio engine: {problem}",
    "btn_devices": "Devices...",
    "dialog_title": "Audio devices",
    "dialog_intro": (
        "ASM finds your headset and microphone by itself - change them here only if it picked the wrong ones. "
        "Programs need no setup: ASM takes each program's sound from Windows, sorts it into Game, Chat, Media "
        "or Aux, applies the equalizer and spatial audio and plays the result to the headset."),
    "scanning": "Looking for sound devices...",
    "scan_failed": "Could not list the sound devices: {error}",
    "no_cable": ("No virtual cable found, so the processed microphone is not available. ASM's setup installs it "
                 "(VB-CABLE): run the setup again and keep that option ticked."),
    "label_headset": "Headset (playback)",
    "label_stream": "Stream mix output (optional)",
    "label_mic_in": "Microphone",
    "label_mic_out": "Processed microphone (virtual cable)",
    "label_latency": "Latency / stability",
    "label_limiter": "Protect the headset with a limiter",
    "mic_out_hint": "In Discord, OBS, games and other programs choose \"CABLE Output\" as the microphone to use the processed one.",
    "vbcable_note": (
        "The processed microphone uses VB-CABLE by VB-Audio Software, installed with ASM. It is donationware: "
        "if it is useful to you, please support its author at <a href=\"https://vb-audio.com/Cable/\">vb-audio.com</a>."),
    "none": "(none)",
    "not_found": "{name} (not found)",
    "latency_low": "Low - about {ms} ms, highest CPU use",
    "latency_balanced": "Balanced - about {ms} ms, good for games and music",
    "latency_safe": "Safe - about {ms} ms, fewest dropouts on slow computers",
    "btn_automatic": "Choose automatically",
    "btn_save": "Save and apply",
    "btn_close": "Close",
    "saved": "Saved. The audio engine is running with these devices.",
    "saved_automatic": "ASM chose the devices again and applied them. It will keep following your devices.",
    "saved_problems": "Saved, but the engine cannot start yet: {problems}",
    "start_failed": "Saved, but the audio engine could not be started.",
    "nothing_to_save": "Choose a headset first.",
    "engine_setting": "Process sound with the ASM audio engine",
    "engine_setting_hint": "Switched off, ASM does not process any sound. Switching it off and on again restarts the engine.",
    "card_title": "Audio engine",
}

_TR = {
    "state_ok": "Ses motoru çalışıyor - {profile} gecikme profili, yaklaşık {latency} ms.",
    "state_starting": "Ses motoru başlatılıyor...",
    "state_stopped": "Ses motoru çalışmıyor. Yeniden başlatmak için kapatıp tekrar açın.",
    "state_off": "Ses motoru kapalı: programlar sesi ASM işlemeden doğrudan Windows çıkışına çalar.",
    "state_unconfigured": "Henüz bir kulaklık çıkışı bulunamadı. Kulaklığı bağlayın ya da Aygıtlar... altından bir aygıt seçin.",
    "state_problem": "Ses motoru: {problem}",
    "btn_devices": "Aygıtlar...",
    "dialog_title": "Ses aygıtları",
    "dialog_intro": (
        "ASM kulaklığınızı ve mikrofonunuzu kendisi bulur - yanlış olanları seçtiyse burada değiştirin. "
        "Programlar için ayar gerekmez: ASM her programın sesini Windows'tan alır, Oyun, Sohbet, Medya veya Aux "
        "kanalına ayırır, ekolayzır ve uzamsal sesi uygular, sonucu kulaklığa çalar."),
    "scanning": "Ses aygıtları aranıyor...",
    "scan_failed": "Ses aygıtları listelenemedi: {error}",
    "no_cable": ("Sanal kablo bulunamadı, bu yüzden işlenmiş mikrofon kullanılamıyor. ASM'nin kurulumu onu "
                 "(VB-CABLE) kurar: kurulumu yeniden çalıştırıp o seçeneği işaretli bırakın."),
    "label_headset": "Kulaklık (oynatma)",
    "label_stream": "Yayın karışımı çıkışı (isteğe bağlı)",
    "label_mic_in": "Mikrofon",
    "label_mic_out": "İşlenmiş mikrofon (sanal kablo)",
    "label_latency": "Gecikme / kararlılık",
    "label_limiter": "Kulaklığı sınırlayıcıyla koru",
    "mic_out_hint": "İşlenmiş mikrofonu kullanmak için Discord, OBS, oyunlar ve diğer programlarda mikrofon olarak \"CABLE Output\" seçin.",
    "vbcable_note": (
        "İşlenmiş mikrofon, ASM ile birlikte kurulan VB-Audio Software'in VB-CABLE'ını kullanır. Bağış (donationware) "
        "ile dağıtılır: işinize yarıyorsa yazarını <a href=\"https://vb-audio.com/Cable/\">vb-audio.com</a> "
        "üzerinden desteklemenizi rica ederiz."),
    "none": "(yok)",
    "not_found": "{name} (bulunamadı)",
    "latency_low": "Düşük - yaklaşık {ms} ms, en yüksek CPU kullanımı",
    "latency_balanced": "Dengeli - yaklaşık {ms} ms, oyun ve müzik için iyi",
    "latency_safe": "Güvenli - yaklaşık {ms} ms, yavaş bilgisayarlarda en az kesinti",
    "btn_automatic": "Otomatik seç",
    "btn_save": "Kaydet ve uygula",
    "btn_close": "Kapat",
    "saved": "Kaydedildi. Ses motoru bu aygıtlarla çalışıyor.",
    "saved_automatic": "ASM aygıtları yeniden seçip uyguladı. Aygıtlarınızı izlemeye devam edecek.",
    "saved_problems": "Kaydedildi ama motor henüz başlayamıyor: {problems}",
    "start_failed": "Kaydedildi ama ses motoru başlatılamadı.",
    "nothing_to_save": "Önce bir kulaklık seçin.",
    "engine_setting": "Sesi ASM ses motoruyla işle",
    "engine_setting_hint": "Kapalıyken ASM hiçbir sesi işlemez. Kapatıp yeniden açmak motoru yeniden başlatır.",
    "card_title": "Ses motoru",
}

_TABLES = {"en": _EN, "tr": _TR}


def t(key: str, lang: str | None = None, **fmt) -> str:
    """The text for *key* in *lang* (English when the language or the key is missing), with ``{fields}`` filled in."""
    table = _TABLES.get((lang or "en")[:2].lower(), _EN)
    text = table.get(key) or _EN.get(key) or key
    try:
        return text.format(**fmt) if fmt else text
    except (KeyError, IndexError, ValueError):
        return text
