# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""Automatic Sonar preset per game (the GG "QuickSet" behaviour), Qt-free.

Three pieces:

* identity — turn "a process is playing audio" into a stable game key and a
  human title, from the launcher's own environment (Steam, Lutris, Heroic);
* configuration — which preset the user tied to which game, persisted in
  ``~/.config/arctis_manager/game_presets.json``;
* ``AutoPresetController`` — a pure state machine deciding when to apply a
  preset. The GUI feeds it the detected
  game and the active preset every few seconds and executes what it returns.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from arctis_sound_manager.constants import _XDG_CONFIG

log = logging.getLogger("GamePresets")

# Filesystem roots, module-level so tests can point them at a tmp_path.
CONFIG_FILE = _XDG_CONFIG / "arctis_manager" / "game_presets.json"
STEAM_ROOTS = (
    Path.home() / ".local" / "share" / "Steam",
    Path.home() / ".steam" / "steam",
    Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
)
HEROIC_ROOTS = (
    Path.home() / ".config" / "heroic",
    Path.home() / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config" / "heroic",
)

_STEAM_ENV_KEYS = ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID")


@dataclass(frozen=True)
class DetectedGame:
    key: str      # "steam:<appid>" | "lutris:<name>" | "heroic:<appname>" | "exe:<name>"
    title: str    # human title
    pid: int | None


# --------------------------------------------------------------------------- #
# Title matching
# --------------------------------------------------------------------------- #

def normalize_title(title: str) -> str:
    """Comparable form of a title: no accents, case, punctuation or spacing."""
    kept = []
    for ch in title or "":
        if unicodedata.category(ch)[0] in "PS":
            kept.append(" ")             # punctuation / symbol: ™ ® : - ' ...
            continue                     # (before NFKD, which turns ™ into "TM")
        kept.extend(c for c in unicodedata.normalize("NFKD", ch)
                    if not unicodedata.category(c).startswith("M"))
    return " ".join("".join(kept).casefold().split())


def match_preset(title: str, presets: list[str]) -> str | None:
    """The preset named exactly like the game once normalized — never fuzzy."""
    wanted = normalize_title(title)
    if not wanted:
        return None
    for preset in presets:
        if normalize_title(preset) == wanted:
            return preset
    return None


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict:
    """Minimal Valve KeyValues parser: quoted tokens and braces only."""
    root: dict = {}
    stack = [root]
    key: str | None = None
    for m in _VDF_TOKEN.finditer(text):
        quoted, brace = m.group(1), m.group(2)
        if brace == "{":
            child: dict = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = re.sub(r"\\(.)", r"\1", quoted)
        else:
            stack[-1][key] = re.sub(r"\\(.)", r"\1", quoted)
            key = None
    return root


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _steam_libraries(root: Path) -> list[Path]:
    libs = [root]
    text = _read_text(root / "steamapps" / "libraryfolders.vdf")
    if text:
        folders = next(iter(parse_vdf(text).values()), {})
        if isinstance(folders, dict):
            for entry in folders.values():
                path = entry.get("path") if isinstance(entry, dict) else None
                if path:
                    libs.append(Path(path))
    return libs


def steam_title(appid: str, roots: tuple[Path, ...] | None = None) -> str | None:
    """``"name"`` from ``appmanifest_<appid>.acf`` in any Steam library."""
    for root in (STEAM_ROOTS if roots is None else roots):
        for lib in _steam_libraries(root):
            text = _read_text(lib / "steamapps" / f"appmanifest_{appid}.acf")
            if not text:
                continue
            state = next(iter(parse_vdf(text).values()), {})
            name = state.get("name") if isinstance(state, dict) else None
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def heroic_title(app_name: str, roots: tuple[Path, ...] | None = None) -> str | None:
    """Title of a Heroic game from its ``store_cache`` library files.

    Heroic keeps ``<config>/store_cache/{legendary,gog,nile}_library.json``
    (an object holding the game list under ``library`` or ``games``, each
    entry carrying ``app_name`` and ``title``) and
    ``<config>/sideload_apps/library.json`` for added games.
    """
    names = ("store_cache/legendary_library.json", "store_cache/gog_library.json",
             "store_cache/nile_library.json", "sideload_apps/library.json")
    for root in (HEROIC_ROOTS if roots is None else roots):
        for rel in names:
            text = _read_text(root / rel)
            if not text:
                continue
            try:
                data = json.loads(text)
                lists = [data] if isinstance(data, list) else [
                    v for v in data.values() if isinstance(v, list)]
                for games in lists:
                    for game in games:
                        if game.get("app_name") == app_name:
                            title = game.get("title")
                            if isinstance(title, str) and title.strip():
                                return title.strip()
            except (ValueError, AttributeError, TypeError):
                continue                 # unexpected shape: try the next file
    return None


