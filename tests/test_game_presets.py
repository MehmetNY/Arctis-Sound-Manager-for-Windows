"""Automatic Sonar preset per game: identity, config and controller."""

import json

import pytest

from arctis_sound_manager import game_presets as gp
from arctis_sound_manager.game_presets import (
    Action, AutoPresetController, DetectedGame, identify_game, match_preset,
    normalize_title,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "CONFIG_FILE", tmp_path / "cfg" / "game_presets.json")
    monkeypatch.setattr(gp, "STEAM_ROOTS", (tmp_path / "steam",))
    monkeypatch.setattr(gp, "HEROIC_ROOTS", (tmp_path / "heroic",))


# -- titles ------------------------------------------------------------------

def test_normalize_title():
    assert normalize_title("The Witcher® 3: Wild Hunt™") == "the witcher 3 wild hunt"
    assert normalize_title("  Pokémon   Légendes - Arceus ") == "pokemon legendes arceus"
    assert normalize_title("Baldur's Gate") == normalize_title("baldur s gate")
    assert normalize_title("") == ""


def test_match_preset_exact_only():
    presets = ["Flat", "Elden Ring", "Doom Eternal"]
    assert match_preset("ELDEN RING™", presets) == "Elden Ring"
    assert match_preset("Elden", presets) is None
    assert match_preset("Doom", presets) is None
    assert match_preset("", presets) is None


# -- identity ----------------------------------------------------------------

def _steam_tree(tmp_path):
    steam = tmp_path / "steam"
    (steam / "steamapps").mkdir(parents=True)
    other = tmp_path / "lib2"
    (other / "steamapps").mkdir(parents=True)
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n'
        '\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t\t"apps" { "1245620" "1" }\n\t}\n}\n'
        % (steam, other))
    (other / "steamapps" / "appmanifest_1245620.acf").write_text(
        '"AppState"\n{\n\t"appid"\t"1245620"\n\t"name"\t"ELDEN RING"\n}\n')


def test_parse_vdf():
    d = gp.parse_vdf('"a" { "b" "c d" "e" { "f" "g\\\\h" } } "z" "1"')
    assert d == {"a": {"b": "c d", "e": {"f": "g\\h"}}, "z": "1"}


def test_steam_title_from_second_library(tmp_path):
    _steam_tree(tmp_path)
    g = identify_game("eldenring.exe", 42, {"SteamAppId": "1245620"})
    assert g == DetectedGame("steam:1245620", "ELDEN RING", 42)


@pytest.mark.parametrize("var", ["SteamGameId", "STEAM_COMPAT_APP_ID"])
def test_steam_alternate_env(var, tmp_path):
    _steam_tree(tmp_path)
    assert identify_game("x", 1, {var: "1245620"}).key == "steam:1245620"


def test_steam_unknown_manifest_falls_back_to_name():
    g = identify_game("Hades", 1, {"SteamAppId": "1145360"})
    assert (g.key, g.title) == ("steam:1145360", "Hades")


def test_steam_zero_appid_ignored():
    assert identify_game("foo", 1, {"SteamAppId": "0"}).key == "exe:foo"


def test_lutris_and_exe():
    assert identify_game("wine", 1, {"GAME_NAME": "Diablo II"}) == \
        DetectedGame("lutris:Diablo II", "Diablo II", 1)
    assert identify_game("Celeste", None, {}) == DetectedGame("exe:celeste", "Celeste", None)
    assert identify_game("Celeste", None).key == "exe:celeste"


def test_heroic_title_from_store_cache(tmp_path):
    cache = tmp_path / "heroic" / "store_cache"
    cache.mkdir(parents=True)
    (cache / "legendary_library.json").write_text(
        json.dumps({"library": [{"app_name": "Sugar", "title": "Fortnite"},
                                {"app_name": "abc", "title": "Rocket League"}]}))
    (cache / "gog_library.json").write_text(
        json.dumps({"games": [{"app_name": "123", "title": "Witcher"}]}))
    assert identify_game("x", 1, {"HEROIC_APP_NAME": "abc"}) == \
        DetectedGame("heroic:abc", "Rocket League", 1)
    assert identify_game("x", 1, {"HEROIC_APP_NAME": "123"}).title == "Witcher"
    assert identify_game("x", 1, {"HEROIC_APP_NAME": "nope"}).title == "x"


def test_heroic_corrupt_cache_falls_back(tmp_path):
    cache = tmp_path / "heroic" / "store_cache"
    cache.mkdir(parents=True)
    (cache / "legendary_library.json").write_text('{"library": [1, 2]}')
    (cache / "gog_library.json").write_text("not json")
    assert identify_game("fallback", 1, {"HEROIC_APP_NAME": "abc"}).title == "fallback"


# -- config ------------------------------------------------------------------

