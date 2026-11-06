"""Attaching the microphone effect to the headset's microphone, and putting Windows back: the real code against an
in-memory registry (the same code runs on HKEY_LOCAL_MACHINE on Windows; CI proves that part: ci/fx_e2e.py)."""
import subprocess
import sys
import uuid

import pytest

from arctis_sound_manager.win.fx import attach as fx
from arctis_sound_manager.win.fx import cli, state, tasks
from arctis_sound_manager.win.fx.registry import REG_BINARY, REG_DWORD, REG_MULTI_SZ, REG_SZ, MemoryRegistry, Value

ARCTIS = "{aaaaaaaa-0000-0000-0000-000000000001}"
SONAR = "{bbbbbbbb-0000-0000-0000-000000000002}"
SPEAKER_MIC = "{cccccccc-0000-0000-0000-000000000003}"
DRIVER_EFFECT = "{11111111-2222-3333-4444-555555555555}"
FXP = fx.CAPTURE + "\\{}\\FxProperties"


def endpoint(reg, guid, name, state=1):
    base = f"{fx.CAPTURE}\\{guid}"
    reg.add(base, {"DeviceState": Value(state, REG_DWORD)})
    reg.add(base + r"\Properties", {"{a45c254e-df1c-4efd-8020-67d146a850e0},2": Value(name, REG_SZ)})
    reg.add(base + r"\FxProperties")


@pytest.fixture
def reg():
    r = MemoryRegistry()
    endpoint(r, ARCTIS, "Mikrofon (Arctis Nova Pro Wireless)")
    endpoint(r, SONAR, "SteelSeries Sonar Virtual Audio Device (Arctis)")
    endpoint(r, SPEAKER_MIC, "Microphone (Realtek(R) Audio)")
    return r


@pytest.fixture
def dll(tmp_path):
    path = tmp_path / "asm_micfx.dll"
    path.write_bytes(b"MZ")
    return str(path)


class Restarts:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return True


def attach(reg, dll, restart=None, **kw):
    kw.setdefault("marker_dir", kw.pop("marker_dir", None))
    return fx.attach(reg, dll, restart=restart, **kw)


# ── which microphone ─────────────────────────────────────────────────────────

def test_only_the_headsets_own_microphone_is_chosen(reg):
    chosen = [e.guid for e in fx.endpoints(reg) if fx.is_headset_microphone(e)]
    assert chosen == [ARCTIS]                                                  # not Sonar's virtual one, not the laptop's


def test_the_ci_can_name_another_endpoint(reg):
    pick = fx.selector(["realtek"])
    assert [e.guid for e in fx.endpoints(reg) if pick(e)] == [SPEAKER_MIC]
    assert fx.selector(None) is fx.is_headset_microphone


# ── attach ───────────────────────────────────────────────────────────────────

def test_attach_registers_the_effect_and_puts_it_on_the_microphone(reg, dll, tmp_path):
    restarts = Restarts()
    r = attach(reg, dll, restarts, marker_dir=tmp_path)
    assert r.ok and r.changed and r.restarted and r.attached == [ARCTIS] and restarts.count == 1
    assert reg.get(fx.COM_KEY + r"\InprocServer32", "").data == dll
    assert reg.get(fx.COM_KEY + r"\InprocServer32", "ThreadingModel").data == "Both"
    apo = reg.values(fx.APO_KEY)
    assert apo["NumAPOInterfaces"].data == 3 and apo["APOInterface2"].data == fx.APO_INTERFACES[2]
    assert apo["Flags"].data == 0xF and apo["MinInputConnections"].kind == REG_DWORD
    assert reg.get(fx.FXP.format(ARCTIS) if hasattr(fx, "FXP") else FXP.format(ARCTIS), fx.EFX).data == fx.CLSID
    assert reg.get(fx.AUDIO, fx.PROTECT).data == 1
    for other in (SONAR, SPEAKER_MIC):
        assert fx.carries(reg, FXP.format(other)) == []                        # nobody else's microphone is touched


def test_attaching_twice_changes_and_restarts_nothing(reg, dll, tmp_path):
    attach(reg, dll, marker_dir=tmp_path)
    restarts = Restarts()
    r = attach(reg, dll, restarts, marker_dir=tmp_path)
    assert r.ok and not r.changed and not r.restarted and restarts.count == 0 and r.attached == [ARCTIS]


