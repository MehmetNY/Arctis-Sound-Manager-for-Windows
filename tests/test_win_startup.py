"""Start with Windows: the Run-key value, Task Manager's own switch, and the quiet launcher."""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

from arctis_sound_manager.win import startup


class FakeRegistry:
    """Just enough of winreg: HKCU only, string and binary values, keys created on demand."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE = 1, 2
    REG_SZ, REG_BINARY = 1, 3

    def __init__(self):
        self.keys: dict[str, dict[str, tuple[object, int]]] = {}

    class _Key:
        def __init__(self, store):
            self.store = store

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, path, reserved=0, access=0):
        if path not in self.keys:
            raise FileNotFoundError(path)
        return self._Key(self.keys[path])

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        return self._Key(self.keys.setdefault(path, {}))

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        return key.store[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        key.store[name] = (value, kind)

    def DeleteValue(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        del key.store[name]

    def value(self, path, name):
        return self.keys.get(path, {}).get(name, (None,))[0]


@pytest.fixture
def reg(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


@pytest.fixture
def not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)


def test_enable_registers_a_short_command_that_names_the_launcher_script(reg, not_frozen, tmp_path):
    command = startup.enable(executable="C:\\Python313\\pythonw.exe", script_dir=tmp_path)
    assert reg.value(startup.RUN_KEY, startup.VALUE_NAME) == command
    assert command == f'"C:\\Python313\\pythonw.exe" "{tmp_path / startup.SCRIPT_NAME}"'
    assert len(command) < 260 or len(str(tmp_path)) > 150        # only a very long temp path may break the limit
    script = (tmp_path / startup.SCRIPT_NAME).read_text(encoding="utf-8")
    assert "from arctis_sound_manager.scripts.winstart import main" in script
    assert "raise SystemExit(main())" in script


def test_the_launcher_script_adds_the_source_tree_to_sys_path_when_the_package_is_not_installed(reg, not_frozen, tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "_installed_package", lambda parent: False)
    startup.write_launcher_script(tmp_path)
    text = (tmp_path / startup.SCRIPT_NAME).read_text(encoding="utf-8")
    assert "sys.path.insert(0, " in text
    assert str(startup._package_parent()) in text


def test_the_launcher_script_is_plain_when_the_package_is_installed(reg, not_frozen, tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "_installed_package", lambda parent: True)
    startup.write_launcher_script(tmp_path)
    assert "sys.path" not in (tmp_path / startup.SCRIPT_NAME).read_text(encoding="utf-8").replace("import sys\n", "")


def test_the_launcher_script_is_rewritten_only_when_it_changes(reg, not_frozen, tmp_path):
    first = startup.write_launcher_script(tmp_path)
    stamp = first.stat().st_mtime_ns
    startup.write_launcher_script(tmp_path)
    assert first.stat().st_mtime_ns == stamp


def test_a_frozen_program_registers_itself_with_the_background_flag(reg, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = startup.enable(executable="C:\\Program Files\\ASM\\ASM.exe", script_dir=tmp_path)
    assert command == '"C:\\Program Files\\ASM\\ASM.exe" --background'
    assert not (tmp_path / startup.SCRIPT_NAME).exists()          # nothing to write: the exe is the launcher


def test_enabled_after_enable_and_disabled_after_disable(reg, not_frozen, tmp_path):
    assert startup.is_enabled() is False
    startup.enable(executable="py.exe", script_dir=tmp_path)
    assert startup.is_enabled() is True
    startup.disable()
    assert startup.is_enabled() is False
    assert reg.value(startup.RUN_KEY, startup.VALUE_NAME) is None


def test_disable_with_nothing_registered_is_fine(reg):
    startup.disable()
    assert startup.is_enabled() is False


@pytest.mark.parametrize("first_byte, on", [(0x02, True), (0x06, True), (0x03, False), (0x07, False)])
def test_task_manager_switch_is_honoured(reg, not_frozen, tmp_path, first_byte, on):
    startup.enable(executable="py.exe", script_dir=tmp_path)
    reg.keys.setdefault(startup.APPROVED_KEY, {})[startup.VALUE_NAME] = (bytes([first_byte]) + bytes(11), reg.REG_BINARY)
    assert startup.is_enabled() is on


def test_enable_switches_task_manager_back_on_when_the_user_had_switched_it_off(reg, not_frozen, tmp_path):
    reg.keys.setdefault(startup.APPROVED_KEY, {})[startup.VALUE_NAME] = (bytes([0x03]) + bytes(11), reg.REG_BINARY)
    startup.enable(executable="py.exe", script_dir=tmp_path)
    assert startup.is_enabled() is True
    assert reg.value(startup.APPROVED_KEY, startup.VALUE_NAME)[0] == 0x02


def test_enable_does_not_invent_a_task_manager_entry(reg, not_frozen, tmp_path):
    startup.enable(executable="py.exe", script_dir=tmp_path)
    assert startup.APPROVED_KEY not in reg.keys


def test_disable_removes_the_task_manager_entry_too(reg, not_frozen, tmp_path):
    startup.enable(executable="py.exe", script_dir=tmp_path)
    reg.keys.setdefault(startup.APPROVED_KEY, {})[startup.VALUE_NAME] = (bytes([0x02]) + bytes(11), reg.REG_BINARY)
    startup.disable()
    assert reg.value(startup.APPROVED_KEY, startup.VALUE_NAME) is None


def test_an_empty_value_does_not_count_as_registered(reg):
    reg.keys[startup.RUN_KEY] = {startup.VALUE_NAME: ("   ", reg.REG_SZ)}
    assert startup.registered_command() is None
    assert startup.is_enabled() is False


def test_windowless_python_swaps_python_exe_for_pythonw_when_it_exists(tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    assert startup.windowless_python(str(tmp_path / "python.exe")) == str(tmp_path / "pythonw.exe")


def test_windowless_python_keeps_the_interpreter_when_there_is_no_pythonw(tmp_path):
    (tmp_path / "python.exe").write_text("")
    assert startup.windowless_python(str(tmp_path / "python.exe")) == str(tmp_path / "python.exe")
    assert startup.windowless_python(str(tmp_path / "other.exe")) == str(tmp_path / "other.exe")


# ----------------------------------------------------------------------------------------------- the autostart module
def test_the_windows_backend_is_the_only_one_on_windows(monkeypatch, reg, not_frozen, tmp_path):
    from arctis_sound_manager import autostart
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    monkeypatch.setattr(startup, "data_dir", lambda: tmp_path)
    assert [b.name for b in autostart._all_backends()] == ["windows"]
    assert autostart.active_backend_name() == "windows"
    assert autostart.autostart_enabled() is False
    autostart.set_autostart(True)
    assert autostart.autostart_enabled() is True
    autostart.set_autostart(False)
    assert autostart.autostart_enabled() is False


def test_linux_backends_are_unchanged(monkeypatch):
    from arctis_sound_manager import autostart
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    assert [b.name for b in autostart._all_backends()] == ["systemd", "dinit", "xdg", "hyprland", "sway"]
    assert autostart.WindowsBackend().is_available() is False
