"""Which build is this? The installer build writes the commit and the time into ``build_id.txt`` next to the code, and
every process says so when it starts, so a log sent in for a problem names the build it came from."""
from __future__ import annotations

from pathlib import Path

_cached: str | None = None


def _file() -> Path:
    return Path(__file__).resolve().parents[1] / "build_id.txt"


def build_id() -> str:
    """``abc1234 (2026-10-27 01:30 UTC)`` for an installer build, ``unknown`` for a source checkout."""
    global _cached
    if _cached is None:
        try:
            text = _file().read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        _cached = text or "unknown"
    return _cached