def test_a_microphone_that_appears_later_is_attached_then(reg, dll, tmp_path):
    reg2 = MemoryRegistry()
    r = attach(reg2, dll, marker_dir=tmp_path)
    assert r.ok and r.attached == [] and "no microphone of the headset" in r.notes[0]
    assert fx.audio_protection(reg2) is None                                    # nothing to protect against yet: nothing changed
    endpoint(reg2, ARCTIS, "Mikrofon (Arctis Nova Pro Wireless)")
    assert attach(reg2, dll, marker_dir=tmp_path).attached == [ARCTIS]


def test_system_keys_that_refuse_a_write_are_opened_first(dll, tmp_path):
    locked = MemoryRegistry(locked={f"{fx.CAPTURE}\\{ARCTIS}\\FxProperties"})
    endpoint(locked, ARCTIS, "Mikrofon (Arctis Nova Pro Wireless)")
    locked.locked = {f"{fx.CAPTURE}\\{ARCTIS}\\FxProperties".lower()}
    r = attach(locked, dll, marker_dir=tmp_path)
    assert r.ok and r.attached == [ARCTIS]
    assert locked.opened == [f"{fx.CAPTURE}\\{ARCTIS}\\FxProperties"]


def test_more_than_one_slot_can_carry_the_effect(reg, dll, tmp_path):
    attach(reg, dll, marker_dir=tmp_path, slots=("sfx", "efx"))
    assert fx.carries(reg, FXP.format(ARCTIS)) == ["sfx", "efx"]
    assert fx.carries(reg, FXP.format(ARCTIS), ("efx",)) == ["efx"]
    assert fx.slot_key("efx").endswith(",7") and fx.slot_key("sfx").endswith(",5") and fx.slot_key("mfx").endswith(",6")


