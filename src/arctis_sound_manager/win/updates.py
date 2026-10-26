# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Updates of the Windows port itself: the newest *published* (not pre-release) release of its GitHub repository.

Nothing here needs Qt, so the tray, the window and the tests share it.

* :func:`check` asks ``releases/latest`` - GitHub leaves drafts and pre-releases out of it, so the continuously
  rebuilt ``windows-latest`` pre-release never triggers a notification; only a release somebody published does.
  A release counts once its installer (``ASM-Setup-<version>.exe``) is attached: the workflow that builds it
  needs a few minutes after the release is published, and an announcement without a download is worse than none.
* :func:`download` fetches that installer over HTTPS and checks it against the SHA-256 GitHub states for the
  asset (and its size), so a damaged or swapped file is never run.
* :func:`launch_installer` starts it elevated and silently-with-progress (``/SILENT``); the installer ends the
  running ASM processes, replaces the files and, because of ``/RELAUNCH=1``, opens the program again.
* :func:`notified` / :func:`mark_notified` remember which version the tray already announced.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from arctis_sound_manager import branding
from arctis_sound_manager.win.audio.params import default_config_dir

log = logging.getLogger(__name__)

REPO = branding.REPO
RELEASES_PAGE = branding.RELEASES_URL
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
#: Where installers may come from. Whatever the API says, a download elsewhere is refused.
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
#: Only for tests (a local server pretending to be GitHub); never set by the program itself.
TEST_API_ENV = "ASM_UPDATE_API_URL"
TEST_PREFIX_ENV = "ASM_UPDATE_DOWNLOAD_PREFIX"

INSTALLER_ARGS = ("/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1")
_INSTALLER_NAME = re.compile(r"^ASM-Setup-[\w.+-]+\.exe$", re.IGNORECASE)
_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)(.*)$", re.IGNORECASE)
_PRERELEASE_SUFFIX = re.compile(r"^[-._+]?(a|b|rc|alpha|beta|dev|pre|preview)\d*", re.IGNORECASE)
_TIMEOUT = 10.0


class UpdateError(Exception):
    """The update could not be fetched or must not be run."""


@dataclass(frozen=True)
class Offer:
    version: str
    tag: str
    page_url: str
    installer_url: str
    installer_name: str
    size: int = 0
    sha256: str = ""
    notes: str = ""


# ── versions ────────────────────────────────────────────────────────────────

def parse_version(text: str) -> tuple[tuple[int, ...], bool] | None:
    """``"v1.7.0"`` -> ``((1, 7, 0), False)``; ``"1.7.0b2"`` -> ``((1, 7, 0), True)`` (a pre-release); junk -> None."""
    match = _VERSION.match((text or "").strip())
    if not match:
        return None
    numbers = tuple(int(part) for part in match.group(1).split("."))
    return numbers, bool(_PRERELEASE_SUFFIX.match(match.group(2)))


def is_newer(candidate: str, current: str) -> bool:
    """Is ``candidate`` a later version than ``current``? Unreadable versions are never "newer"."""
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a[0]), len(b[0]))
    key = lambda v: (v[0] + (0,) * (width - len(v[0])), 0 if v[1] else 1)   # noqa: E731 — 1.7.0b2 < 1.7.0
    return key(a) > key(b)


# ── asking GitHub ───────────────────────────────────────────────────────────

def _api_url() -> str:
    return os.environ.get(TEST_API_ENV) or API_URL


def _download_prefix() -> str:
    return os.environ.get(TEST_PREFIX_ENV) or DOWNLOAD_PREFIX


def _request(url: str, accept: str) -> urllib.request.Request:
    from arctis_sound_manager.utils import project_version
    try:
        agent = f"ArctisSoundManager-Windows/{project_version()}"
    except Exception:                                                # noqa: BLE001
        agent = "ArctisSoundManager-Windows"
    return urllib.request.Request(url, headers={"Accept": accept, "User-Agent": agent})


