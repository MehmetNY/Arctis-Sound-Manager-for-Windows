"""The status strip about the audio engine."""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                                            # noqa: E402

from arctis_sound_manager.gui.win_engine_banner import EngineBanner                    # noqa: E402
from arctis_sound_manager.win.audio.engine_config import EngineConfig, save_config      # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeBridge:
    def __init__(self, status=None):
        self.value = status

    def status(self, max_age=None):
        return self.value


OK = {"running": True, "profile": "balanced", "latency_ms": 70.0, "endpoints": {"in:game": {"state": "running"}}}


def configure(tmp_path):
    save_config(EngineConfig(inputs={"game": "CABLE-A Output", "chat": "", "media": "", "aux": ""},
                             outputs={"headset": "Arctis"}), tmp_path)


def make(tmp_path, bridge=None, **kw):
    kw.setdefault("enabled", lambda: True)
    kw.setdefault("lang", lambda: "en")
    return EngineBanner(bridge=bridge or FakeBridge(), config_dir=tmp_path, **kw)


def test_an_unconfigured_engine_offers_the_assistant(tmp_path, qapp):
    b = make(tmp_path)
    b.set_page_visible(True)
    assert b.state.code == "unconfigured" and not b.isHidden() and "not set up" in b.text()
    assert not b._setup_btn.isHidden() and b._start_btn.isHidden()


def test_a_configured_but_stopped_engine_offers_start_and_setup(tmp_path, qapp):
    configure(tmp_path)
    b = make(tmp_path)
    b.set_page_visible(True)
    assert b.state.code == "stopped" and not b._start_btn.isHidden() and not b._setup_btn.isHidden()


def test_a_healthy_engine_hides_the_strip(tmp_path, qapp):
    configure(tmp_path)
    b = make(tmp_path, FakeBridge(OK))
    b.set_page_visible(True)
    assert b.state.healthy and b.isHidden()


def test_the_strip_only_shows_on_pages_that_need_the_engine(tmp_path, qapp):
    b = make(tmp_path)
    b.set_page_visible(False)
    assert b.isHidden()
    b.set_page_visible(True)
    assert not b.isHidden()
    b.set_page_visible(False)
    assert b.isHidden()


def test_a_switched_off_engine_stays_quiet(tmp_path, qapp):
    b = make(tmp_path, enabled=lambda: False)
    b.set_page_visible(True)
    assert b.state.code == "off" and b.isHidden()


def test_a_device_problem_is_named_and_offers_setup(tmp_path, qapp):
    configure(tmp_path)
    status = dict(OK, endpoints={"in:game": {"state": "running"}, "out:headset": {"state": "waiting", "error": "gone"}})
    b = make(tmp_path, FakeBridge(status))
    b.set_page_visible(True)
    assert b.state.code == "problem" and "Headset: gone" in b.text() and not b._setup_btn.isHidden()


def test_it_follows_the_engine_and_reports_changes_once(tmp_path, qapp):
    configure(tmp_path)
    bridge = FakeBridge(None)
    b = make(tmp_path, bridge)
    assert b.state.code == "stopped"
    codes = []
    b.sig_state.connect(codes.append)
    b.set_page_visible(True)
    b.refresh(); b.refresh()
    assert codes == []                                           # still "stopped": nothing to report
    bridge.value = OK
    b.refresh(); b.refresh()
    assert codes == ["ok"] and b.isHidden()                      # reported once, not on every poll


def test_start_launches_the_engine_in_the_background_and_shows_starting(tmp_path, qapp):
    configure(tmp_path)
    calls = []
    b = make(tmp_path, starter=lambda cfg, wait=0.0: calls.append((cfg, wait)) or True)
    b.set_page_visible(True)
    b.start()
    deadline = time.time() + 3
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert calls == [(tmp_path, 15.0)]
    assert b.state.code == "starting" and b._start_btn.isHidden()
    b.start()                                                    # a second click while starting does nothing
    time.sleep(0.05)
    assert len(calls) == 1


def test_starting_ends_when_the_engine_answers(tmp_path, qapp):
    configure(tmp_path)
    bridge = FakeBridge(None)
    b = make(tmp_path, bridge, starter=lambda cfg, wait=0.0: True)
    b.set_page_visible(True)
    b.start()
    assert b.state.code == "starting"
    bridge.value = OK
    b.refresh()
    assert b.state.code == "ok" and not b._starting


def test_a_failing_starter_does_not_raise(tmp_path, qapp):
    configure(tmp_path)

    def boom(cfg, wait=0.0):
        raise OSError("no python")
    b = make(tmp_path, starter=boom)
    b.set_page_visible(True)
    b.start()
    time.sleep(0.1)


def test_the_setup_button_opens_the_assistant_and_refreshes(tmp_path, qapp):
    opened = []
    b = make(tmp_path, open_setup=lambda: opened.append(1))
    b.set_page_visible(True)
    b._setup_btn.click()
    assert opened == [1]


def test_text_follows_the_ui_language(tmp_path, qapp):
    b = make(tmp_path, lang=lambda: "tr")
    b.set_page_visible(True)
    assert "kurulmadı" in b.text() and b._setup_btn.text() == "Kurulum..."


def test_a_broken_config_file_never_breaks_the_strip(tmp_path, qapp, monkeypatch):
    from arctis_sound_manager.gui import win_engine_banner as mod
    monkeypatch.setattr(mod, "load_config", lambda d: (_ for _ in ()).throw(OSError("denied")))
    b = make(tmp_path)
    b.set_page_visible(True)
    assert b.state.code == "unconfigured"


def test_it_only_polls_while_a_page_that_needs_the_engine_is_showing(tmp_path, qapp):
    b = make(tmp_path)
    assert not b._timer.isActive()
    b.set_page_visible(True)
    assert b._timer.isActive()
    b.set_page_visible(False)
    assert not b._timer.isActive()