def test_a_missing_file_is_refused_on_windows_only(reg, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    r = attach(reg, str(tmp_path / "gone.dll"), marker_dir=tmp_path)
    assert not r.ok and "missing" in r.errors[0] and not reg.exists(fx.COM_KEY)


def test_the_audio_protection_is_switched_off_only_when_there_is_a_microphone_to_attach(reg, dll, tmp_path):
    reg.set(fx.AUDIO, fx.PROTECT, 0, REG_DWORD)
    attach(reg, dll, marker_dir=tmp_path)
    assert fx.audio_protection(reg) == 1 and reg.get(fx.STATE, "ProtectBefore").data == "0"


# ── processing modes ─────────────────────────────────────────────────────────

def mode_blob(*names):
    """What a driver leaves in an endpoint's properties: some header bytes and the ids of the modes it offers."""
    return b"\x41\x00\x00\x00\x01\x00\x00\x00" + b"".join(uuid.UUID(fx.MODES[n]).bytes_le + b"\xb0\x00\x00\x00" for n in names)


def offer(reg, guid, *names):
    reg.add(f"{fx.CAPTURE}\\{guid}\\Properties", {"{a2a3fff4-353f-407c-9d86-1f9dc7d5a606},2": Value(mode_blob(*names), REG_BINARY)})


def mode_names(reg, guid, slot="efx"):
    value = reg.get(FXP.format(guid), fx.mode_key(slot))
    return None if value is None else sorted(n for n, g in fx.MODES.items() if g.upper() in [x.upper() for x in value.data])


def test_the_effect_says_which_modes_it_works_in(reg, dll, tmp_path):
    offer(reg, ARCTIS, "default", "raw", "communications")
    attach(reg, dll, marker_dir=tmp_path)
    value = reg.get(FXP.format(ARCTIS), "{d3993a3f-99c2-4402-b5ec-a92a0367664b},7")
    assert value.kind == REG_MULTI_SZ and mode_names(reg, ARCTIS) == ["communications", "default", "media", "movie"]
    assert reg.get(FXP.format(SONAR), fx.mode_key("efx")) is None              # only the headset's microphone is touched


def test_a_driver_that_offers_only_the_raw_mode_gets_the_raw_mode_too(reg, dll, tmp_path):
    offer(reg, ARCTIS, "raw")
    assert fx.supported_modes(reg, fx.endpoints(reg)[0]) == {"raw"}
    attach(reg, dll, marker_dir=tmp_path)
    assert mode_names(reg, ARCTIS) == ["communications", "default", "media", "movie", "raw"]


def test_the_modes_can_be_named(reg, dll, tmp_path):
    attach(reg, dll, marker_dir=tmp_path, modes=["raw", "bogus", "raw"])
    assert mode_names(reg, ARCTIS) == ["raw"]


def test_each_slot_gets_its_own_mode_list_and_the_old_pair_none(reg, dll, tmp_path):
    attach(reg, dll, marker_dir=tmp_path, slots=("lfx", "sfx", "mfx", "efx"))
    assert [mode_names(reg, ARCTIS, s) is not None for s in ("sfx", "mfx", "efx")] == [True, True, True]
    assert [k for k in reg.values(FXP.format(ARCTIS)) if k.startswith(fx.MODES_FMTID)] == [
        f"{fx.MODES_FMTID},5", f"{fx.MODES_FMTID},6", f"{fx.MODES_FMTID},7"]


def test_detach_puts_the_drivers_own_mode_list_back(reg, dll, tmp_path):
    own = [fx.MODES["default"]]
    reg.set(FXP.format(ARCTIS), fx.mode_key("efx"), own, REG_MULTI_SZ)
    attach(reg, dll, marker_dir=tmp_path)
    assert mode_names(reg, ARCTIS) != ["default"]
    fx.detach(reg, restart=None, marker_dir=tmp_path)
    assert list(reg.get(FXP.format(ARCTIS), fx.mode_key("efx")).data) == own
    attach(reg, dll, marker_dir=tmp_path)
    fx.detach(reg, restart=None, marker_dir=tmp_path)
    assert list(reg.get(FXP.format(ARCTIS), fx.mode_key("efx")).data) == own    # twice in a row: still the driver's


def test_the_mode_list_of_an_endpoint_attached_by_an_older_build_is_added(reg, dll, tmp_path):
    reg.set(FXP.format(ARCTIS), fx.EFX, fx.CLSID, REG_SZ)                       # the effect, but no list
    s = fx.status(reg, dll, marker_dir=tmp_path)
    assert s.attached == [ARCTIS] and s.missing == [ARCTIS] and s.needs_attach
    attach(reg, dll, marker_dir=tmp_path)
    s = fx.status(reg, dll, marker_dir=tmp_path)
    assert not s.missing and fx.has_modes(reg, fx.endpoints(reg)[0])


def test_attaching_again_leaves_the_mode_list_alone(reg, dll, tmp_path):
    attach(reg, dll, marker_dir=tmp_path)
    restarts = Restarts()
    assert not attach(reg, dll, restarts, marker_dir=tmp_path).changed and restarts.count == 0


# ── detach ───────────────────────────────────────────────────────────────────

def test_detach_puts_windows_back_exactly_as_it_was(reg, dll, tmp_path):
    reg.set(FXP.format(ARCTIS), fx.EFX, DRIVER_EFFECT, REG_SZ)                  # the driver had an effect of its own there
    reg.set(fx.AUDIO, fx.PROTECT, 0, REG_DWORD)
    before = {p: dict(reg.values(p)) for p in (FXP.format(ARCTIS), fx.AUDIO)}
    attach(reg, dll, marker_dir=tmp_path)
    restarts = Restarts()
    r = fx.detach(reg, restart=restarts, marker_dir=tmp_path)
    assert r.ok and r.changed and restarts.count == 1
    assert {p: dict(reg.values(p)) for p in (FXP.format(ARCTIS), fx.AUDIO)} == before
    assert not reg.exists(fx.COM_KEY) and not reg.exists(fx.APO_KEY) and not reg.exists(fx.STATE)


def test_detach_removes_the_value_that_was_not_there_before_and_the_protection_setting(reg, dll, tmp_path):
    attach(reg, dll, marker_dir=tmp_path, slots=("sfx", "efx"))
    fx.detach(reg, restart=None, marker_dir=tmp_path)
    assert fx.carries(reg, FXP.format(ARCTIS)) == [] and fx.audio_protection(reg) is None
    assert reg.get(fx.AUDIO, fx.PROTECT) is None


def test_detach_leaves_the_protection_alone_when_it_was_already_off(reg, dll, tmp_path):
    reg.set(fx.AUDIO, fx.PROTECT, 1, REG_DWORD)                                 # somebody else's setting (an equalizer, say)
    attach(reg, dll, marker_dir=tmp_path)
    fx.detach(reg, restart=None, marker_dir=tmp_path)
    assert fx.audio_protection(reg) == 1


def test_detach_with_nothing_attached_changes_nothing(reg, tmp_path):
    restarts = Restarts()
    r = fx.detach(reg, restart=restarts, marker_dir=tmp_path)
    assert r.ok and not r.changed and restarts.count == 0


def test_detach_finds_an_attachment_an_older_build_left_without_a_record(reg, tmp_path):
    reg.set(FXP.format(ARCTIS), fx.EFX, fx.CLSID, REG_SZ)
    r = fx.detach(reg, restart=None, marker_dir=tmp_path)
    assert r.ok and r.changed and fx.attached_guids(reg) == []


def test_a_reason_keeps_the_effect_away_until_somebody_asks_for_it_again(reg, dll, tmp_path):
    attach(reg, dll, marker_dir=tmp_path)
    fx.detach(reg, restart=None, reason="switched off by hand", marker_dir=tmp_path)
    assert fx.disabled_reason(tmp_path) == "switched off by hand"
    r = attach(reg, dll, marker_dir=tmp_path)
    assert not r.ok and "switched off" in r.errors[0] and fx.attached_guids(reg) == []
    assert attach(reg, dll, marker_dir=tmp_path, force=True).attached == [ARCTIS]
    fx.clear_marker(tmp_path)
    assert fx.disabled_reason(tmp_path) is None


# ── looking ──────────────────────────────────────────────────────────────────

def test_status_reads_what_is_in_place_without_changing_anything(reg, dll, tmp_path):
    s = fx.status(reg, dll, marker_dir=tmp_path)
    assert s.dll_present and s.wanted == [ARCTIS] and s.missing == [ARCTIS] and s.needs_attach and not s.ready
    attach(reg, dll, marker_dir=tmp_path)
    s = fx.status(reg, dll, marker_dir=tmp_path)
    assert s.ready and not s.needs_attach and s.attached == [ARCTIS] and s.com_registered and s.protection_off
    endpoint(reg, "{dddddddd-0000-0000-0000-000000000004}", "Mikrofon 2 (Arctis Nova Pro Wireless)")     # a driver update: a new endpoint
    s = fx.status(reg, dll, marker_dir=tmp_path)
    assert s.needs_attach and len(s.missing) == 1 and s.ready


def test_the_registered_path_must_be_the_installed_one(reg, dll, tmp_path):
    attach(reg, dll, marker_dir=tmp_path)
    other = tmp_path / "moved" / "asm_micfx.dll"
    assert not fx.status(reg, str(other), marker_dir=tmp_path).com_registered     # the program moved: registration is stale


# ── the guard ────────────────────────────────────────────────────────────────

def test_crashes_of_the_audio_engine_in_the_effect_take_it_away_for_good(reg, dll, tmp_path):
    attach(reg, dll, marker_dir=tmp_path)
    r = fx.guard(reg, crashes=lambda: 1, restart=None, marker_dir=tmp_path)
    assert fx.attached_guids(reg) == [ARCTIS] and "1 crash" in r.notes[0]
    r = fx.guard(reg, crashes=lambda: 2, restart=None, marker_dir=tmp_path)
    assert fx.attached_guids(reg) == [] and "crashed 2 times" in fx.disabled_reason(tmp_path)


def test_the_crash_count_reads_windows_event_log_for_audiodg_and_the_effect_only():
    xml = ("<Event><Data>audiodg.exe</Data><Data>C:\\Program Files\\ArctisSoundManager\\fx\\asm_micfx.dll</Data></Event>"
           "<Event><Data>audiodg.exe</Data><Data>C:\\Windows\\System32\\other.dll</Data></Event>"
           "<Event><Data>chrome.exe</Data><Data>asm_micfx.dll</Data></Event>"
           "<Event><Data>audiodg.exe</Data><Data>asm_micfx.dll</Data></Event>")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=xml, stderr="")
    assert fx.audiodg_crashes(30, run=run) == 2
    assert seen[0][:3] == ["wevtutil", "qe", "Application"] and "1800000" in seen[0][3]
    assert fx.audiodg_crashes(30, run=lambda *a, **k: (_ for _ in ()).throw(OSError("no wevtutil"))) == 0


