"""The Windows audio-engine set-up assistant, with fake devices and a fake engine."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                                            # noqa: E402

from arctis_sound_manager.gui import win_audio_setup as wa                              # noqa: E402
from arctis_sound_manager.win.audio.backend import DeviceInfo                           # noqa: E402
from arctis_sound_manager.win.audio.engine_config import EngineConfig, load_config, save_config   # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def dev(index, name, ins=0, outs=0, api="Windows WASAPI", default_out=False):
    return DeviceInfo(index, name, api, ins, outs, 48000.0, default_output=default_out)


DEVICES = [
    dev(1, "CABLE-A Output (VB-Audio Cable A)", ins=2), dev(2, "CABLE-A Input (VB-Audio Cable A)", outs=2),
    dev(3, "CABLE-B Output (VB-Audio Cable B)", ins=2), dev(4, "CABLE-B Input (VB-Audio Cable B)", outs=2),
    dev(5, "CABLE-C Output (VB-Audio Cable C)", ins=2), dev(6, "CABLE-C Input (VB-Audio Cable C)", outs=2),
    dev(7, "CABLE-D Output (VB-Audio Cable D)", ins=2), dev(8, "CABLE-D Input (VB-Audio Cable D)", outs=2),
    dev(9, "Headset Earphone (Arctis Nova Pro Wireless)", outs=2),
    dev(10, "Headset Microphone (Arctis Nova Pro Wireless)", ins=1),
    dev(11, "Speakers (Realtek(R) Audio)", outs=2, default_out=True),
    dev(12, "Speakers (Realtek(R) Audio)", outs=2, api="MME"),                         # the same device, another host API
]


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
    kw.setdefault("stop", lambda cfg: True)
    kw.setdefault("selftest", lambda cfg: "selftest output")
    kw.setdefault("client_factory", FakeClient)
    kw.setdefault("bridge", FakeBridge())
    return wa.AudioEngineSetupDialog(config_dir=tmp_path, probe=probe or (lambda: list(devices)), synchronous=True, **kw)


def current(dialog, key):
    return dialog._combos[key].currentData()


# ── what the drop-downs offer ────────────────────────────────────────────────

def test_candidates_use_wasapi_devices_with_cables_first():
    cand = wa.candidates(DEVICES)
    assert [d.name for d in cand["cable_in"][:4]] == [f"CABLE-{c} Output (VB-Audio Cable {c})" for c in "ABCD"]
    assert all(d.wasapi for lists in cand.values() for d in lists)
    assert cand["headset"][0].name.startswith("Headset Earphone") or cand["headset"][0].name.startswith("Speakers")
    assert all("CABLE" not in d.name for d in cand["headset"][:2])
    assert [d.name for d in cand["cable_out"][:4]] == [f"CABLE-{c} Input (VB-Audio Cable {c})" for c in "ABCD"]
    assert all(d.max_inputs > 0 for d in cand["mic"])


def test_without_wasapi_every_device_is_offered():
    mme_only = [dev(1, "Speakers", outs=2, api="MME")]
    assert wa.candidates(mme_only)["headset"][0].name == "Speakers"


def test_combo_items_keep_a_configured_device_that_is_gone():
    options = [DEVICES[0], DEVICES[2]]
    items = wa.combo_items(options, "Old Cable Output", optional=True, lang="en")
    assert items[0] == ("(none)", "") and items[-1] == ("Old Cable Output (not found)", "Old Cable Output")
    assert wa.pick_index(items, "Old Cable Output") == len(items) - 1
    # a piece of a name picks the device that contains it
    items = wa.combo_items(options, "cable-b", optional=False, lang="en")
    assert items[wa.pick_index(items, "cable-b")][1].startswith("CABLE-B")
    assert wa.pick_index(items, "nothing") == 0


def test_the_same_device_listed_twice_appears_once():
    items = wa.combo_items([DEVICES[10], DEVICES[11]], "", optional=False)
    assert [v for _l, v in items] == ["Speakers (Realtek(R) Audio)"]


# ── the dialog ───────────────────────────────────────────────────────────────

def test_a_first_run_preselects_the_suggestion(tmp_path, qapp):
    d = make(tmp_path, qapp)
    assert current(d, "in:game").startswith("CABLE-A Output") and current(d, "in:chat").startswith("CABLE-B Output")
    assert current(d, "in:media").startswith("CABLE-C Output") and current(d, "in:aux") == ""
    assert current(d, "out:headset").startswith("Headset Earphone")
    assert current(d, "mic:in").startswith("Headset Microphone") and current(d, "mic:out").startswith("CABLE-D Input")
    assert d._latency.currentData() == "balanced" and d._limiter.isChecked()
    assert d._save_btn.isEnabled()


def test_an_existing_configuration_is_shown_not_replaced(tmp_path, qapp):
    save_config(EngineConfig(latency="safe", inputs={"game": "CABLE-B Output", "chat": "", "media": "", "aux": ""},
                             outputs={"headset": "Speakers (Realtek"}, master_limiter=False, block=960), tmp_path)
    d = make(tmp_path, qapp)
    assert current(d, "in:game").startswith("CABLE-B Output") and current(d, "in:chat") == ""
    assert current(d, "out:headset").startswith("Speakers") and current(d, "mic:in") == ""
    assert d._latency.currentData() == "safe" and not d._limiter.isChecked()


def test_detect_refills_the_form(tmp_path, qapp):
    save_config(EngineConfig(inputs={"game": "CABLE-D Output", "chat": "", "media": "", "aux": ""},
                             outputs={"headset": "Speakers (Realtek"}), tmp_path)
    d = make(tmp_path, qapp)
    d.detect()
    assert current(d, "in:game").startswith("CABLE-A Output") and current(d, "out:headset").startswith("Headset Earphone")


def test_no_cables_says_what_to_install(tmp_path, qapp):
    d = make(tmp_path, qapp, devices=[dev(1, "Headset Earphone (Arctis)", outs=2)])
    assert "VB-CABLE" in d._log.toPlainText()


def test_a_failed_scan_is_reported(tmp_path, qapp):
    def boom():
        raise RuntimeError("PortAudio is gone")
    d = make(tmp_path, qapp, probe=boom)
    assert "PortAudio is gone" in d._log.toPlainText() and d._save_btn.isEnabled()


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
    assert cfg.inputs["game"].startswith("CABLE-A Output") and cfg.outputs["headset"].startswith("Headset Earphone")
    assert cfg.mic_input.startswith("Headset Microphone") and cfg.mic_output.startswith("CABLE-D Input")
    assert not cfg.problems() and changed == [1]
    assert ensured == [(tmp_path, 10.0)]
    assert FakeClient.instances[-1].reconfigured == 1
    text = d._log.toPlainText()
    assert "Saved" in text and "Next:" in text


def test_save_keeps_the_settings_the_form_has_no_control_for(tmp_path, qapp):
    save_config(EngineConfig(block=960, mic_block=480, device_latency="high",
                             inputs={"game": "CABLE-A Output", "chat": "", "media": "", "aux": ""},
                             outputs={"headset": "Headset Earphone", "stream": "CABLE-D Input"},
                             routing={"game": "stream", "chat": "headset", "media": "headset", "aux": "headset"}), tmp_path)
    d = make(tmp_path, qapp)
    d.save()
    cfg = load_config(tmp_path)
    assert (cfg.block, cfg.mic_block, cfg.device_latency) == (960, 480, "high")
    assert cfg.outputs["stream"].startswith("CABLE-D Input") and cfg.routing["game"] == "stream"   # the name is made exact


def test_the_stream_cable_is_a_form_field_and_optional(tmp_path, qapp):
    """Streamer mode's cable is chosen in the form; leaving it empty leaves no entry (the engine then makes no mix)."""
    d = make(tmp_path, qapp)
    assert "out:stream" in d._combos and current(d, "out:stream") == ""            # four cables: nothing to spare
    assert d._combos["out:stream"].itemText(0) == "(none)"
    assert [d._combos["out:stream"].itemData(i) for i in range(1, 5)] == [
        f"CABLE-{c} Input (VB-Audio Cable {c})" for c in "ABCD"]                       # only cable *inputs* are offered
    d.save()
    assert "stream" not in load_config(tmp_path).outputs

    pick = d._combos["out:stream"]
    pick.setCurrentIndex(pick.findData("CABLE-A Input (VB-Audio Cable A)"))
    assert d.selected_config().outputs == {"headset": current(d, "out:headset"), "stream": "CABLE-A Input (VB-Audio Cable A)"}
    d.save()
    assert load_config(tmp_path).outputs["stream"] == "CABLE-A Input (VB-Audio Cable A)"


