"""The engine's one-line state, and the translated screen texts."""
from __future__ import annotations

from arctis_sound_manager.win.audio import strings
from arctis_sound_manager.win.audio.summary import endpoint_lines, engine_state


def running(**over):
    base = {"running": True, "profile": "balanced", "latency_ms": 69.96,
            "endpoints": {"in:game": {"state": "running", "device": "CABLE-A Output"},
                          "out:headset": {"state": "running", "device": "Headset Earphone"}}}
    base.update(over)
    return base


def test_a_healthy_engine_reports_profile_and_latency():
    s = engine_state(running())
    assert s.code == "ok" and s.healthy and "balanced" in s.text and "70 ms" in s.text


def test_switched_off_wins_over_everything():
    assert engine_state(running(), enabled=False).code == "off"
    assert engine_state(None, enabled=False).code == "off"


def test_no_engine_is_stopped_or_unconfigured_depending_on_the_cables():
    assert engine_state(None, configured=True).code == "stopped"
    assert engine_state(None, configured=False).code == "unconfigured"


def test_an_idle_engine_waiting_for_a_configuration_is_unconfigured():
    assert engine_state({"running": False, "problems": ["no headset"]}).code == "unconfigured"
    assert engine_state({"running": False, "problems": []}).code == "starting"


def test_a_missing_device_is_a_problem_naming_it():
    status = running(endpoints={"in:game": {"state": "running"},
                                "out:headset": {"state": "waiting", "error": "no such device"}})
    s = engine_state(status)
    assert s.code == "problem" and "Headset: no such device" in s.text and not s.healthy


def test_endpoint_lines_list_every_device():
    lines = endpoint_lines(running())
    assert lines == ["Game: running (CABLE-A Output)", "Headset: running (Headset Earphone)"]
    assert endpoint_lines(None) == []


def test_turkish_texts_exist_for_every_english_key():
    assert set(strings._TR) == set(strings._EN)


def test_every_text_formats_with_its_own_fields():
    fields = {"profile": "p", "latency": 1, "problem": "x", "error": "e", "name": "n", "ms": 5, "problems": "q"}
    for table in strings._TABLES.values():
        for key, text in table.items():
            text.format(**fields)                                    # no stray {braces}, no unknown fields


def test_language_fallback_and_formatting():
    assert strings.t("banner_start", "tr") == "Başlat"
    assert strings.t("banner_start", "fr") == "Start"                # unknown language: English
    assert strings.t("banner_start", None) == "Start"
    assert strings.t("no-such-key") == "no-such-key"
    assert strings.t("banner_problem", "tr", problem="X") == "Ses motoru: X"
    assert strings.t("banner_problem", "en") == "Audio engine: {problem}"        # missing field: text returned as is
    assert strings.t("banner_ok", "tr_TR", profile="safe", latency=90).startswith("Ses motoru çalışıyor")