# ── the audio service ────────────────────────────────────────────────────────

def test_the_audio_service_is_restarted_and_the_endpoint_builder_only_when_asked():
    ran = []
    ok = fx.restart_audio_service(run=lambda cmd, **kw: ran.append(cmd), sleep=lambda s: None)
    assert ok and ran == [["net", "stop", "audiosrv", "/y"], ["net", "start", "audiosrv"]]
    ran.clear()
    fx.restart_audio_service(run=lambda cmd, **kw: ran.append(cmd), sleep=lambda s: None, builder=True)
    assert ["net", "stop", "AudioEndpointBuilder", "/y"] in ran and ran[-1] == ["net", "start", "audiosrv"]
    assert not fx.restart_audio_service(run=lambda *a, **k: (_ for _ in ()).throw(OSError("denied")), sleep=lambda s: None)


# ── what the window shows ────────────────────────────────────────────────────

def test_the_state_follows_the_registry(reg, dll, tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "program_data_dir", lambda: tmp_path)
    assert state.describe(None, reg, str(tmp_path / "gone.dll")).code == state.MISSING
    assert state.describe(None, reg, dll).code == state.NEEDS_SETUP
    attach(reg, dll, marker_dir=tmp_path)
    assert state.describe(None, reg, dll).code == state.READY
    running = {"microphone": {"state": "processing"}}
    assert state.describe(running, reg, dll).code == state.RUNNING and state.describe(running, reg, dll).active
    fx.detach(reg, restart=None, reason="audiodg.exe crashed 2 times in the effect", marker_dir=tmp_path)
    s = state.describe(None, reg, dll)
    assert s.code == state.DISABLED and "crashed 2 times" in s.detail
    empty = MemoryRegistry()
    fx.clear_marker(tmp_path)
    assert state.describe(None, empty, dll).code == state.NO_HEADSET


