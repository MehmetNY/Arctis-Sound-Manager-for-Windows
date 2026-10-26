"""win/updates.py: which release counts, what is downloaded, and that nothing unchecked is ever run."""
from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from arctis_sound_manager.win import updates
from arctis_sound_manager.win.updates import Offer, UpdateError

BASE = updates.DOWNLOAD_PREFIX + "v1.7.0/"


def release(tag="v1.7.0", *, draft=False, prerelease=False, assets=None, body="notes"):
    if assets is None:
        assets = [asset("ASM-Setup-1.7.0.exe")]
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "assets": assets, "body": body,
            "html_url": f"https://github.com/{updates.REPO}/releases/tag/{tag}"}


def asset(name, *, url=None, digest="", size=5):
    out = {"name": name, "browser_download_url": url if url is not None else BASE + name, "size": size}
    if digest:
        out["digest"] = digest
    return out


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, headers=None):
        super().__init__(payload)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(payload: bytes, headers=None, seen=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append(request)
        return FakeResponse(payload, headers)
    return opener


def http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "x", {}, None)


# ── versions ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("candidate,current,newer", [
    ("1.7.0", "1.6.4", True), ("v1.7.0", "1.6.4", True), ("1.6.5", "1.6.4", True), ("2.0", "1.9.9", True),
    ("1.6.4", "1.6.4", False), ("1.6.3", "1.6.4", False), ("1.6.4.1", "1.6.4", True), ("1.6.4", "1.6.4.0", False),
    ("1.10.0", "1.9.0", True),                                    # numbers, not text
    ("1.7.0b2", "1.7.0", False), ("1.7.0", "1.7.0b2", True),      # a beta is before its release
    ("junk", "1.6.4", False), ("1.7.0", "junk", False), ("", "1.6.4", False),
])
def test_versions_compare_as_numbers(candidate, current, newer):
    assert updates.is_newer(candidate, current) is newer


def test_parse_version_flags_prereleases():
    assert updates.parse_version("v1.7.0") == ((1, 7, 0), False)
    assert updates.parse_version("1.7.0rc1") == ((1, 7, 0), True)
    assert updates.parse_version("1.7.0-win2") == ((1, 7, 0), False)
    assert updates.parse_version("nope") is None


# ── what a release offers ───────────────────────────────────────────────────

def test_a_newer_published_release_with_its_installer_is_offered():
    rel = release(assets=[asset("notes.txt"), asset("ASM-Setup-1.7.0.exe", digest="sha256:ABCDEF", size=123)])
    offer = updates.offer_from_release(rel, "1.6.4")
    assert offer == Offer(version="1.7.0", tag="v1.7.0", page_url=rel["html_url"],
                          installer_url=BASE + "ASM-Setup-1.7.0.exe", installer_name="ASM-Setup-1.7.0.exe",
                          size=123, sha256="abcdef", notes="notes")


@pytest.mark.parametrize("rel,current", [
    (release(), "1.7.0"), (release(), "2.0.0"),                                   # not newer
    (release(draft=True), "1.6.4"), (release(prerelease=True), "1.6.4"),          # not published
    (release(tag="v1.7.0-beta1"), "1.6.4"), (release(tag="windows-latest"), "1.6.4"),
    (release(assets=[]), "1.6.4"),                                                # the installer is not attached yet
    (release(assets=[asset("source.zip")]), "1.6.4"),
    (release(assets=[asset("ASM-Setup-1.7.0.exe", url="https://evil.example/ASM-Setup-1.7.0.exe")]), "1.6.4"),
    (release(assets=[asset("ASM-Setup-1.7.0.exe", url="https://github.com/someone/else/releases/download/v1/ASM-Setup-1.7.0.exe")]), "1.6.4"),
    (release(assets=[asset("..\\evil.exe")]), "1.6.4"),
    ("not a release", "1.6.4"), ({}, "1.6.4"),
])
def test_everything_else_is_not_offered(rel, current):
    assert updates.offer_from_release(rel, current) is None


