"""The Windows "Audio devices" window, with fake devices and a fake engine."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                                            # noqa: E402

from arctis_sound_manager.gui import win_audio_devices as wa                            # noqa: E402
from arctis_sound_manager.win.audio.backend import DeviceInfo                           # noqa: E402
from arctis_sound_manager.win.audio.engine_config import EngineConfig, load_config, save_config   # noqa: E402
from arctis_sound_manager.win.audio.strings import t                                    # noqa: E402
from arctis_sound_manager.win.audio.tap import tap_devices                              # noqa: E402
from arctis_sound_manager.win.fx import state as fxstate                                # noqa: E402


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
MONITOR = dev(13, "Monitor (NVIDIA High Definition Audio)", outs=2)
DAC = dev(14, "Line out (USB DAC)", outs=2)
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

    def reconfigure(self, force=False):
        self.reconfigured += 1
        return {"ok": True, "problems": self.problems}


class FxAsks:
    """Stands in for the scheduled tasks that set the microphone effect up: remembers what was asked."""

    def __init__(self, answer=True):
        self.asked, self.answer = [], answer

    def __call__(self, action):
        self.asked.append(action)
        return self.answer


def fx_state(code, detail=""):
    return lambda status=None: fxstate.FxState(code, detail)


def make(tmp_path, qapp, devices=DEVICES, probe=None, **kw):
    kw.setdefault("fx_describe", fx_state(fxstate.READY))
    kw.setdefault("fx_request", FxAsks())
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
    assert set(cand) == {"headset", "stream"}
    assert all(d.wasapi for lists in cand.values() for d in lists)
    assert not [d for lists in cand.values() for d in lists if d.name.startswith("ASM Tap")]
    assert cand["headset"][0].name.startswith("Headset Earphone")
    assert not [d for lists in cand.values() for d in lists if "CABLE" in d.name]       # somebody's virtual cable is not offered


def test_without_wasapi_every_device_is_offered():
    mme_only = [dev(1, "Speakers", outs=2, api="MME")]
    assert wa.candidates(mme_only)["headset"][0].name == "Speakers"


def test_virtual_devices_of_other_software_are_never_offered_or_chosen():
    virtual = [dev(20, "Voicemeeter Input (VB-Audio Voicemeeter VAIO)", outs=2),
               dev(21, "SteelSeries Sonar - Gaming (SteelSeries Sonar Virtual Audio Device)", outs=2)]
    cand = wa.candidates([*DEVICES, *virtual])
    assert [d.name for d in cand["headset"] if wa.is_virtual(d.name)] == []
    assert not hasattr(wa, "has_cable")                                                  # nothing in the window asks for a cable


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
    assert d._latency.currentData() == "balanced" and d._limiter.isChecked()
    assert d._save_btn.isEnabled() and d.message() == ""


def test_there_is_no_channel_by_channel_cable_form_and_no_microphone_device(tmp_path, qapp):
    """The programs' sound is taken by the engine itself, the microphone is the headset's own: nothing to assign."""
    d = make(tmp_path, qapp)
    assert set(d._combos) == {"out:headset", "out:stream"}


def test_an_existing_configuration_is_shown_not_replaced(tmp_path, qapp):
    save_config(own_config(latency="safe", outputs={"headset": "Speakers (Realtek"}, master_limiter=False, block=960), tmp_path)
    d = make(tmp_path, qapp)
    assert current(d, "out:headset").startswith("Speakers")
    assert d._latency.currentData() == "safe" and not d._limiter.isChecked()


def test_the_stream_row_only_shows_when_it_matters(tmp_path, qapp):
    d = make(tmp_path, qapp)
    label, combo = d._rows["out:stream"]
    assert label.isHidden() and combo.isHidden()                                 # one other output: nothing to spare
    d = make(tmp_path, qapp, devices=[*DEVICES, MONITOR, DAC])
    assert not d._rows["out:stream"][1].isHidden()                               # a second real output device to send the stream mix to
    save_config(own_config(outputs={"headset": "Headset Earphone", "stream": "Old Interface"}), tmp_path)
    d = make(tmp_path, qapp)
    assert not d._rows["out:stream"][1].isHidden()
    assert current(d, "out:stream") == "Old Interface" and "(not found)" in d._combos["out:stream"].currentText()


def test_a_virtual_cable_on_the_pc_is_invisible_in_the_window(tmp_path, qapp):
    """Nothing virtual is installed, needed, mentioned or chosen by ASM."""
    from PySide6.QtWidgets import QLabel
    d = make(tmp_path, qapp)
    assert d.message() == ""
    texts = [x.text() for x in d.findChildren(QLabel)] + [c.itemText(i) for c in d._combos.values() for i in range(c.count())]
    assert not [x for x in texts if "cable" in x.lower() or "vb-audio" in x.lower()]
    d.save()
    assert "CABLE" not in (tmp_path / "win_audio_engine.json").read_text()


def test_the_window_works_without_any_virtual_device_at_all(tmp_path, qapp):
    d = make(tmp_path, qapp, devices=[*HEADSET, *OTHERS, *TAPS])
    assert d.message() == "" and d._save_btn.isEnabled()
    d.save()
    cfg = load_config(tmp_path)
    assert cfg.outputs["headset"].startswith("Headset Earphone") and cfg.mic_fx
    assert cfg.inputs == GAME_TAP and not cfg.problems()


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
    assert cfg.inputs == GAME_TAP and cfg.outputs["headset"].startswith("Headset Earphone") and cfg.mic_fx
    assert not cfg.problems() and changed == [1] and cfg.auto is False
    assert ensured == [(tmp_path, 10.0)]
    assert FakeClient.instances[-1].reconfigured == 1
    assert d.message().startswith("Saved")


def test_save_keeps_the_settings_the_form_has_no_control_for(tmp_path, qapp):
    save_config(own_config(block=960, mic_block=480, device_latency="high",
                           outputs={"headset": "Headset Earphone", "stream": "Monitor"},
                           routing={"game": "stream", "chat": "headset", "media": "headset", "aux": "headset"}), tmp_path)
    d = make(tmp_path, qapp, devices=[*DEVICES, MONITOR, DAC])
    d.save()
    cfg = load_config(tmp_path)
    assert (cfg.block, cfg.mic_block, cfg.device_latency) == (960, 480, "high")
    assert cfg.inputs == GAME_TAP and cfg.routing["game"] == "stream"
    assert cfg.outputs["stream"].startswith("Monitor (NVIDIA")                   # the name is made exact


def test_unselecting_the_stream_output_removes_it_from_the_saved_file(tmp_path, qapp):
    save_config(own_config(outputs={"headset": "Headset Earphone", "stream": "Monitor"}), tmp_path)
    d = make(tmp_path, qapp, devices=[*DEVICES, MONITOR, DAC])
    assert current(d, "out:stream").startswith("Monitor")
    d._combos["out:stream"].setCurrentIndex(0)
    d.save()
    assert "stream" not in load_config(tmp_path).outputs


def test_the_microphone_setting_survives_a_save(tmp_path, qapp):
    save_config(own_config(mic_fx=False), tmp_path)
    d = make(tmp_path, qapp)
    d.save()
    assert load_config(tmp_path).mic_fx is False                                 # the form has no control for it: kept as it was


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
    assert cfg.outputs["headset"].startswith("Headset Earphone") and cfg.mic_fx
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


# ── the microphone effect ────────────────────────────────────────────────────

def test_the_window_says_how_the_microphone_effect_is_doing(tmp_path, qapp):
    for code, key in ((fxstate.RUNNING, "fx_running"), (fxstate.READY, "fx_ready"), (fxstate.NEEDS_SETUP, "fx_needs_setup"),
                      (fxstate.NO_HEADSET, "fx_no_headset"), (fxstate.MISSING, "fx_missing"), (fxstate.OFF, "fx_off")):
        d = make(tmp_path, qapp, fx_describe=fx_state(code))
        assert d.fx_text() == t(key, "en"), code
    d = make(tmp_path, qapp, fx_describe=fx_state(fxstate.DISABLED, "audiodg.exe crashed 2 times in the effect"))
    assert "audiodg.exe crashed 2 times" in d.fx_text()


def test_the_button_sets_the_effect_up_or_switches_it_off(tmp_path, qapp):
    asks = FxAsks()
    d = make(tmp_path, qapp, fx_describe=fx_state(fxstate.NEEDS_SETUP), fx_request=asks)
    assert not d._fx_btn.isHidden() and d._fx_btn.text() == t("btn_fx_setup", "en")
    d._fx_btn.click()
    assert asks.asked == ["attach"] and d.message() == t("fx_asked", "en")
    d = make(tmp_path, qapp, fx_describe=fx_state(fxstate.READY), fx_request=asks)
    assert d._fx_btn.text() == t("btn_fx_remove", "en")
    d._fx_btn.click()
    assert asks.asked == ["attach", "detach"] and d.message() == t("fx_asked_off", "en")
    d = make(tmp_path, qapp, fx_describe=fx_state(fxstate.DISABLED, "off"), fx_request=asks)
    d._fx_btn.click()
    assert asks.asked[-1] == "attach"                                            # switched off by hand or by the guard: asking again re-enables


def test_there_is_no_button_when_there_is_nothing_to_set_up(tmp_path, qapp):
    for code in (fxstate.OFF, fxstate.NO_HEADSET, fxstate.MISSING):
        assert make(tmp_path, qapp, fx_describe=fx_state(code))._fx_btn.isHidden()


def test_without_the_scheduled_tasks_the_window_says_what_to_do_instead(tmp_path, qapp):
    d = make(tmp_path, qapp, fx_describe=fx_state(fxstate.NEEDS_SETUP), fx_request=FxAsks(answer=False))
    d._fx_btn.click()
    assert d.message() == t("fx_cannot_ask", "en") and "--fx attach" in d.message()


def test_a_failing_status_lookup_never_breaks_the_window(tmp_path, qapp):
    def boom(status=None):
        raise RuntimeError("registry unreadable")
    d = make(tmp_path, qapp, fx_describe=boom)
    assert d.fx_text() == t("fx_off", "en")


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