def test_the_state_of_a_registry_that_cannot_be_read_is_off_not_an_error(dll):
    class Broken(MemoryRegistry):
        def subkeys(self, path):
            raise OSError("hive unavailable")
    assert state.describe(None, Broken(), dll).code == state.OFF


def test_off_the_platform_nothing_is_attempted():
    assert state.describe(None).code == state.OFF
    assert state.request("attach", run_task=lambda action: False) is False
    assert state.request("detach", run_task=lambda action: action == "detach") is True


# ── keeping it attached without anybody opening a window ─────────────────────

class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_the_daemon_asks_for_the_setup_when_it_is_missing_and_not_more_often_than_every_ten_minutes():
    clock, asked = Clock(), []
    states = {"now": state.FxState(state.NEEDS_SETUP)}
    m = state.Maintainer(lambda: states["now"], asked.append, lambda: 0, clock)
    assert m.tick() == "attach" and asked == ["attach"]
    clock.now = 599
    assert m.tick() is None
    clock.now = 601
    assert m.tick() == "attach"
    states["now"] = state.FxState(state.READY)
    clock.now = 5000
    assert m.tick() is None and asked == ["attach", "attach"]                  # nothing to do once it holds


def test_the_daemon_asks_for_the_guard_when_the_audio_engine_keeps_crashing():
    clock, asked = Clock(), []
    crashes = {"n": 0}
    m = state.Maintainer(lambda: state.FxState(state.READY), asked.append, lambda: crashes["n"], clock)
    assert m.tick() is None
    crashes["n"] = 2
    clock.now = 299
    assert m.tick() is None                                                     # looked a moment ago: not again yet
    clock.now = 301
    assert m.tick() == "guard" and asked == ["guard"]