def test_an_asset_without_a_digest_is_still_offered_but_without_checksum():
    offer = updates.offer_from_release(release(), "1.6.4")
    assert offer is not None and offer.sha256 == ""


# ── asking GitHub ───────────────────────────────────────────────────────────

def test_fetch_latest_reads_the_json_and_identifies_itself():
    seen = []
    data = updates.fetch_latest(opener_for(json.dumps(release()).encode(), seen=seen))
    assert data["tag_name"] == "v1.7.0"
    assert seen[0].full_url == updates.API_URL
    assert seen[0].get_header("User-agent").startswith("ArctisSoundManager-Windows")


def test_a_repository_without_releases_is_not_an_error():
    def opener(request, timeout=None):
        raise http_error(404)
    assert updates.fetch_latest(opener) is None
    assert updates.check("1.6.4", opener) is None


@pytest.mark.parametrize("failure", [http_error(500), urllib.error.URLError("no network"), OSError("reset"),
                                     TimeoutError("slow")])
def test_other_failures_are_reported_not_swallowed(failure):
    def opener(request, timeout=None):
        raise failure
    with pytest.raises(UpdateError):
        updates.fetch_latest(opener)


def test_garbage_from_github_is_an_error_or_nothing():
    with pytest.raises(UpdateError):
        updates.fetch_latest(opener_for(b"<html>"))
    assert updates.fetch_latest(opener_for(b"[1, 2]")) is None


def test_check_combines_both_steps():
    offer = updates.check("1.6.4", opener_for(json.dumps(release()).encode()))
    assert offer is not None and offer.version == "1.7.0"
    assert updates.check("1.7.0", opener_for(json.dumps(release()).encode())) is None


def test_the_test_hooks_redirect_the_api_and_the_download_prefix(monkeypatch):
    monkeypatch.setenv(updates.TEST_API_ENV, "http://127.0.0.1:9/api")
    monkeypatch.setenv(updates.TEST_PREFIX_ENV, "http://127.0.0.1:9/dl/")
    seen = []
    rel = release(assets=[asset("ASM-Setup-1.7.0.exe", url="http://127.0.0.1:9/dl/ASM-Setup-1.7.0.exe")])
    offer = updates.check("1.6.4", opener_for(json.dumps(rel).encode(), seen=seen))
    assert seen[0].full_url == "http://127.0.0.1:9/api" and offer is not None


# ── downloading ─────────────────────────────────────────────────────────────

PAYLOAD = b"MZ\x90\x00 pretend installer " * 100


def offer_for(payload=PAYLOAD, **over):
    values = dict(version="1.7.0", tag="v1.7.0", page_url="p", installer_url=BASE + "ASM-Setup-1.7.0.exe",
                  installer_name="ASM-Setup-1.7.0.exe", size=len(payload),
                  sha256=hashlib.sha256(payload).hexdigest())
    values.update(over)
    return Offer(**values)


def test_download_writes_the_file_reports_progress_and_verifies(tmp_path):
    steps = []
    path = updates.download(offer_for(), tmp_path, progress=lambda done, total: steps.append((done, total)),
                            opener=opener_for(PAYLOAD))
    assert path == tmp_path / "ASM-Setup-1.7.0.exe" and path.read_bytes() == PAYLOAD
    assert steps[-1] == (len(PAYLOAD), len(PAYLOAD)) and steps == sorted(steps)


def test_download_into_a_new_temporary_folder_by_default():
    path = updates.download(offer_for(), opener=opener_for(PAYLOAD))
    try:
        assert path.exists() and path.parent.name.startswith("asm_update_")
    finally:
        path.unlink()
        path.parent.rmdir()


def test_a_file_that_does_not_match_the_checksum_is_deleted_and_refused(tmp_path):
    with pytest.raises(UpdateError, match="checksum"):
        updates.download(offer_for(sha256="0" * 64), tmp_path, opener=opener_for(PAYLOAD))
    assert list(tmp_path.iterdir()) == []


