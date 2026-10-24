# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The few texts of the Windows audio-engine screens (set-up assistant, status banner).

Kept in code, English and Turkish, rather than in the translation files that are synchronised with the
translators' platform: these screens exist only on Windows. A language without an entry falls back to
English.
"""
from __future__ import annotations

_EN = {
    "banner_ok": "Audio engine running - {profile} latency profile, about {latency} ms.",
    "banner_starting": "The audio engine is starting...",
    "banner_stopped": "The audio engine is not running. Channel mixing, ChatMix, equalizer and spatial audio need it.",
    "banner_off": "The audio engine is switched off, so the sound is not processed.",
    "banner_unconfigured": "Audio engine not set up yet: on Windows ASM needs virtual audio cables to process your sound.",
    "banner_problem": "Audio engine: {problem}",
    "banner_setup": "Set up...",
    "banner_start": "Start",
    "dialog_title": "Audio engine setup",
    "dialog_intro": (
        "On Windows ASM processes your sound with its own audio engine. Each channel needs a virtual audio "
        "cable (for example the free VB-CABLE A, B, C and D from vb-audio.com): programs play into a cable's "
        "Input end, the engine reads the same cable's Output end, applies equalizer, spatial audio and "
        "ChatMix, and plays the result to your headset. One more cable carries your processed microphone."),
    "scanning": "Looking for sound devices...",
    "scan_failed": "Could not list the sound devices: {error}",
    "no_cables": ("No virtual audio cables found. Install VB-CABLE (A, B, C, D), restart Windows and open "
                  "this window again."),
    "label_game": "Game channel (cable output)",
    "label_chat": "Chat channel (cable output)",
    "label_media": "Media channel (cable output)",
    "label_aux": "Aux channel (cable output, optional)",
    "label_headset": "Headset (playback)",
    "label_stream": "Stream mix output (optional)",
    "label_mic_in": "Microphone",
    "label_mic_out": "Processed microphone (cable input)",
    "label_latency": "Latency / stability",
    "label_limiter": "Protect the headset with a limiter",
    "none": "(none)",
    "not_found": "{name} (not found)",
    "latency_low": "Low - about {ms} ms, highest CPU use",
    "latency_balanced": "Balanced - about {ms} ms, good for games and music",
    "latency_safe": "Safe - about {ms} ms, fewest dropouts on slow computers",
    "btn_detect": "Detect automatically",
    "btn_save": "Save and apply",
    "btn_test": "Test",
    "btn_close": "Close",
    "saved": "Saved. The audio engine is running with these devices.",
    "saved_waiting": "Saved. The engine is starting; this window will update when it is ready.",
    "saved_problems": "Saved, but the engine cannot start yet: {problems}",
    "start_failed": "Saved, but the audio engine could not be started.",
    "nothing_to_save": "Pick at least a headset and one channel cable.",
    "testing": "Testing (this takes a few seconds)...",
    "next_steps": (
        "Next: choose the cable's Input as the playback device of your programs - for example set Windows' "
        "default output to the Media cable (Settings > System > Sound) and pick the Game cable for your games "
        "in Settings > System > Sound > Volume mixer. In Discord, OBS and other recorders choose the "
        "processed-microphone cable's Output as the microphone."),
    "engine_setting": "Process sound with the ASM audio engine",
    "engine_setting_hint": "Switched off, programs that play into the virtual cables are silent until you switch it on again.",
    "card_title": "Audio engine",
}

_TR = {
    "banner_ok": "Ses motoru çalışıyor - {profile} gecikme profili, yaklaşık {latency} ms.",
    "banner_starting": "Ses motoru başlatılıyor...",
    "banner_stopped": "Ses motoru çalışmıyor. Kanal karıştırma, ChatMix, ekolayzır ve uzamsal ses ona bağlı.",
    "banner_off": "Ses motoru kapalı; ses işlenmiyor.",
    "banner_unconfigured": "Ses motoru henüz kurulmadı: Windows'ta ASM sesi işlemek için sanal ses kablolarına ihtiyaç duyar.",
    "banner_problem": "Ses motoru: {problem}",
    "banner_setup": "Kurulum...",
    "banner_start": "Başlat",
    "dialog_title": "Ses motoru kurulumu",
    "dialog_intro": (
        "Windows'ta ASM sesinizi kendi ses motoruyla işler. Her kanal için bir sanal ses kablosu gerekir "
        "(örneğin vb-audio.com'dan ücretsiz VB-CABLE A, B, C ve D): programlar kablonun Input ucuna çalar, "
        "motor aynı kablonun Output ucunu okuyup ekolayzır, uzamsal ses ve ChatMix uygular ve sonucu "
        "kulaklığınıza çalar. Bir kablo daha işlenmiş mikrofonunuzu taşır."),
    "scanning": "Ses aygıtları aranıyor...",
    "scan_failed": "Ses aygıtları listelenemedi: {error}",
    "no_cables": ("Sanal ses kablosu bulunamadı. VB-CABLE'ı (A, B, C, D) kurun, Windows'u yeniden başlatın "
                  "ve bu pencereyi yeniden açın."),
    "label_game": "Oyun kanalı (kablo çıkışı)",
    "label_chat": "Sohbet kanalı (kablo çıkışı)",
    "label_media": "Medya kanalı (kablo çıkışı)",
    "label_aux": "Aux kanalı (kablo çıkışı, isteğe bağlı)",
    "label_headset": "Kulaklık (oynatma)",
    "label_stream": "Yayın karışımı çıkışı (isteğe bağlı)",
    "label_mic_in": "Mikrofon",
    "label_mic_out": "İşlenmiş mikrofon (kablo girişi)",
    "label_latency": "Gecikme / kararlılık",
    "label_limiter": "Kulaklığı sınırlayıcıyla koru",
    "none": "(yok)",
    "not_found": "{name} (bulunamadı)",
    "latency_low": "Düşük - yaklaşık {ms} ms, en yüksek CPU kullanımı",
    "latency_balanced": "Dengeli - yaklaşık {ms} ms, oyun ve müzik için iyi",
    "latency_safe": "Güvenli - yaklaşık {ms} ms, yavaş bilgisayarlarda en az kesinti",
    "btn_detect": "Otomatik algıla",
    "btn_save": "Kaydet ve uygula",
    "btn_test": "Sına",
    "btn_close": "Kapat",
    "saved": "Kaydedildi. Ses motoru bu aygıtlarla çalışıyor.",
    "saved_waiting": "Kaydedildi. Motor başlıyor; hazır olunca bu pencere güncellenecek.",
    "saved_problems": "Kaydedildi ama motor henüz başlayamıyor: {problems}",
    "start_failed": "Kaydedildi ama ses motoru başlatılamadı.",
    "nothing_to_save": "En az bir kulaklık ve bir kanal kablosu seçin.",
    "testing": "Sınanıyor (birkaç saniye sürer)...",
    "next_steps": (
        "Sonra: programlarınızın oynatma aygıtı olarak kablonun Input ucunu seçin - örneğin Windows'un "
        "varsayılan çıkışını Medya kablosuna ayarlayın (Ayarlar > Sistem > Ses) ve oyunlarınız için "
        "Ayarlar > Sistem > Ses > Ses düzeyi karıştırıcısında Oyun kablosunu seçin. Discord, OBS gibi "
        "kayıt programlarında mikrofon olarak işlenmiş-mikrofon kablosunun Output ucunu seçin."),
    "engine_setting": "Sesi ASM ses motoruyla işle",
    "engine_setting_hint": "Kapalıyken sanal kablolara çalan programlar, motoru yeniden açana kadar sessiz kalır.",
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