def test_unselecting_the_stream_cable_removes_it_from_the_saved_file(tmp_path, qapp):
    save_config(EngineConfig(inputs={"game": "CABLE-A Output", "chat": "", "media": "", "aux": ""},
                             outputs={"headset": "Headset Earphone", "stream": "CABLE-D Input"}), tmp_path)
    d = make(tmp_path, qapp)
    assert current(d, "out:stream").startswith("CABLE-D Input")
    d._combos["out:stream"].setCurrentIndex(0)
    d.save()
    assert "stream" not in load_config(tmp_path).outputs


def test_a_stream_cable_that_is_gone_stays_visible(tmp_path, qapp):
    save_config(EngineConfig(inputs={"game": "CABLE-A Output", "chat": "", "media": "", "aux": ""},
                             outputs={"headset": "Headset Earphone", "stream": "Old Cable Input"}), tmp_path)
    d = make(tmp_path, qapp)
    assert current(d, "out:stream") == "Old Cable Input"
    assert "(not found)" in d._combos["out:stream"].currentText()


def test_detect_suggests_the_spare_cable_for_the_stream(tmp_path, qapp):
    six = [*DEVICES, dev(13, "CABLE-E Output (VB-Audio Cable E)", ins=2), dev(14, "CABLE-E Input (VB-Audio Cable E)", outs=2),
           dev(15, "CABLE-F Output (VB-Audio Cable F)", ins=2), dev(16, "CABLE-F Input (VB-Audio Cable F)", outs=2)]
    d = make(tmp_path, qapp, devices=six)
    assert current(d, "mic:out").startswith("CABLE-E Input") and current(d, "out:stream").startswith("CABLE-F Input")


