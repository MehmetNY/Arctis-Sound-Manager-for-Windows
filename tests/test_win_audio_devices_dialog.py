"""The Windows "Audio devices" window, with fake devices and a fake engine."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                                            # noqa: E402

from arctis_sound_manager.gui import win_audio_devices as wa                            # noqa: E402
from arctis_sound_manager.win.audio.backend import DeviceInfo                           # noqa: E402
from arctis_sound_manager.win.audio.engine_config import EngineConfig, load_config, save_config   # noqa: E402
from arctis_sound_manager.win.audio.tap import tap_devices                              # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def dev(index, name, ins=0, outs=0, api="Windows WASAPI", default_out=False):
    return DeviceInfo(index, name, api, ins, outs, 48000.0, default_output=default_out)


CABLE = [dev(1, "CABLE Output (VB-Audio Virtual Cable)", ins=2), dev(2, "CABLE Input (VB-Audio Virtual Cable)", outs=2)]
HEADSET = [dev(9, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2),
           dev(10, "Headset Microphone (Arctis Nova Pro Wireless)", ins=1)]
OTHERS = [dev(11, "Speakers (Realtek(R) Audio)", outs=2, default_out=True),
          dev(12, "Speakers (Realtek(R) Audio)", outs=2, api="MME")]                    # the same device, another host API
TAPS = tap_devices()
DEVICES = [*CABLE, *HEADSET, *OTHERS, *TAPS]
GAME_TAP = {"game": "ASM Tap game", "chat": "ASM Tap chat", "media": "ASM Tap media", "aux": "ASM Tap aux"}


class FakeBridge:
    def __init__(self, status=None):
        self.value = status

    def status(self, max_age=None):
        return self.value


class FakeClient:
    instances: list = []

    def __init__(self, config_dir=None, timeout=1.0):
        self.config_dir = config_dir
        self.reconfigured = 0
        FakeClient.instances.append(self)
        self.problems = []
        self.up = True

    def available(self):
        return self.up

    def reconfigure(self):
        self.reconfigured += 1
        return {"ok": True, "problems": self.problems}


def make(tmp_path, qapp, devices=DEVICES, probe=None, **kw):
    kw.setdefault("ensure_engine", lambda cfg, wait=0.0: True)
    kw.setdefault("client_factory", FakeClient)
    kw.setdefault("bridge", FakeBridge())
    return wa.AudioDevicesDialog(config_dir=tmp_path, probe=probe or (lambda: list(devices)), synchronous=True, **kw)


def current(dialog, key):
    return dialog._combos[key].currentData()


def own_config(**over):
    base = dict(inputs=dict(GAME_TAP), outputs={"headset": "Headset Earphone (Arctis Nova Pro Wireless)"})
    base.update(over)
    return EngineConfig(**base)


# ── what the drop-downs offer ────────────────────────────────────────────────

def test_candidates_use_wasapi_devices_the_likeliest_first_and_never_the_taps_own_devices():
    cand = wa.candidates(DEVICES)
    assert all(d.wasapi for lists in cand.values() for d in lists)
    assert not [d for lists in cand.values() for d in lists if d.name.startswith("ASM Tap")]
    assert cand["headset"][0].name.startswith(("Headset Earphone", "Speakers"))
    assert all("CABLE" not in d.name for d in cand["headset"][:2])
    assert [d.name for d in cand["cable_out"][:1]] == ["CABLE Input (VB-Audio Virtual Cable)"]
    assert [d.name for d in cand["mic"]] == ["Headset Microphone (Arctis Nova Pro Wireless)", "CABLE Output (VB-Audio Virtual Cable)"]


def test_without_wasapi_every_device_is_offered():
    mme_only = [dev(1, "Speakers", outs=2, api="MME")]
    assert wa.candidates(mme_only)["headset"][0].name == "Speakers"


def test_a_cable_is_found_only_by_its_playback_end():
    assert wa.has_cable(DEVICES) and not wa.has_cable([*HEADSET, *OTHERS, *TAPS])
    assert not wa.has_cable([CABLE[0]])


def test_combo_items_keep_a_configured_device_that_is_gone():
    options = [CABLE[0], HEADSET[1]]
    items = wa.combo_items(options, "Old Cable Output", optional=True, lang="en")
    assert items[0] == ("(none)", "") and items[-1] == ("Old Cable Output (not found)", "Old Cable Output")
    assert wa.pick_index(items, "Old Cable Output") == len(items) - 1
    # a piece of a name picks the device that contains it
    items = wa.combo_items(options, "headset micro", optional=False, lang="en")
    assert items[wa.pick_index(items, "headset micro")][1].startswith("Headset Microphone")
    assert wa.pick_index(items, "nothing") == 0


def test_the_same_device_listed_twice_appears_once():
    items = wa.combo_items(OTHERS, "", optional=False)
    assert [v for _l, v in items] == ["Speakers (Realtek(R) Audio)"]


# ── the window ───────────────────────────────────────────────────────────────

def test_a_first_run_preselects_asms_own_choice(tmp_path, qapp):
    d = make(tmp_path, qapp)
    assert current(d, "out:headset").startswith("Headset Earphone")
    assert current(d, "mic:in").startswith("Headset Microphone") and current(d, "mic:out").startswith("CABLE Input")
    assert d._latency.currentData() == "balanced" and d._limiter.isChecked()
    assert d._save_btn.isEnabled() and d.message() == ""


def test_there_is_no_channel_by_channel_cable_form(tmp_path, qapp):
    """The programs' sound is taken by the engine itself: nothing to assign per channel."""
    d = make(tmp_path, qapp)
    assert set(d._combos) == {"out:headset", "mic:in", "mic:out", "out:stream"}