def test_a_disabled_effect_is_not_asked_for_again_by_the_daemon():
    asked = []
    m = state.Maintainer(lambda: state.FxState(state.DISABLED, "off"), asked.append, lambda: 0, Clock())
    assert m.tick() is None and asked == []                                     # only the person switches it on again


# ── the scheduled tasks ──────────────────────────────────────────────────────

def test_three_tasks_exist_and_a_standard_user_may_only_start_them():
    assert [tasks.task_name(a) for a in tasks.ACTIONS] == [
        "ASM microphone effect - attach", "ASM microphone effect - detach", "ASM microphone effect - guard"]
    script = tasks._script(r"C:\Program Files\Arctis Sound Manager\ArctisSoundManager.exe")
    assert "'--fx ' + $t.a" in script and "ArctisSoundManager.exe" in script
    assert "(A;;GRGX;;;IU)" in script and "(A;;FA;;;SY)" in script           # users: read and execute (= start); no write
    assert "'it''s'" in tasks._script("it's") or "it''s" in tasks._script("it's")      # a quote in the path cannot break out


def test_the_tasks_are_started_and_removed_through_windows_own_tools(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    ran = []

    def run(cmd, **kw):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    assert tasks.start("attach", run=run) and ran[-1] == ["schtasks", "/run", "/tn", "ASM microphone effect - attach"]
    assert not tasks.start("rm -rf", run=run)                                  # only the three known actions
    assert tasks.installed(run=run)
    tasks.remove(run=run)
    assert sum(1 for c in ran if c[:2] == ["schtasks", "/delete"]) == 3
    assert tasks.install(r"C:\x.exe", run=run) is True
    assert not tasks.install(r"C:\x.exe", run=lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="denied"))


def test_without_windows_none_of_it_does_anything():
    assert not tasks.start("attach") and not tasks.installed() and not tasks.install("x")


# ── the command line ─────────────────────────────────────────────────────────

def test_the_command_line_status_attach_and_detach(reg, dll, tmp_path, monkeypatch, capsys):
    import json
    from arctis_sound_manager.win.fx import registry
    monkeypatch.setattr(registry, "system_registry", lambda: reg)
    monkeypatch.setenv("ASM_MICFX_DLL", dll)
    monkeypatch.setattr(fx, "program_data_dir", lambda: tmp_path)
    monkeypatch.setattr(fx, "restart_audio_service", lambda *a, **k: True)
    assert cli.main(["status", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["dll_present"] and info["wanted"] == [ARCTIS] and info["needs_attach"] and not info["ready"]
    assert cli.main(["attach", "--slots", "sfx,efx", "--modes", "raw,default"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["ok"] and done["attached"] == [ARCTIS] and done["restarted"]
    assert fx.carries(reg, FXP.format(ARCTIS)) == ["sfx", "efx"] and mode_names(reg, ARCTIS, "sfx") == ["default", "raw"]
    assert cli.main(["detach", "--reason", "by hand"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] and fx.disabled_reason(tmp_path) == "by hand"
    assert cli.main(["attach"]) == 1 and "switched off" in json.loads(capsys.readouterr().out)["errors"][0]
    assert cli.main(["attach", "--force"]) == 0
    assert cli.main(["nonsense"]) == 2


def test_the_command_line_can_target_another_endpoint_and_ignore_junk_slots(reg, dll, monkeypatch, capsys, tmp_path):
    from arctis_sound_manager.win.fx import registry
    monkeypatch.setattr(registry, "system_registry", lambda: reg)
    monkeypatch.setenv("ASM_MICFX_DLL", dll)
    monkeypatch.setattr(fx, "program_data_dir", lambda: tmp_path)
    monkeypatch.setattr(fx, "restart_audio_service", lambda *a, **k: True)
    assert cli.main(["attach", "--match", "Realtek", "--slots", "bogus"]) == 0
    assert fx.attached_guids(reg) == [SPEAKER_MIC] and fx.carries(reg, FXP.format(SPEAKER_MIC)) == ["efx"]


def test_the_default_file_is_next_to_the_program(monkeypatch, tmp_path):
    monkeypatch.delenv("ASM_MICFX_DLL", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "ArctisSoundManager.exe"))
    assert cli.dll_path() == str(tmp_path / "fx" / "asm_micfx.dll")
