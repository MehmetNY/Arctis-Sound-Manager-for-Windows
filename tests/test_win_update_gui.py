"""The Windows build's Settings page and update plumbing: no telemetry row, no Linux-only audio menus, an honest
update check, and an update worker that only ever starts a verified installer."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from arctis_sound_manager import update_checker as uc
from arctis_sound_manager.win import updates


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _page(monkeypatch, windows: bool):
    from arctis_sound_manager.gui import device_page
    monkeypatch.setattr(device_page, "IS_WINDOWS", windows)
    return device_page.DevicePage()


def _shown_keys(page) -> set[str]:
    return {key for group in page._settings_groups for key in (group._keys or ())}


# ── Settings page ───────────────────────────────────────────────────────────

def test_windows_has_no_telemetry_row_and_other_systems_keep_it(qapp, monkeypatch):
    assert not hasattr(_page(monkeypatch, True), "_telemetry_toggle")
    assert hasattr(_page(monkeypatch, False), "_telemetry_toggle")


LINUX_ONLY = {"redirect_audio_on_connect", "redirect_audio_on_connect_channel", "redirect_audio_on_disconnect",
              "redirect_audio_on_disconnect_device", "external_output_device", "pipewire_quantum"}


def test_windows_hides_the_audio_menus_that_only_exist_on_linux(qapp, monkeypatch):
    windows = _shown_keys(_page(monkeypatch, True))
    assert not windows & LINUX_ONLY
    assert "hrir_id" in windows                                    # spatial audio's response file stays
    assert LINUX_ONLY <= _shown_keys(_page(monkeypatch, False))


def test_the_hidden_audio_keys_do_not_reappear_in_general(qapp, monkeypatch):
    page = _page(monkeypatch, True)
    general = page._general_widget
    assert general._keys is None and LINUX_ONLY <= general._exclude
    assert not any(general._in_scope(key) for key in LINUX_ONLY)


def test_an_unreachable_github_is_not_reported_as_up_to_date(qapp, monkeypatch):
    page = _page(monkeypatch, True)
    page._on_check_update_failed("could not reach GitHub: no network")
    assert page._check_update_btn.isEnabled()
    assert "up to date" not in page._update_status_lbl.text().lower()
    assert page._update_status_lbl.text() and page._update_status_lbl.toolTip().startswith("could not reach")


def test_a_found_update_is_offered_for_clicking_on_windows(qapp, monkeypatch):
    page = _page(monkeypatch, True)
    seen = []
    page.sig_update_result.connect(lambda *a: seen.append(a))
    page._on_check_update_result("1.7.0", "https://example/release", "https://example/ASM-Setup-1.7.0.exe")
    assert "1.7.0" in page._update_status_lbl.text()
    assert page._update_version == "1.7.0" and seen == [("1.7.0", "https://example/release",
                                                         "https://example/ASM-Setup-1.7.0.exe")]


# ── the checker ─────────────────────────────────────────────────────────────

def _collect(worker):
    got = {"result": [], "failed": []}
    worker.result.connect(lambda *a: got["result"].append(a))
    worker.failed.connect(lambda *a: got["failed"].append(a))
    return got


def offer(version="1.7.0"):
    return updates.Offer(version=version, tag=f"v{version}", page_url="https://example/page",
                         installer_url="https://example/setup.exe", installer_name=f"ASM-Setup-{version}.exe")


def test_on_windows_the_checker_asks_the_ports_own_releases(qapp, monkeypatch):
    monkeypatch.setattr(uc, "IS_WINDOWS", True)
    monkeypatch.setattr(updates, "check", lambda current: offer())
    monkeypatch.setattr(uc, "detect_all_install_methods", lambda: pytest.fail("package managers were probed"))
    worker = uc.UpdateCheckWorker("1.6.4")
    got = _collect(worker)
    worker.run()
    assert got == {"result": [("1.7.0", "https://example/page", "https://example/setup.exe")], "failed": []}


def test_on_windows_no_newer_release_means_an_empty_result(qapp, monkeypatch):
    monkeypatch.setattr(uc, "IS_WINDOWS", True)
    monkeypatch.setattr(updates, "check", lambda current: None)
    worker = uc.UpdateCheckWorker("1.6.4")
    got = _collect(worker)
    worker.run()
    assert got == {"result": [("", "", "")], "failed": []}


@pytest.mark.parametrize("failure", [updates.UpdateError("could not reach GitHub"), RuntimeError("boom")])
def test_on_windows_a_failed_check_says_failed_not_up_to_date(qapp, monkeypatch, failure):
    monkeypatch.setattr(uc, "IS_WINDOWS", True)

    def broken(current):
        raise failure
    monkeypatch.setattr(updates, "check", broken)
    worker = uc.UpdateCheckWorker("1.6.4")
    got = _collect(worker)
    worker.run()
    assert got["result"] == [] and len(got["failed"]) == 1


# ── the install worker ──────────────────────────────────────────────────────

def _install(monkeypatch, *, check=None, download=None, launch=None):
    monkeypatch.setattr(updates, "check", check or (lambda current: offer()))
    monkeypatch.setattr(updates, "download", download or (lambda o, progress=None, cancelled=None: "installer.exe"))
    started = []
    monkeypatch.setattr(updates, "launch_installer", launch or (lambda path: started.append(path)))
    worker = uc.WinUpdateInstallWorker("1.6.4")
    done = []
    worker.done.connect(lambda ok, message: done.append((ok, message)))
    return worker, done, started


def test_the_worker_downloads_then_starts_the_installer_and_reports_the_version(qapp, monkeypatch):
    worker, done, started = _install(monkeypatch)
    worker.run()
    assert done == [(True, "1.7.0")] and started == ["installer.exe"]


def test_a_vanished_release_starts_nothing(qapp, monkeypatch):
    worker, done, started = _install(monkeypatch, check=lambda current: None)
    worker.run()
    assert done[0][0] is False and started == []


def test_a_failed_download_starts_nothing(qapp, monkeypatch):
    def download(o, progress=None, cancelled=None):
        raise updates.UpdateError("does not match the checksum")
    worker, done, started = _install(monkeypatch, download=download)
    worker.run()
    assert done == [(False, "does not match the checksum")] and started == []


def test_a_refused_permission_is_reported_and_the_program_stays_open(qapp, monkeypatch):
    def launch(path):
        raise updates.UpdateError("the installer was not started (permission refused?)")
    worker, done, _ = _install(monkeypatch, launch=launch)
    worker.run()
    assert done[0][0] is False and "not started" in done[0][1]


def test_cancelling_reaches_the_download(qapp, monkeypatch):
    seen = []

    def download(o, progress=None, cancelled=None):
        seen.append(cancelled())
        raise updates.UpdateError("cancelled")
    worker, done, started = _install(monkeypatch, download=download)
    worker.cancel()
    worker.run()
    assert seen == [True] and done == [(False, "cancelled")] and started == []