def test_an_existing_configuration_is_shown_not_replaced(tmp_path, qapp):
    save_config(own_config(latency="safe", outputs={"headset": "Speakers (Realtek"}, master_limiter=False, block=960), tmp_path)
    d = make(tmp_path, qapp)
    assert current(d, "out:headset").startswith("Speakers") and current(d, "mic:in") == ""
    assert d._latency.currentData() == "safe" and not d._limiter.isChecked()


def test_the_stream_row_only_shows_when_it_matters(tmp_path, qapp):
    d = make(tmp_path, qapp)
    label, combo = d._rows["out:stream"]
    assert label.isHidden() and combo.isHidden()                                 # one cable: nothing to spare
    two = [*DEVICES, dev(13, "CABLE-B Output (VB-Audio Cable B)", ins=2), dev(14, "CABLE-B Input (VB-Audio Cable B)", outs=2)]
    d = make(tmp_path, qapp, devices=two)
    assert not d._rows["out:stream"][1].isHidden()
    save_config(own_config(outputs={"headset": "Headset Earphone", "stream": "Old Cable Input"}), tmp_path)
    d = make(tmp_path, qapp)
    assert not d._rows["out:stream"][1].isHidden()
    assert current(d, "out:stream") == "Old Cable Input" and "(not found)" in d._combos["out:stream"].currentText()


def test_no_cable_says_what_is_missing_and_switches_the_microphone_off(tmp_path, qapp):
    d = make(tmp_path, qapp, devices=[*HEADSET, *OTHERS, *TAPS])
    assert "VB-CABLE" in d.message()
    assert not d._combos["mic:out"].isEnabled() and not d._combos["mic:in"].isEnabled() and d._mic_hint.isHidden()
    d.save()
    cfg = load_config(tmp_path)
    assert cfg.mic_input == "" and cfg.mic_output == "" and cfg.outputs["headset"].startswith("Headset Earphone")
    assert cfg.inputs == GAME_TAP and not cfg.problems()                       # the engine still runs without the mic


def test_the_vb_cable_donation_note_is_in_the_window(tmp_path, qapp):
    d = make(tmp_path, qapp)
    from PySide6.QtWidgets import QLabel
    notes = [x.text() for x in d.findChildren(QLabel)]
    assert any("VB-CABLE" in x and "vb-audio.com" in x and "donationware" in x for x in notes)


def test_a_failed_scan_is_reported(tmp_path, qapp):
    def boom():
        raise RuntimeError("PortAudio is gone")
    d = make(tmp_path, qapp, probe=boom)
    assert "PortAudio is gone" in d.message() and d._save_btn.isEnabled()


def test_save_writes_the_file_and_has_the_engine_rebuild(tmp_path, qapp):
    ensured = []
    d = make(tmp_path, qapp, ensure_engine=lambda cfg, wait=0.0: ensured.append((cfg, wait)) or True)
    d._latency.setCurrentIndex(d._latency.findData("low"))
    d._limiter.setChecked(False)
    changed = []
    d.sig_changed.connect(lambda: changed.append(1))
    d.save()
    cfg = load_config(tmp_path)
    assert cfg.latency == "low" and cfg.master_limiter is False
    assert cfg.inputs == GAME_TAP and cfg.outputs["headset"].startswith("Headset Earphone")
    assert cfg.mic_input.startswith("Headset Microphone") and cfg.mic_output.startswith("CABLE Input")
    assert not cfg.problems() and changed == [1] and cfg.auto is False
    assert ensured == [(tmp_path, 10.0)]
    assert FakeClient.instances[-1].reconfigured == 1
    assert d.message().startswith("Saved")


def test_save_keeps_the_settings_the_form_has_no_control_for(tmp_path, qapp):
    save_config(own_config(block=960, mic_block=480, device_latency="high",
                           outputs={"headset": "Headset Earphone", "stream": "CABLE Input"},
                           routing={"game": "stream", "chat": "headset", "media": "headset", "aux": "headset"}), tmp_path)
    d = make(tmp_path, qapp)
    d.save()
    cfg = load_config(tmp_path)
    assert (cfg.block, cfg.mic_block, cfg.device_latency) == (960, 480, "high")
    assert cfg.inputs == GAME_TAP and cfg.routing["game"] == "stream"
    assert cfg.outputs["stream"].startswith("CABLE Input")                       # the name is made exact