def test_a_truncated_download_is_refused(tmp_path):
    with pytest.raises(UpdateError, match="incomplete"):
        updates.download(offer_for(size=len(PAYLOAD) + 1, sha256=""), tmp_path, opener=opener_for(PAYLOAD))
    assert list(tmp_path.iterdir()) == []


def test_without_a_stated_checksum_or_size_the_file_is_accepted(tmp_path):
    path = updates.download(offer_for(size=0, sha256=""), tmp_path, opener=opener_for(PAYLOAD))
    assert path.read_bytes() == PAYLOAD


def test_a_network_failure_leaves_nothing_behind(tmp_path):
    def opener(request, timeout=None):
        raise urllib.error.URLError("dropped")
    with pytest.raises(UpdateError, match="failed"):
        updates.download(offer_for(), tmp_path, opener=opener)
    assert list(tmp_path.iterdir()) == []


def test_cancelling_stops_the_download_and_removes_the_file(tmp_path):
    with pytest.raises(UpdateError, match="cancelled"):
        updates.download(offer_for(), tmp_path, opener=opener_for(PAYLOAD), cancelled=lambda: True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("over", [{"installer_url": "https://evil.example/ASM-Setup-1.7.0.exe"},
                                  {"installer_name": "evil.exe"}, {"installer_name": "..\\ASM-Setup-1.exe"}])
def test_an_unexpected_address_or_name_is_never_fetched(tmp_path, over):
    def opener(request, timeout=None):
        pytest.fail("a request was made")
    with pytest.raises(UpdateError, match="unexpected"):
        updates.download(offer_for(**over), tmp_path, opener=opener)


# ── running the installer ───────────────────────────────────────────────────

def test_the_installer_runs_silently_with_progress_and_asks_to_reopen_the_program():
    program, params = updates.installer_command(Path("C:/tmp/ASM-Setup-1.7.0.exe"))
    assert program.endswith("ASM-Setup-1.7.0.exe")
    assert params.split() == ["/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"]


def test_launch_elevates_and_reports_a_refused_permission(monkeypatch):
    calls = []

    def shell_execute(hwnd, verb, program, params, folder, show):
        calls.append((verb, program, params))
        return result[0]

    result = [42]
    import ctypes
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(ShellExecuteW=shell_execute)),
                        raising=False)
    monkeypatch.setattr(updates.sys, "platform", "win32")
    updates.launch_installer(Path("C:/tmp/ASM-Setup-1.7.0.exe"))
    assert calls[0][0] == "runas" and "/RELAUNCH=1" in calls[0][2]
    result[0] = 5                                                 # ShellExecute's "access denied"
    with pytest.raises(UpdateError, match="not started"):
        updates.launch_installer(Path("C:/tmp/ASM-Setup-1.7.0.exe"))


def test_launch_is_refused_off_windows(monkeypatch):
    monkeypatch.setattr(updates.sys, "platform", "linux")
    with pytest.raises(UpdateError):
        updates.launch_installer(Path("x.exe"))


# ── announcing once ─────────────────────────────────────────────────────────

def test_a_version_is_announced_once_and_a_later_one_again(tmp_path):
    assert updates.should_announce("1.7.0", tmp_path) is True
    updates.mark_notified("1.7.0", tmp_path)
    assert updates.notified(tmp_path) == "1.7.0"
    assert updates.should_announce("1.7.0", tmp_path) is False
    assert updates.should_announce("1.6.9", tmp_path) is False
    assert updates.should_announce("1.7.1", tmp_path) is True
    assert updates.should_announce("", tmp_path) is False


def test_an_unreadable_state_file_means_nothing_was_announced(tmp_path):
    (tmp_path / "update_notified.json").write_text("{not json")
    assert updates.notified(tmp_path) == "" and updates.should_announce("1.7.0", tmp_path) is True