def test_config_defaults_and_corrupt(tmp_path):
    assert gp.load_config() == {"enabled": False, "games": {}}
    assert not gp.is_enabled()
    gp.config_path().parent.mkdir(parents=True)
    gp.config_path().write_text("{broken")
    assert gp.load_config() == {"enabled": False, "games": {}}


def test_enabled_roundtrip():
    gp.set_enabled(True)
    assert gp.is_enabled()
    gp.set_enabled(False)
    assert not gp.is_enabled()
    assert not list(gp.config_path().parent.glob("*.tmp"))


def test_association_semantics():
    assert gp.get_association("steam:1") == (False, None)
    gp.set_association("steam:1", "Game", "Footsteps")
    assert gp.get_association("steam:1") == (True, "Footsteps")
    gp.set_association("steam:1", "Game", None)
    assert gp.get_association("steam:1") == (True, None)
    gp.set_association("steam:1", "Game", None, automatic=True)
    assert gp.get_association("steam:1") == (False, None)


def test_target_preset():
    g = DetectedGame("steam:1", "Elden Ring", None)
    assert gp.target_preset(g, ["Elden Ring"]) == "Elden Ring"
    gp.set_association("steam:1", "Elden Ring", "Footsteps")
    assert gp.target_preset(g, ["Elden Ring"]) == "Footsteps"
    gp.set_association("steam:1", "Elden Ring", None)
    assert gp.target_preset(g, ["Elden Ring"]) is None


# -- controller --------------------------------------------------------------

A = DetectedGame("steam:1", "A", 1)
B = DetectedGame("steam:2", "B", 2)
TARGETS = {"steam:1": "PA", "steam:2": "PB"}


def make(targets=None):
    t = dict(TARGETS if targets is None else targets)
    return AutoPresetController(lambda g: t.get(g.key)), t


def test_apply_on_start():
    c, _ = make()
    assert c.tick(A, "Flat", 0) == Action("apply", "PA", A)
    assert c.active_game == A
    assert c.tick(A, "PA", 1) is None


def test_noop_when_already_right():
    c, _ = make()
    assert c.tick(A, "PA", 0) is None
    assert c.tick(None, "PA", 1) is None
    assert c.tick(None, "PA", 100) is None


def test_no_target_no_action():
    c, _ = make({})
    assert c.tick(A, "Flat", 0) is None
    assert c.tick(None, "Flat", 99) is None


def test_no_action_when_game_closes():
    c, _ = make()
    c.tick(A, "Flat", 0)
    assert c.tick(None, "PA", 10) is None
    assert c.active_game is None
    assert c.tick(None, "PA", 200) is None


def test_change_after_close_not_attributed_to_game():
    """Apply, manual change, close, change again, relaunch (live-test bug)."""
    c, t = make()
    assert c.tick(A, "Flat", 0) == Action("apply", "PA", A)
    assert c.tick(A, "Forza Horizon 6", 1) == Action("remember", "Forza Horizon 6", A)
    t["steam:1"] = "Forza Horizon 6"           # what set_association now resolves to
    assert c.tick(None, "Forza Horizon 6", 2) is None
    assert c.tick(None, "Palworld", 3) is None   # changed while the game is closed
    assert c.tick(A, "Palworld", 4) == Action("apply", "Forza Horizon 6", A)
    assert c.tick(A, "Forza Horizon 6", 5) is None


def test_flapping_is_harmless():
    c, _ = make()
    c.tick(A, "Flat", 0)
    c.tick(None, "PA", 1)
    assert c.tick(A, "PA", 2) is None           # target already current


def test_new_session_applies_again():
    c, _ = make()
    c.tick(A, "Flat", 0)
    c.tick(None, "PA", 1)
    assert c.tick(A, "Other", 100) == Action("apply", "PA", A)


def test_direct_switch_applies_new_target():
    c, _ = make()
    assert c.tick(A, "Flat", 0) == Action("apply", "PA", A)
    assert c.tick(B, "PA", 1) == Action("apply", "PB", B)


def test_switch_to_game_without_target_does_nothing():
    c, _ = make({"steam:1": "PA"})
    c.tick(A, "Flat", 0)
    assert c.tick(B, "PA", 1) is None
    assert c.active_game == B


def test_reassess_to_do_not_change_does_nothing():
    c, t = make()
    c.tick(A, "Flat", 0)
    del t["steam:1"]
    c.reassess()
    assert c.tick(A, "PA", 1) is None
    assert c.tick(A, "PA", 2) is None


def test_reassess_applies_new_target_and_clears_override():
    c, t = make()
    c.tick(A, "Flat", 0)
    assert c.tick(A, "Mine", 1).kind == "remember"
    t["steam:1"] = "PX"
    c.reassess()
    assert c.tick(A, "Mine", 2) == Action("apply", "PX", A)
    assert c.tick(A, "PX", 3) is None


