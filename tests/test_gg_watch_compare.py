# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""
test_gg_watch_compare.py — only a spec whose content moved is "changed".

GG 122 opened #341 over 15 shared specs that were byte-identical to GG 121's:
the previous state had simply never hashed them. A file with no previous hash
has nothing to differ from.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).parent.parent / "scripts" / "reverse-engineering" / "gg-watch.py"
_spec = importlib.util.spec_from_file_location("gg_watch", _PATH)
gg_watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gg_watch)


def test_untracked_spec_is_not_reported_as_changed():
    state = {"edevice_files": ["a.edevice"], "spec_hashes": {"a.device": "1"}}
    new, changed = gg_watch.compare_to_state(
        ["a.edevice"], {"a.device": "1", "headset.device": "x"}, state)
    assert (new, changed) == ([], [])


def test_a_moved_hash_and_a_new_device_file_are_reported():
    state = {"edevice_files": ["a.edevice"], "spec_hashes": {"a.device": "1"}}
    new, changed = gg_watch.compare_to_state(
        ["a.edevice", "nova_9.edevice"], {"a.device": "2"}, state)
    assert (new, changed) == (["nova_9.edevice"], ["a.device"])
