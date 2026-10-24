# Copyright (C) 2022 Giacomo Furlan (elegos) — original work
# Copyright (C) 2026 loteran — modifications
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys
from pathlib import Path

IS_WINDOWS: bool = sys.platform == 'win32'


def _config_home() -> Path:
    """Per-user config root: ``~/.config`` on Windows, XDG_CONFIG_HOME elsewhere.

    About sixty places in the code base spell ``Path.home() / ".config" / "arctis_manager"``
    out by hand (EQ presets, routing overrides, language, channel volumes ...). On Windows that
    resolves to ``C:\\Users\\<you>\\.config\\arctis_manager``, so the central settings live there
    too: one folder, instead of half of the files in %APPDATA% and half in ``.config``.

    The name ``_XDG_CONFIG`` below is kept as is because other modules import it.
    """
    if IS_WINDOWS:
        return Path.home() / '.config'
    return Path(os.environ.get('XDG_CONFIG_HOME') or (Path.home() / '.config'))


def _migrate_legacy_windows_config(new_root: Path | None = None) -> bool:
    """Copy an earlier port's ``%APPDATA%\\arctis_manager`` into the current config folder.

    The first Windows builds kept the central settings (device YAMLs, general settings) in
    %APPDATA%. Copy, never move: if anything goes wrong the old folder is still intact. Done
    once: it only happens while the new folder does not exist yet.
    """
    try:
        appdata = os.environ.get('APPDATA')
        if not IS_WINDOWS or not appdata:
            return False
        old = Path(appdata) / 'arctis_manager'
        new = (new_root or _config_home()) / 'arctis_manager'
        if not old.is_dir() or new.exists() or old == new:
            return False
        import shutil
        shutil.copytree(old, new)
        return True
    except Exception:  # noqa: BLE001 - a failed migration must never stop the app from starting
        return False


_XDG_CONFIG = _config_home()
_migrate_legacy_windows_config()


def _invoking_user_config_home() -> Path | None:
    """XDG config home of the user who invoked us, when running as root via sudo/pkexec.

    `sudo asm-cli udev write-rules` (what the docs and the CLI hints tell users
    to run) resolves Path.home() to /root, so device YAML overrides living in
    the real user's ~/.config/arctis_manager/devices were invisible and never
    made it into the generated rules (#146). Returns None when not elevated, or
    when the invoking user cannot be resolved. Read-only use only: never write
    into the returned folder as root, that would leave root-owned files behind.
    """
    # No POSIX uid on Windows: there is no sudo/pkexec indirection to undo.
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        return None

    uid = os.environ.get('PKEXEC_UID') or os.environ.get('SUDO_UID')
    if uid is None:
        return None

    try:
        import pwd

        home = Path(pwd.getpwuid(int(uid)).pw_dir)
    except (KeyError, ValueError, OSError):
        return None

    if home == Path.home():
        return None

    return home / '.config'

# /DBus
DBUS_BUS_NAME = 'name.giacomofurlan.ArctisManager.Next'
DBUS_OBJECT_BASE_PATH = '/name/giacomofurlan/ArctisManager/Next'

DBUS_SETTINGS_INTERFACE_NAME = f'{DBUS_BUS_NAME}.Settings'
DBUS_SETTINGS_OBJECT_PATH = f'{DBUS_OBJECT_BASE_PATH}/Settings'

DBUS_STATUS_INTERFACE_NAME = f'{DBUS_BUS_NAME}.Status'
DBUS_STATUS_OBJECT_PATH = f'{DBUS_OBJECT_BASE_PATH}/Status'

DBUS_CONFIG_INTERFACE_NAME = f'{DBUS_BUS_NAME}.Config'
DBUS_CONFIG_OBJECT_PATH = f'{DBUS_OBJECT_BASE_PATH}/Config'
# ./DBus

# Systemd
SYSTEMD_SERVICE_NAME = 'arctis-manager.service'
HOME_SYSTEMD_SERVICE_FOLDER = _XDG_CONFIG / 'systemd' / 'user'
HOME_DINIT_SERVICE_FOLDER = _XDG_CONFIG / 'dinit.d'
# ./Systemd

# Two different things used to share one name here, and the name fitted the
# wrong one. The headset's ChatMix dial reports its two halves as "media mix"
# and "chat mix" — the firmware's vocabulary — and its "media" half is the
# GAME channel. So PULSE_MEDIA_NODE_NAME meant Arctis_Game, and every other
# caller that reached for "the media sink" silently got Game instead.
#
# That mattered in one place: the sink ASM makes the system default on connect.
# Everything that follows the default — a browser, a music player, system
# sounds, anything ASM does not route by name — landed on Game, which files it
# all as game audio and unbalances the very ChatMix dial above.
PULSE_GAME_NODE_NAME = 'Arctis_Game'
"""The sink the ChatMix dial's "media" half drives. Firmware vocabulary."""

PULSE_MEDIA_NODE_NAME = 'Arctis_Media'
"""The Media channel — where audio that follows the system default belongs."""

PULSE_CHAT_NODE_NAME = 'Arctis_Chat'

PULSE_AUX_NODE_NAME = 'Arctis_Aux'

STEELSERIES_VENDOR_ID = 0x1038

SETTINGS_FOLDER = _XDG_CONFIG / 'arctis_manager' / 'settings'

HOME_LANG_FOLDER = _XDG_CONFIG / 'arctis_manager' / 'lang'

HOME_CONFIG_FOLDER = _XDG_CONFIG / 'arctis_manager' / 'devices'
SRC_CONFIG_FOLDER = Path(__file__).parent / 'devices'

# Only set when running elevated (sudo / pkexec): the device folder of the user
# who invoked us, read so their YAML overrides still count. See
# _invoking_user_config_home().
_INVOKING_USER_XDG_CONFIG = _invoking_user_config_home()
INVOKING_USER_CONFIG_FOLDER: Path | None = (
    _INVOKING_USER_XDG_CONFIG / 'arctis_manager' / 'devices'
    if _INVOKING_USER_XDG_CONFIG is not None else None
)

DEVICES_CONFIG_FOLDER: list[Path] = [HOME_CONFIG_FOLDER, SRC_CONFIG_FOLDER]
if INVOKING_USER_CONFIG_FOLDER is not None:
    DEVICES_CONFIG_FOLDER.insert(0, INVOKING_USER_CONFIG_FOLDER)

UDEV_RULES_PATHS = [
    # /etc has highest priority — local admin / asm-cli writes here.
    '/etc/udev/rules.d/91-steelseries-arctis.rules',
    # Distro-installed rules (Arch, Fedora, modern Debian/Ubuntu with usrmerge).
    '/usr/lib/udev/rules.d/91-steelseries-arctis.rules',
    # Pre-usrmerge Debian / Ubuntu locations, kept for backwards compat.
    '/lib/udev/rules.d/91-steelseries-arctis.rules',
    # Runtime overlay (transient, used by some package managers and NixOS).
    '/run/udev/rules.d/91-steelseries-arctis.rules',
]

# Companion file carrying only TAG+="uaccess" — it must sort before
# 73-seat-late.rules to be acted on (systemd >= 258, #297). Always installed
# next to whichever UDEV_RULES_PATHS entry the main file goes to.
UDEV_UACCESS_RULES_NAME = '70-steelseries-arctis-uaccess.rules'
UDEV_UACCESS_RULES_PATHS = [
    str(Path(path).with_name(UDEV_UACCESS_RULES_NAME))
    for path in UDEV_RULES_PATHS
]
