# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Windows refuses to rename over - or open - a file that another program has open at that instant
(the GUI reading the settings, an antivirus or cloud-sync client scanning the new file).

Before: the write raised PermissionError, so the setting silently did not stick; and a *read* that hit
the same moment treated the file as corrupt, renamed it to ``.broken`` and fell back to defaults -
every setting gone ("turn off while the session is locked or idle" back to its default ON, the screen
timeout back to 30).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from arctis_sound_manager import settings as S


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(S, "_SHARING_RETRY_DELAY_S", 0)


def _fail_first(monkeypatch, owner, name, times, exc=PermissionError):
    real = getattr(owner, name)
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= times:
            raise exc(13, "The process cannot access the file because it is being used by another process")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, flaky)
    return calls


def test_a_write_waits_out_a_file_that_is_briefly_in_use(monkeypatch, tmp_path):
    target = tmp_path / "general_settings.yaml"
    calls = _fail_first(monkeypatch, Path, "replace", times=7)

    S._atomic_yaml_dump({"oled_off_when_away": False, "oled_screen_timeout": 90}, target)

    assert calls["n"] == 8
    assert YAML(typ="safe").load(target) == {"oled_off_when_away": False, "oled_screen_timeout": 90}
    assert [p.name for p in tmp_path.iterdir()] == ["general_settings.yaml"], "no temp file may be left behind"


def test_a_write_that_never_gets_through_still_raises_and_cleans_up(monkeypatch, tmp_path):
    target = tmp_path / "general_settings.yaml"
    target.write_text("oled_screen_timeout: 30\n")
    _fail_first(monkeypatch, Path, "replace", times=10_000)

    with pytest.raises(PermissionError):
        S._atomic_yaml_dump({"oled_screen_timeout": 90}, target)

    assert target.read_text() == "oled_screen_timeout: 30\n", "the old file must be left as it was"
    assert [p.name for p in tmp_path.iterdir()] == ["general_settings.yaml"]


def test_a_read_waits_out_a_file_that_is_briefly_in_use(monkeypatch, tmp_path, caplog):
    target = tmp_path / "general_settings.yaml"
    target.write_text("oled_off_when_away: false\noled_screen_timeout: 90\n")
    calls = _fail_first(monkeypatch, YAML, "load", times=5)

    import logging
    data = S._load_yaml_with_backup(target, logger=logging.getLogger("t"))

    assert data == {"oled_off_when_away": False, "oled_screen_timeout": 90}
    assert calls["n"] >= 6   # five refusals, then success (ruamel calls load() again once it has opened the Path)
    assert not (tmp_path / "general_settings.yaml.broken").exists()


def test_a_file_that_stays_locked_is_never_renamed_to_broken(monkeypatch, tmp_path, caplog):
    target = tmp_path / "general_settings.yaml"
    target.write_text("oled_off_when_away: false\noled_screen_timeout: 90\n")
    _fail_first(monkeypatch, YAML, "load", times=10_000)

    import logging
    assert S._load_yaml_with_backup(target, logger=logging.getLogger("t")) is None

    assert target.read_text() == "oled_off_when_away: false\noled_screen_timeout: 90\n", "the user's settings must survive"
    assert not (tmp_path / "general_settings.yaml.broken").exists()


def test_a_genuinely_corrupt_file_is_still_set_aside(tmp_path):
    """The old behaviour for real corruption is untouched."""
    import logging
    target = tmp_path / "general_settings.yaml"
    target.write_text("oled_off_when_away: [unclosed\n")

    assert S._load_yaml_with_backup(target, logger=logging.getLogger("t")) is None
    assert (tmp_path / "general_settings.yaml.broken").exists()
