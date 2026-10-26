"""asm-gui under Windows conditions: no DISPLAY, no systemd, no udev, no PipeWire, no D-Bus.

A daemon process (fake Arctis Nova Pro Wireless X, status frame captured on real
hardware) and the real GUI ``main()`` run in separate clean interpreters, as on a
user's PC. The GUI must come up, draw, and show what the daemon reported.

Qt runs on its offscreen platform, so this proves the *logic* (nothing Linux-only on the
startup path, IPC works, the status reaches the widgets), not Windows' own look.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent


@pytest.fixture(scope="module")
def gui_run():
    tmp = tempfile.mkdtemp(prefix="asm-win-gui-")
    env = dict(os.environ, WIN_HARNESS_TMP=tmp, WIN_HARNESS_SERVE="1", QT_QPA_PLATFORM="offscreen")
    log = open(Path(tmp) / "daemon.log", "w+")
    daemon = subprocess.Popen([sys.executable, str(HERE / "win_daemon_harness.py")], env=env,
                              stdout=log, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            log.seek(0)
            if "READY" in log.read():
                break
            assert daemon.poll() is None, "daemon harness died:\n" + Path(log.name).read_text()[-2000:]
            time.sleep(0.3)
        else:
            pytest.fail("daemon harness never became ready:\n" + Path(log.name).read_text()[-2000:])

        proc = subprocess.run([sys.executable, str(HERE / "win_gui_harness.py")], env=env,
                              capture_output=True, text=True, timeout=120)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT:")), None)
        assert line, f"GUI produced no result.\nstdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
        yield json.loads(line[len("RESULT:"):]), proc
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
        log.close()


def test_the_window_opens_and_draws(gui_run):
    result, _ = gui_run
    assert result["title"] == "Arctis Sound Manager"
    assert result["saved"] and Path(result["shot"]).stat().st_size > 10_000
    assert result["distinct_colors"] > 5          # not a blank/black frame


def test_the_headset_reported_by_the_daemon_reaches_the_widgets(gui_run):
    result, _ = gui_run
    assert result["has_headset_name"] and result["has_online"] and result["has_battery"]


def test_a_windows_environment_is_not_mistaken_for_an_x11_desktop(gui_run):
    """Regression: with QT_QPA_PLATFORM and WAYLAND_DISPLAY both unset (Windows), asm-gui printed
    'libxcb-cursor0 is missing' and exited before opening any window."""
    result, _ = gui_run
    assert result["xcb_help"] is None, result["xcb_help"]


def test_startup_log_has_no_linux_tooling_errors(gui_run):
    _, proc = gui_run
    log = proc.stdout + proc.stderr
    assert "Traceback" not in log
    for noise in ("systemctl", "pkexec", "udev", "asm-setup", "XDG", "DISPLAY"):
        assert noise not in log, f"Linux-only tooling surfaced in the Windows GUI run: {noise!r}"


def test_clips_is_removed_from_the_windows_build_not_just_hidden(gui_run):
    """The user asked for it gone from inside the program: no button (hidden or not), no page, no
    text anywhere in the window or in the Help page."""
    result, _ = gui_run
    assert len(result["sidebar_labels"]) == 7, result["sidebar_labels"]        # Channels, EQ, Headset, DAC, Settings, Help, About
    assert result["stack_pages"] == 8                                           # seven pages + the theme editor
    assert result["clips_page_object"] is False
    assert result["help_has_clips"] is False
    assert result["clip_text_anywhere"] == [], result["clip_text_anywhere"]


def test_the_pages_after_the_removed_one_are_still_the_right_pages(gui_run):
    """Sidebar index == stack index. Removing a page renumbers the ones after it; Settings, Help and
    the theme editor must still land on their own widgets."""
    result, _ = gui_run
    assert result["page_numbers"] == [4, 5, 6, 7]
    assert result["settings_page_is_settings"] and result["help_page_is_help"] and result["theme_page_is_theme"]
    assert result["about_page_is_about"]


def test_about_sits_under_help_with_the_asked_for_content_and_the_sidebar_donates_to_boomfi(gui_run):
    result, _ = gui_run
    assert result["sidebar_labels"][-1] == "About" and result["sidebar_labels"][-2] == "Help"
    text = "\n".join(result["about_labels"])
    assert "github.com/MehmetNY/Arctis-Sound-Manager-for-Windows" in text
    assert "elegos/Linux-Arctis-Manager" in text and "loteran/Arctis-Sound-Manager" in text
    assert result["about_saved"] and Path(result["about_shot"]).stat().st_size > 10_000
    assert result["donation_opened"] == ["https://pay.boomfi.xyz/lite/3LDETXyLVlVUrjbQwydY5dPZGYY"]
    assert "Ko-fi" not in result["donate_label"]


def test_the_sidebar_keeps_its_look_in_a_tall_window_and_scrolls_instead_of_overlapping_in_a_short_one(gui_run):
    """Seven entries need about 760 px of buttons. A tall window shows them all (no scroll bar); a laptop's 700 px
    window scrolls the buttons while the logo / donation block stays below them - the About button never runs into
    the logo."""
    result = gui_run[0]
    tall = result["nav_at_default"]
    assert tall["scroll_range"] == 0, tall                     # nothing to scroll at the default size
    short = result["nav_short"]
    assert short["window_h"] <= 700 and short["bar_range"] > 0, short            # it does scroll when short
    assert short["sidebar_min"] <= 600, short                  # the column no longer forces a tall window
    assert short["nav_bottom_vs_block"] >= 0, short            # the buttons end above the logo block
    assert short["about_bottom_vs_block"] >= 0, short          # About (the active page) is scrolled into view
    assert short["saved"] and Path(short["shot"]).stat().st_size > 10_000


def test_the_sound_processing_cards_are_on_the_headset_page_and_the_equaliser_stays_on_its_own(gui_run):
    """Spatial audio, volume boost, Smart Volume and the microphone's noise cancelling / reduction / gate /
    compressor are headset settings and live on the Headset page. The equaliser - curve, presets, macro
    sliders - is NOT among them: it stays on the Equalizer page."""
    placement = gui_run[0]["placement"]
    assert placement["cards_on_headset"] == {ch: True for ch in ("game", "media", "aux", "chat", "micro")}
    assert placement["eq_on_equalizer"] and all(placement["eq_on_equalizer"].values()), placement
    assert not any(placement["eq_on_headset"].values()), placement
    assert placement["presets_on_equalizer"] and all(placement["presets_on_equalizer"].values()), placement
    assert not any(placement["presets_on_headset"].values()), placement


def test_no_engine_banner_anywhere(gui_run):
    """The Windows build starts its engine by itself; there is no "engine not set up" strip over the pages."""
    result, _ = gui_run
    assert result["has_banner_object"] is False
    assert result["nag_text"] == [], result["nag_text"]


def test_the_dac_wheel_osd_watcher_connects_to_the_daemon(gui_run):
    """Regression: it failed every second with "'MessageBus' object has no attribute 'introspect'"."""
    result, proc = gui_run
    assert result["wheel_errors"] == [], result["wheel_errors"]
    assert result["wheel_baselines"] >= 1, "the watcher never got as far as subscribing to StatusChanged"
    assert "has no attribute 'introspect'" not in proc.stdout + proc.stderr


def test_no_linux_audio_tooling_noise_in_the_gui_log(gui_run):
    _, proc = gui_run
    log = proc.stdout + proc.stderr
    for noise in ("pulsectl", "pactl", "pw-cli", "Sonar node", "source-outputs", "Desktop session query failed"):
        assert noise not in log, f"Linux audio tooling surfaced in the Windows GUI log: {noise!r}"
