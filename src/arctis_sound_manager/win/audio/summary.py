# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""One line about how the audio engine is doing, for the status banner and the set-up assistant."""
from __future__ import annotations

from dataclasses import dataclass

from arctis_sound_manager.win.audio.strings import t

_NAMES = {"in:game": "Game", "in:chat": "Chat", "in:media": "Media", "in:aux": "Aux", "out:headset": "Headset",
          "out:stream": "Stream", "mic:in": "Mic", "mic:out": "Mic out"}


@dataclass(frozen=True)
class EngineState:
    code: str            # off | unconfigured | stopped | starting | problem | ok
    text: str

    @property
    def healthy(self) -> bool:
        return self.code == "ok"


def _problems(status: dict) -> list[str]:
    found = []
    for key, ep in (status.get("endpoints") or {}).items():
        if ep.get("state") != "running":
            found.append(f"{_NAMES.get(key, key)}: {ep.get('error') or ep.get('state', 'closed')}")
    return found


def engine_state(status: dict | None, enabled: bool = True, configured: bool = True, lang: str | None = None) -> EngineState:
    """Summarise the engine. ``status`` is its ``status`` reply (``None`` when it is not running)."""
    if not enabled:
        return EngineState("off", t("banner_off", lang))
    if status is None:
        return EngineState("stopped" if configured else "unconfigured",
                           t("banner_stopped" if configured else "banner_unconfigured", lang))
    if status.get("problems") and not status.get("running"):
        return EngineState("unconfigured", t("banner_unconfigured", lang))
    if not status.get("running"):
        return EngineState("starting", t("banner_starting", lang))
    problems = _problems(status)
    if problems:
        return EngineState("problem", t("banner_problem", lang, problem="; ".join(problems[:3])))
    return EngineState("ok", t("banner_ok", lang, profile=status.get("profile", "balanced"),
                               latency=int(round(float(status.get("latency_ms", 0))))))


def endpoint_lines(status: dict | None) -> list[str]:
    """``Game: running (CABLE-A Output ...)`` for every endpoint, for the assistant's detail box."""
    lines = []
    for key, ep in ((status or {}).get("endpoints") or {}).items():
        detail = ep.get("device") or ep.get("error") or ""
        lines.append(f"{_NAMES.get(key, key)}: {ep.get('state', 'closed')}" + (f" ({detail})" if detail else ""))
    return lines