def test_save_needs_a_headset_and_a_channel(tmp_path, qapp):
    d = make(tmp_path, qapp)
    for key in ("in:game", "in:chat", "in:media", "in:aux"):
        d._combos[key].setCurrentIndex(0)
    d.save()
    assert "headset and one channel cable" in d._log.toPlainText()
    assert not (tmp_path / "win_audio_engine.json").exists()


def test_problems_the_engine_reports_are_shown(tmp_path, qapp):
    class Picky(FakeClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.problems = ["the same device is assigned to two channels"]
    d = make(tmp_path, qapp, client_factory=Picky)
    d.save()
    assert "two channels" in d._log.toPlainText()


def test_an_engine_that_will_not_start_is_reported(tmp_path, qapp):
    d = make(tmp_path, qapp, ensure_engine=lambda cfg, wait=0.0: False)
    d.save()
    assert "could not be started" in d._log.toPlainText() and d._save_btn.isEnabled()
    assert load_config(tmp_path).inputs["game"]                                   # the file was still saved


def test_the_test_button_pauses_a_running_engine_and_restarts_it(tmp_path, qapp):
    events = []
    d = make(tmp_path, qapp, ensure_engine=lambda cfg, wait=0.0: events.append("start") or True,
             stop=lambda cfg: events.append("stop") or True,
             selftest=lambda cfg: events.append("selftest") or "ALL GOOD")
    d.test()
    assert events == ["stop", "selftest", "start"] and "ALL GOOD" in d._log.toPlainText() and d._test_btn.isEnabled()


def test_the_test_button_leaves_a_stopped_engine_stopped(tmp_path, qapp):
    class Down(FakeClient):
        def available(self):
            return False
    events = []
    d = make(tmp_path, qapp, client_factory=Down, ensure_engine=lambda cfg, wait=0.0: events.append("start") or True,
             stop=lambda cfg: events.append("stop") or True, selftest=lambda cfg: events.append("selftest") or "ok")
    d.test()
    assert events == ["selftest"]


def test_a_crashing_test_is_reported(tmp_path, qapp):
    def boom(cfg):
        raise RuntimeError("no sounddevice")
    d = make(tmp_path, qapp, selftest=boom)
    d.test()
    assert "no sounddevice" in d._log.toPlainText() and d._test_btn.isEnabled()


def test_the_state_line_follows_the_engine(tmp_path, qapp):
    status = {"running": True, "profile": "balanced", "latency_ms": 69.9,
              "endpoints": {"in:game": {"state": "running", "device": "CABLE-A Output"}}}
    bridge = FakeBridge(status)
    d = make(tmp_path, qapp, bridge=bridge)
    assert "running" in d._state_lbl.text() and "Game: running (CABLE-A Output)" in d._state_lbl.text()
    bridge.value = None
    d.refresh_state()
    assert "not running" in d._state_lbl.text() or "not set up" in d._state_lbl.text()


def test_threaded_scan_delivers_its_result(tmp_path, qapp):
    """The real (asynchronous) path: the worker thread's result reaches the form."""
    import time
    d = wa.AudioEngineSetupDialog(config_dir=tmp_path, probe=lambda: list(DEVICES), ensure_engine=lambda *a, **k: True,
                                  stop=lambda *a: True, selftest=lambda c: "", client_factory=FakeClient,
                                  bridge=FakeBridge(), synchronous=False, poll_ms=60000)
    try:
        deadline = time.time() + 5
        while not d._save_btn.isEnabled() and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        qapp.processEvents()
        assert d._save_btn.isEnabled() and current(d, "in:game").startswith("CABLE-A Output")
    finally:
        d.close()


def test_selftest_text_runs_the_engine_module(monkeypatch, tmp_path):
    seen = {}

    class R:
        stdout, stderr, returncode = "tone ok\n", "warn\n", 0
    monkeypatch.setattr(wa.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd) or R())
    assert wa.selftest_text(tmp_path) == "tone ok\nwarn"
    assert seen["cmd"][1:4] == ["-m", wa.MODULE, "--selftest"] and "--config-dir" in seen["cmd"]