def identify_game(name: str, pid: int | None,
                  environ: dict[str, str] | None = None) -> DetectedGame:
    """Stable key and human title for a game process."""
    env = environ or {}
    for var in _STEAM_ENV_KEYS:
        appid = (env.get(var) or "").strip()
        if appid.isdigit() and int(appid) != 0:
            return DetectedGame(f"steam:{int(appid)}",
                                steam_title(str(int(appid))) or name, pid)
    lutris = (env.get("GAME_NAME") or "").strip()
    if lutris:
        return DetectedGame(f"lutris:{lutris}", lutris, pid)
    heroic = (env.get("HEROIC_APP_NAME") or "").strip()
    if heroic:
        return DetectedGame(f"heroic:{heroic}", heroic_title(heroic) or name, pid)
    return DetectedGame(f"exe:{name.lower()}", name, pid)


# Helper processes of the launchers that talk to the audio server but are never
# the game. The clip blocklist already covers the Steam client and its helpers.
_CLIENT_EXCLUDED = ("eosoverlayrenderer", "epicwebhelper", "epicgameslauncher",
                    "crashhandler", "gameoverlayui")


def _is_launched_game(env: dict[str, str]) -> bool:
    """Whether a process environment says a launcher started it as a game."""
    for var in _STEAM_ENV_KEYS:
        value = (env.get(var) or "").strip()
        if value.isdigit() and int(value) != 0:
            return True
    return bool((env.get("GAME_NAME") or "").strip()
                or (env.get("HEROIC_APP_NAME") or "").strip())


def _detect_game_client() -> tuple[str, int] | None:
    """A game connected to the audio server that has not opened a stream yet.

    A title is a PulseAudio *client* from launch, long before its first sink
    input; waiting for sound means the preset lands after the menu music
    starts. The client's process is judged like a stream's: under a game
    runtime, or started by a launcher that exports the game's identity.
    """
    try:
        import pulsectl
    except ImportError:
        return None
    from arctis_sound_manager import clip_capture as cc
    try:
        with pulsectl.Pulse("asm-game-preset-clients") as pulse:
            for client in pulse.client_list():
                props = getattr(client, "proplist", None) or {}
                try:
                    pid = int(props.get("application.process.id", ""))
                except (TypeError, ValueError):
                    continue
                name = (props.get("application.name")
                        or props.get("application.process.binary")
                        or getattr(client, "name", "") or "")
                name = name.removesuffix(".exe").strip()
                if (not name or cc._is_not_a_game(name)
                        or any(x in name.lower() for x in _CLIENT_EXCLUDED)):
                    continue
                haystack = cc._process_cmdline(pid) + cc._process_environ(pid)
                if (any(h in haystack for h in cc._GAME_RUNTIME_HINTS)
                        or _is_launched_game(cc.process_environ_dict(pid))):
                    return name, pid
    except Exception as exc:
        log.debug("game client detection failed: %s", exc)
    return None


def detect_current_game() -> DetectedGame | None:
    """The game running right now, if any.

    Strict stream detection first (the game is already playing sound), then
    the audio-server clients (the game just launched and is still silent).
    """
    from arctis_sound_manager.clip_capture import (
        detect_game_process, process_environ_dict)
    found = detect_game_process(strict=True) or _detect_game_client()
    if not found:
        return None
    name, pid = found
    env = process_environ_dict(pid) if pid is not None else {}
    return identify_game(name, pid, env)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def config_path() -> Path:
    return CONFIG_FILE


