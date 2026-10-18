# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""#325: WirePlumber < 0.5.18 with a hide-parent rule hides ASM's nodes from
every PipeWire tool ASM starts; the daemon exempts its tools with
wireplumber.daemon, through PIPEWIRE_CONFIG_NAME."""
from pathlib import Path

import pytest

from arctis_sound_manager import pw_utils

_STEAMOS_RULE = """
node.software-dsp.rules = [
  { matches = [ { node.name = "~alsa_output.*" } ]
    actions = { create-filter = { filter-path = "/x.conf" hide-parent = true } } }
]
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake host root with WirePlumber's library and config folders."""
    root = tmp_path / "host"
    (root / "usr/lib").mkdir(parents=True)
    (root / "usr/share/wireplumber/wireplumber.conf.d").mkdir(parents=True)
    monkeypatch.setattr(pw_utils, "_host_roots", lambda: [str(root)])
    monkeypatch.setattr(pw_utils.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(pw_utils, "_ASM_CLIENT_CONF", tmp_path / "home/asm/client-asm.conf")
    # set-then-delete so monkeypatch restores the original state afterwards:
    # the code under test writes os.environ directly.
    monkeypatch.setenv("PIPEWIRE_CONFIG_NAME", "unset-by-fixture")
    monkeypatch.delenv("PIPEWIRE_CONFIG_NAME")
    return root


def _wireplumber(root: Path, micro: int, rule: bool = True) -> None:
    (root / f"usr/lib/libwireplumber-0.5.so.0.5{micro:02d}.0").touch()
    if rule:
        (root / "usr/share/wireplumber/wireplumber.conf.d/alsa-dsp.conf").write_text(_STEAMOS_RULE)


def test_version_read_from_the_library_file_name(host):
    _wireplumber(host, 14)
    assert pw_utils.wireplumber_version() == (0, 5, 14)


def test_steamos_wireplumber_gets_the_exemption(host):
    _wireplumber(host, 14)
    assert pw_utils.exempt_from_software_dsp_hiding() is True
    conf = Path(pw_utils.os.environ["PIPEWIRE_CONFIG_NAME"])
    assert conf == pw_utils._ASM_CLIENT_CONF
    assert "wireplumber.daemon = true" in conf.read_text()
    assert "libpipewire-module-protocol-native" in conf.read_text()


def test_fixed_wireplumber_is_left_alone(host):
    _wireplumber(host, 18)
    assert pw_utils.exempt_from_software_dsp_hiding() is False
    assert "PIPEWIRE_CONFIG_NAME" not in pw_utils.os.environ


def test_no_hide_parent_rule_no_exemption(host):
    _wireplumber(host, 14, rule=False)
    assert pw_utils.exempt_from_software_dsp_hiding() is False
    assert "PIPEWIRE_CONFIG_NAME" not in pw_utils.os.environ


def test_rule_in_the_users_own_config_counts(host):
    _wireplumber(host, 14, rule=False)
    user = pw_utils.Path.home() / ".config/wireplumber/wireplumber.conf.d"
    user.mkdir(parents=True)
    (user / "dsp.conf").write_text(_STEAMOS_RULE)
    assert pw_utils.exempt_from_software_dsp_hiding() is True


def test_a_config_the_user_chose_is_kept(host, monkeypatch):
    _wireplumber(host, 14)
    monkeypatch.setenv("PIPEWIRE_CONFIG_NAME", "/my/client.conf")
    assert pw_utils.exempt_from_software_dsp_hiding() is False
    assert pw_utils.os.environ["PIPEWIRE_CONFIG_NAME"] == "/my/client.conf"


def test_unknown_wireplumber_no_exemption(host):
    assert pw_utils.wireplumber_version() is None
    assert pw_utils.exempt_from_software_dsp_hiding() is False
