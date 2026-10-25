"""On Windows nothing may touch the PipeWire graph: no service restarts, no "safe mode" that moves files around,
no HRIR copies into ~/.local/share/pipewire. The daemon calls these entry points whenever a headset connects or a
setting changes, so each one has to answer a neutral default and leave the disk alone."""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

NEUTRAL = {
    "mark_link_permission_fallback": ((("game",)), False),
    "clear_safe_mode_and_restore": ((), None),
    "maybe_recover_from_safe_mode": ((), False),
    "_enter_filter_chain_safe_mode": ((), None),
    "_restart_filter_chain": ((), None),
    "ensure_filter_chain_healthy": ((), False),
    "ensure_hrir_materialized": (("atmos",), False),
    "apply_hrir_choice": (("atmos",), None),
    "check_and_fix_stale_configs": ((), (False, False)),
    "sync_stream_mix_conf": ((True,), False),
    "regenerate_hesuvi_if_changed": ((), False),
    "apply_spatial_audio_change": ((), False),
    "ensure_sonar_eq_configs": ((), False),
    "ensure_spatial_eq_links": ((), {}),
    "ensure_physical_output_links": ((), {}),
    "release_physical_output_links": ((), 0),
    "ensure_micro_capture_link": ((), False),
    "ensure_stream_mix_links": ((), False),
}


@pytest.fixture()
def windows_pipewire(monkeypatch):
    """sonar_to_pipewire as it is imported on Windows (the guards are decided when the module loads)."""
    from arctis_sound_manager import constants, platform_guard, sonar_to_pipewire
    monkeypatch.setattr(constants, "IS_WINDOWS", True)
    importlib.reload(platform_guard)
    module = importlib.reload(sonar_to_pipewire)
    try:
        yield module
    finally:
        monkeypatch.undo()
        importlib.reload(platform_guard)
        importlib.reload(sonar_to_pipewire)


def test_every_pipewire_entry_point_is_a_no_op_on_windows(windows_pipewire, monkeypatch):
    started = []

    def record(*args, **kwargs):
        started.append(args)
        raise OSError("no such program on Windows")
    for fn in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, fn, record)
    for name, (args, expected) in NEUTRAL.items():
        function = getattr(windows_pipewire, name)
        assert hasattr(function, "__wrapped__"), f"{name} is not guarded"        # the stand-in, not the original
        result = function(*args)
        assert result == expected and type(result) is type(expected), name
    assert started == []


def test_nothing_is_written_to_the_pipewire_folders(windows_pipewire):
    home = Path.home()
    folders = [home / ".config" / "pipewire", home / ".local" / "share" / "pipewire"]
    before = [sorted(p.rglob("*")) if p.exists() else None for p in folders]
    for name, (args, _expected) in NEUTRAL.items():
        getattr(windows_pipewire, name)(*args)
    assert [sorted(p.rglob("*")) if p.exists() else None for p in folders] == before


def test_on_linux_the_functions_are_the_original_ones(monkeypatch):
    """The Linux code path stays what upstream ships: no wrapper around any of them."""
    from arctis_sound_manager import constants, sonar_to_pipewire
    assert constants.IS_WINDOWS is False
    for name in NEUTRAL:
        fn = getattr(sonar_to_pipewire, name)
        assert not hasattr(fn, "__wrapped__"), name
