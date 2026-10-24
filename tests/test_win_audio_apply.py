"""'Apply' on Windows: the pages leave their state where the audio engine reads it, then tell the engine."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from arctis_sound_manager.eq_types import EqBand                                      # noqa: E402
from arctis_sound_manager.gui import sonar_page as sp                                  # noqa: E402
from arctis_sound_manager.win.audio import apply as win_apply                          # noqa: E402
from arctis_sound_manager.win.audio.params import load_eq_state, load_micro_state, mic_spec   # noqa: E402

BANDS = [EqBand(freq=100, gain=2.0, q=0.7, type="peakingEQ", enabled=True),
         EqBand(freq=8000, gain=-3.0, q=1.0, type="highshelf", enabled=True)]


class FakeBridge:
    def __init__(self):
        self.reloads = 0

    def reload(self):
        self.reloads += 1


@pytest.fixture
def bridge(monkeypatch):
    fake = FakeBridge()
    monkeypatch.setattr(win_apply, "shared_bridge", lambda: fake)
    return fake


@pytest.fixture
def home(monkeypatch, tmp_path, bridge):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sp, "IS_WINDOWS", True)
    monkeypatch.setattr(sp, "_load_boost", lambda: {"enabled": True, "db": 3.0})
    monkeypatch.setattr(sp, "_load_smart_volume", lambda: {"enabled": True, "level": 0.4, "loudness": "balanced"})
    monkeypatch.setattr(sp, "_load_micro_proc", lambda: {
        "noiseCanceling": {"enabled": True, "value": 0.7}, "noiseGate": {"enabled": True, "value": -45}})
    return tmp_path / ".config" / "arctis_manager"


def test_the_saved_channel_state_is_what_the_engine_reads_back(home):
    assert win_apply.save_channel_state("game", BANDS, 1.5, -2.0, 0.5, boost_db=3.0,
                                        smart_volume={"enabled": True, "level": 0.4, "loudness": "balanced"})
    state = load_eq_state("game", home)
    assert [(b.freq, b.gain, b.type) for b in state["bands"]] == [(100, 2.0, "peakingEQ"), (8000, -3.0, "highshelf")]
    assert (state["basses_db"], state["voix_db"], state["aigus_db"], state["boost_db"]) == (1.5, -2.0, 0.5, 3.0)
    assert state["smart_volume"]["enabled"] is True


def test_the_microphone_state_carries_the_noise_processing(home):
    proc = {"noiseCanceling": {"enabled": True, "value": 0.7}, "noiseGate": {"enabled": True, "value": -45}}
    assert win_apply.save_channel_state("micro", BANDS, 0.0, 2.0, 0.0, boost_db=0.0, micro_processing=proc)
    spec = mic_spec(load_micro_state(home))
    assert spec.noise_canceling and spec.noise_canceling_strength == pytest.approx(0.7) and spec.gate_threshold_db == -45
    data = json.loads((home / "sonar_micro_state.json").read_text())
    assert data["noise_canceling"] == {"enabled": True, "value": 0.7} and data["noise_reduction"]["noiseGate"]["value"] == -45


def test_a_channel_the_engine_does_not_process_is_not_written(home):
    assert not win_apply.save_channel_state("output", BANDS, 0, 0, 0)
    assert not (home / "sonar_eq_state_output.json").exists()


def test_reload_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("no bridge")
    monkeypatch.setattr(win_apply, "shared_bridge", boom)
    win_apply.reload_engine()


def test_the_apply_worker_saves_the_state_and_reloads_the_engine(home, bridge):
    worker = sp._ApplyWorker("chat", BANDS, 1.0, 2.0, 3.0)
    done = []
    worker.done.connect(done.append)
    worker.run()
    assert done == [True] and bridge.reloads == 1
    state = load_eq_state("chat", home)
    assert state["boost_db"] == 3.0 and state["smart_volume"]["level"] == 0.4         # Boost / Smart Volume ride along
    assert (state["basses_db"], state["voix_db"], state["aigus_db"]) == (1.0, 2.0, 3.0)


def test_the_apply_worker_handles_the_microphone(home, bridge):
    worker = sp._ApplyWorker("micro", BANDS, 0.0, 1.0, 0.0)
    done = []
    worker.done.connect(done.append)
    worker.run()
    assert done == [True] and (home / "sonar_micro_state.json").exists() and bridge.reloads == 1
    assert json.loads((home / "sonar_micro_state.json").read_text())["noise_canceling"]["value"] == 0.7


def test_the_output_eq_is_accepted_without_touching_the_engine(home, bridge):
    worker = sp._ApplyWorker("output", BANDS, 0.0, 0.0, 0.0)
    done = []
    worker.done.connect(done.append)
    worker.run()
    assert done == [True] and bridge.reloads == 0


def test_a_failure_saving_the_state_is_reported_not_raised(home, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(win_apply, "save_channel_state", boom)
    worker = sp._ApplyWorker("game", BANDS, 0.0, 0.0, 0.0)
    done = []
    worker.done.connect(done.append)
    worker.run()
    assert done == [False]


def test_apply_all_saves_every_channel_and_reloads_once(home, bridge, monkeypatch):
    monkeypatch.setattr(sp, "_channel_bands_and_macro",
                        lambda channel: (BANDS if channel != "chat" else [], {"basses": 1.0, "voix": 0.0, "aigus": 0.0}))
    worker = sp._ApplyAllWorker()
    done = []
    worker.done.connect(done.append)
    worker.run()
    assert done == [True] and bridge.reloads == 1
    for channel in ("game", "media", "chat"):
        assert (home / f"sonar_eq_state_{channel}.json").exists()
    assert (home / "sonar_micro_state.json").exists()
    assert load_eq_state("chat", home)["bands"] == [] and len(load_eq_state("game", home)["bands"]) == 2


def test_apply_all_reports_a_failure(home, monkeypatch):
    monkeypatch.setattr(sp, "_channel_bands_and_macro", lambda c: (_ for _ in ()).throw(RuntimeError("x")))
    worker = sp._ApplyAllWorker()
    done = []
    worker.done.connect(done.append)
    worker.run()
    assert done == [False]


def test_channel_bands_and_macro_reads_the_active_preset(home, monkeypatch):
    preset = home / "p.json"
    monkeypatch.setattr(sp, "_list_presets", lambda channel: {"mine": preset, "other": preset})
    monkeypatch.setattr(sp, "_active_preset_name", lambda channel: "mine")
    monkeypatch.setattr(sp, "_parse_preset", lambda path: BANDS if path == preset else [])
    monkeypatch.setattr(sp, "_load_macro", lambda channel: {"basses": 2.0})
    assert sp._channel_bands_and_macro("game") == (BANDS, {"basses": 2.0})
    monkeypatch.setattr(sp, "_list_presets", lambda channel: (_ for _ in ()).throw(OSError("gone")))
    assert sp._channel_bands_and_macro("game")[0] == []                                # unreadable presets: flat EQ