def test_unselecting_the_stream_cable_removes_it_from_the_saved_file(tmp_path, qapp):
    save_config(own_config(outputs={"headset": "Headset Earphone", "stream": "CABLE Input"}), tmp_path)
    d = make(tmp_path, qapp)
    assert current(d, "out:stream").startswith("CABLE Input")
    d._combos["out:stream"].setCurrentIndex(0)
    d.save()
    assert "stream" not in load_config(tmp_path).outputs


def test_a_microphone_without_a_cable_to_send_it_to_is_dropped(tmp_path, qapp):
    d = make(tmp_path, qapp)
    d._combos["mic:out"].setCurrentIndex(0)
    cfg = d.selected_config()
    assert cfg.mic_output == "" and cfg.mic_input == ""


def test_save_needs_a_headset(tmp_path, qapp):
    d = make(tmp_path, qapp, devices=[HEADSET[1], *TAPS])                      # a microphone but nothing to play to
    d.save()
    assert "Choose a headset" in d.message()
    assert not (tmp_path / "win_audio_engine.json").exists()


def test_a_chosen_configuration_is_no_longer_asms_to_change(tmp_path, qapp):
    save_config(own_config(auto=True), tmp_path)
    d = make(tmp_path, qapp)
    d.save()
    assert load_config(tmp_path).auto is False


def test_choose_automatically_gives_the_choice_back_to_asm(tmp_path, qapp):
    save_config(own_config(latency="safe", outputs={"headset": "Speakers (Realtek"}, master_limiter=False, block=960), tmp_path)
    d = make(tmp_path, qapp)
    d.automatic()
    cfg = load_config(tmp_path)
    assert cfg.auto is True
    assert cfg.outputs["headset"].startswith("Headset Earphone") and cfg.mic_input.startswith("Headset Microphone")
    assert cfg.inputs == GAME_TAP
    assert (cfg.latency, cfg.master_limiter, cfg.block) == ("safe", False, 960)  # only the devices are ASM's to choose
    assert current(d, "out:headset").startswith("Headset Earphone")             # the form shows what was chosen
    assert "keep following" in d.message()
    assert FakeClient.instances[-1].reconfigured >= 1


def test_choose_automatically_does_nothing_before_the_scan_has_delivered(tmp_path, qapp):
    d = make(tmp_path, qapp, probe=lambda: [])
    d.automatic()
    assert not (tmp_path / "win_audio_engine.json").exists()


def test_problems_the_engine_reports_are_shown(tmp_path, qapp):
    class Picky(FakeClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.problems = ["the same device is assigned to two channels"]
    d = make(tmp_path, qapp, client_factory=Picky)
    d.save()
    assert "two channels" in d.message()


def test_an_engine_that_will_not_start_is_reported(tmp_path, qapp):
    d = make(tmp_path, qapp, ensure_engine=lambda cfg, wait=0.0: False)
    d.save()
    assert "could not be started" in d.message() and d._save_btn.isEnabled()
    assert load_config(tmp_path).inputs["game"]                                   # the file was still saved


def test_the_state_line_follows_the_engine(tmp_path, qapp):
    status = {"running": True, "profile": "balanced", "latency_ms": 69.9,
              "endpoints": {"in:game": {"state": "running", "device": "ASM Tap game"}}}
    bridge = FakeBridge(status)
    d = make(tmp_path, qapp, bridge=bridge)
    assert "running" in d.state_text() and "Game: running (ASM Tap game)" in d.state_text()
    bridge.value = None
    d.refresh_state()
    assert "not running" in d.state_text() or "No headset" in d.state_text()


def test_the_texts_are_turkish_in_a_turkish_ui(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(wa, "_lang", lambda: "tr")
    d = make(tmp_path, qapp)
    assert d.windowTitle() == "Ses aygıtları" and d._save_btn.text() == "Kaydet ve uygula"
    d.save()
    assert d.message().startswith("Kaydedildi")


def test_threaded_scan_delivers_its_result(tmp_path, qapp):
    """The real (asynchronous) path: the worker thread's result reaches the form."""
    import time
    d = wa.AudioDevicesDialog(config_dir=tmp_path, probe=lambda: list(DEVICES), ensure_engine=lambda *a, **k: True,
                              client_factory=FakeClient, bridge=FakeBridge(), synchronous=False, poll_ms=60000)
    try:
        deadline = time.time() + 5
        while not d._save_btn.isEnabled() and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        qapp.processEvents()
        assert d._save_btn.isEnabled() and current(d, "out:headset").startswith("Headset Earphone")
    finally:
        d.close()