def load_config() -> dict:
    """``{"enabled": bool, "games": {key: {"title", "preset"}}}``; defaults on any error."""
    cfg: dict = {"enabled": False, "games": {}}
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    cfg["enabled"] = bool(raw.get("enabled", False))
    games = raw.get("games")
    if isinstance(games, dict):
        cfg["games"] = {k: v for k, v in games.items() if isinstance(v, dict)}
    return cfg


def save_config(cfg: dict) -> None:
    path = config_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not save %s: %s", path, exc)


def is_enabled() -> bool:
    return bool(load_config()["enabled"])


def set_enabled(on: bool) -> None:
    cfg = load_config()
    cfg["enabled"] = bool(on)
    save_config(cfg)


def get_association(key: str) -> tuple[bool, str | None]:
    """``(has_entry, preset)``: no entry means automatic, preset None "do not change"."""
    entry = load_config()["games"].get(key)
    if entry is None:
        return False, None
    return True, entry.get("preset")


def set_association(key: str, title: str, preset: str | None, *,
                    automatic: bool = False) -> None:
    cfg = load_config()
    if automatic:
        cfg["games"].pop(key, None)
    else:
        cfg["games"][key] = {"title": title, "preset": preset}
    save_config(cfg)


def target_preset(game: DetectedGame, game_presets: list[str]) -> str | None:
    """Association first (None = do not change), else a title match."""
    has_entry, preset = get_association(game.key)
    if has_entry:
        return preset
    return match_preset(game.title, game_presets)


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #

@dataclass
class Action:
    kind: str          # "apply" | "remember" (store as the game's association)
    preset: str
    game: DetectedGame | None


class AutoPresetController:
    """Decides when to apply a game's preset and when to remember a manual one.

    The preset is never put back when the game closes: it stays until the user
    or the next game changes it. A preset the user picks by hand while the game
    runs is reported once as a ``"remember"`` action so it becomes that game's
    association; nothing is applied for it. Only ticks where the game is
    present can remember: a change made after it closed is never attributed
    to it.
    """

    def __init__(self, resolve_target: Callable[[DetectedGame], str | None],
                 can_remember: Callable[[DetectedGame], bool] | None = None):
        """``can_remember`` vetoes "remember" for a game (an explicit "do not
        change" must not be turned into an association); default: always."""
        self._resolve = resolve_target
        self._can_remember = can_remember or (lambda game: True)
        self._clear()

    def _clear(self) -> None:
        self._game: DetectedGame | None = None
        self._expected: str | None = None   # preset we set, or found, for this game
        self._reassess = False

    @property
    def active_game(self) -> DetectedGame | None:
        return self._game

    def reset(self) -> None:
        """Feature disabled: forget everything."""
        self._clear()

    def reassess(self) -> None:
        """The active game's association changed: re-resolve on the next tick."""
        if self._game is not None:
            self._reassess = True

    def apply_failed(self, preset: str) -> None:
        """An "apply" did not go through: the preset still active is not a
        manual choice and must not be remembered for the game."""
        if self._expected == preset:
            self._expected = None

    def _evaluate(self, game: DetectedGame, current: str | None) -> Action | None:
        target = self._resolve(game)
        if target is not None and target != current:
            self._expected = target
            return Action("apply", target, game)
        self._expected = target if target is not None else current
        return None

    def tick(self, game: DetectedGame | None, current_preset: str | None,
             now: float) -> Action | None:
        if game is None:
            # Session over, at once: whatever the user does while the game is
            # away is not about the game, and a game that comes back simply
            # resolves its target again (already their last choice).
            self._clear()
            return None

        if self._game is None or game.key != self._game.key:
            self._game = game
            self._reassess = False
            return self._evaluate(game, current_preset)

        self._game = game
        if self._reassess:
            self._reassess = False
            return self._evaluate(game, current_preset)
        if (current_preset is not None and self._expected is not None
                and current_preset != self._expected):
            self._expected = current_preset      # the user changed it by hand
            if self._can_remember(game):
                return Action("remember", current_preset, game)
        elif self._expected is None:
            self._expected = current_preset
        return None