def test_failed_apply_is_not_remembered():
    """The apply failed, the old preset is still active: not a manual choice."""
    c, _ = make()
    assert c.tick(A, "Flat", 0) == Action("apply", "PA", A)
    c.apply_failed("PA")
    assert c.tick(A, "Flat", 1) is None
    assert c.tick(A, "Mine", 2) == Action("remember", "Mine", A)


def test_stale_failure_ignored():
    c, _ = make()
    c.tick(A, "Flat", 0)
    c.apply_failed("Other")
    assert c.tick(A, "Flat", 1) == Action("remember", "Flat", A)


def test_reset_forgets():
    c, _ = make()
    c.tick(A, "Flat", 0)
    c.reset()
    assert c.active_game is None
    assert c.tick(None, "PA", 100) is None


# -- launch-time detection (audio-server clients) ----------------------------

class _Client:
    def __init__(self, name, pid):
        self.name = name
        self.proplist = {"application.name": name, "application.process.id": str(pid)}


class _FakePulse:
    clients: list = []
    closed = 0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        type(self).closed += 1

    def client_list(self):
        return self.clients


def _fake_clients(monkeypatch, clients, cmdlines, environs):
    import pulsectl
    from arctis_sound_manager import clip_capture as cc
    _FakePulse.clients, _FakePulse.closed = clients, 0
    monkeypatch.setattr(pulsectl, "Pulse", _FakePulse)
    monkeypatch.setattr(cc, "detect_game_process", lambda strict=True: None)
    monkeypatch.setattr(cc, "_process_cmdline", lambda pid: cmdlines.get(pid, ""))
    monkeypatch.setattr(cc, "_process_environ", lambda pid: "")
    monkeypatch.setattr(cc, "process_environ_dict", lambda pid: environs.get(pid, {}))


def test_client_fallback_finds_silent_game(monkeypatch):
    _fake_clients(
        monkeypatch,
        [_Client("Steam", 1), _Client("EOSOverlayRenderer", 2),
         _Client("Steam Voice Settings", 3), _Client("Rocket League", 4)],
        {1: "/steam/steam", 2: "wine eosoverlayrenderer", 3: "/steam/x",
         4: "z:/steamapps/common/rocketleague.exe"},
        {4: {"SteamAppId": "252950"}})
    g = gp.detect_current_game()
    assert (g.key, g.title, g.pid) == ("steam:252950", "Rocket League", 4)
    assert _FakePulse.closed == 1


def test_client_fallback_launcher_env_without_runtime_hint(monkeypatch):
    _fake_clients(monkeypatch, [_Client("Celeste", 9)], {}, {9: {"GAME_NAME": "Celeste"}})
    assert gp.detect_current_game().key == "lutris:Celeste"


def test_client_fallback_ignores_plain_apps(monkeypatch):
    _fake_clients(monkeypatch, [_Client("Spotify", 5), _Client("EOSOverlayRenderer", 6)],
                  {6: "wine proton"}, {5: {"SteamAppId": "0"}})
    assert gp.detect_current_game() is None


# -- stream on the Game channel without a process id of its own --------------

class _Stream:
    def __init__(self, name, sink, client, pid=None):
        self.sink, self.client, self.corked = sink, client, False
        self.proplist = {"application.name": name}
        if pid is not None:
            self.proplist["application.process.id"] = str(pid)


class _Sink:
    def __init__(self, index, name):
        self.index, self.name, self.proplist = index, name, {"node.name": name}


class _StreamPulse(_FakePulse):
    streams: list = []
    sinks: list = []

    def sink_input_list(self):
        return self.streams

    def sink_list(self):
        return self.sinks

    def client_info(self, index):
        return next(c for c in self.clients if c.index == index)


def test_game_stream_without_pid_takes_it_from_its_client(monkeypatch):
    """Black Myth: Wukong's streams carry no process id, its client does. Read
    from the stream alone, the game on the Game channel had no SteamAppId and
    was known only as "b1", which no preset is named after (#328)."""
    import pulsectl
    from arctis_sound_manager import clip_capture as cc

    client = _Client("b1", 24576)
    client.index = 7
    _StreamPulse.clients = [client]
    _StreamPulse.sinks = [_Sink(3, "Arctis_Game")]
    _StreamPulse.streams = [_Stream("b1", sink=3, client=7)]
    monkeypatch.setattr(pulsectl, "Pulse", _StreamPulse)
    monkeypatch.setattr(cc, "_is_audible", lambda si: True)
    monkeypatch.setattr(cc, "process_environ_dict",
                        lambda pid: {"SteamAppId": "2358720"} if pid == 24576 else {})
    monkeypatch.setattr(gp, "_detect_game_client", lambda: None)

    assert cc.detect_game_process(strict=True) == ("b1", 24576)
    assert gp.detect_current_game().key == "steam:2358720"