def fetch_latest(opener: Callable = urllib.request.urlopen, timeout: float = _TIMEOUT) -> dict | None:
    """The JSON of the latest published release, or None when the repository has none (yet)."""
    try:
        with opener(_request(_api_url(), "application/vnd.github+json"), timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:                                          # no published release at all
            return None
        raise UpdateError(f"GitHub answered {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"could not reach GitHub: {exc}") from exc
    return data if isinstance(data, dict) else None


def offer_from_release(release: dict, current: str) -> Offer | None:
    """The offer a release makes to someone running ``current`` - None when it is not newer, is a draft or
    pre-release, or has no installer attached yet."""
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        return None
    tag = str(release.get("tag_name") or "")
    parsed = parse_version(tag)
    if parsed is None or parsed[1]:
        return None
    version = ".".join(str(n) for n in parsed[0])
    if not is_newer(version, current):
        return None
    prefix = _download_prefix()
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name, url = str(asset.get("name") or ""), str(asset.get("browser_download_url") or "")
        if not _INSTALLER_NAME.match(name) or not url.startswith(prefix):
            continue
        digest = str(asset.get("digest") or "")
        sha = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""
        size = asset.get("size")
        return Offer(version=version, tag=tag, page_url=str(release.get("html_url") or RELEASES_PAGE),
                     installer_url=url, installer_name=name, size=size if isinstance(size, int) else 0,
                     sha256=sha, notes=str(release.get("body") or ""))
    return None


def check(current: str, opener: Callable = urllib.request.urlopen) -> Offer | None:
    """The update on offer for someone running ``current``, or None. Raises :class:`UpdateError` when GitHub
    cannot be asked (the caller decides whether that is worth telling anybody)."""
    release = fetch_latest(opener)
    return offer_from_release(release, current) if release else None


# ── fetching and running the installer ──────────────────────────────────────

def download(offer: Offer, folder: Path | None = None, progress: Callable[[int, int], None] | None = None,
             opener: Callable = urllib.request.urlopen, cancelled: Callable[[], bool] | None = None) -> Path:
    """Fetch the installer into ``folder`` (a new temporary one by default) and verify it."""
    if not offer.installer_url.startswith(_download_prefix()) or not _INSTALLER_NAME.match(offer.installer_name):
        raise UpdateError("refusing to download from an unexpected address")
    folder = folder or Path(tempfile.mkdtemp(prefix="asm_update_"))
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / offer.installer_name
    digest = hashlib.sha256()
    done = 0
    try:
        with opener(_request(offer.installer_url, "application/octet-stream"), timeout=30) as response, \
                open(target, "wb") as out:
            total = int(response.headers.get("Content-Length") or offer.size or 0) if hasattr(response, "headers") \
                else offer.size
            while True:
                if cancelled and cancelled():
                    raise UpdateError("cancelled")
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except UpdateError:
        target.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise UpdateError(f"the download failed: {exc}") from exc
    if offer.size and done != offer.size:
        target.unlink(missing_ok=True)
        raise UpdateError(f"the download is incomplete ({done} of {offer.size} bytes)")
    if offer.sha256 and digest.hexdigest().lower() != offer.sha256.lower():
        target.unlink(missing_ok=True)
        raise UpdateError("the download does not match the checksum GitHub states for it")
    return target


def installer_command(path: Path) -> tuple[str, str]:
    """(program, parameter string) of the installer run."""
    return str(path), " ".join(INSTALLER_ARGS)


def launch_installer(path: Path) -> None:
    """Start the installer elevated (it asks for the administrator's permission) and return at once: the program
    that calls this must end right afterwards so the installer can replace its files."""
    program, params = installer_command(path)
    if sys.platform != "win32":
        raise UpdateError("the installer only runs on Windows")
    import ctypes
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", program, params, str(path.parent), 1)
    if int(result) <= 32:                                            # <= 32 is an error code (1223 = UAC declined)
        raise UpdateError("the installer was not started (permission refused?)")


# ── what the tray already announced ─────────────────────────────────────────

def _state_file(folder: Path | None = None) -> Path:
    return (folder or default_config_dir()) / "update_notified.json"


def notified(folder: Path | None = None) -> str:
    try:
        return str(json.loads(_state_file(folder).read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def mark_notified(version: str, folder: Path | None = None) -> None:
    path = _state_file(folder)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": version}), encoding="utf-8")
    except OSError as exc:
        log.debug("could not remember the announced version: %r", exc)


def should_announce(version: str, folder: Path | None = None) -> bool:
    """A new version is announced once; a later one is announced again."""
    seen = notified(folder)
    return bool(version) and (not seen or is_newer(version, seen))
