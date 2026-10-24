#!/usr/bin/env python3
# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Watch the projects ASM depends on or competes with, and open one issue per
run listing what changed since the last one.

Half of ASM's bugs live in how it drives other software (PipeWire,
WirePlumber, libusb, the kernel's HID drivers), and a change there lands on
users before anyone here notices: the kernel's hid-steelseries-arctis driver
shipped in Linux 7.3 a week before it was read (#331). This makes the next
one show up as an issue the day it is published.

State lives in .github/upstream-watch-state.json: the last version or commit
seen per source. A source with no state is adopted silently, so adding one
does not open an issue about its whole history.

    upstream-watch.py              check, open an issue, write the state
    upstream-watch.py --dry-run    check and print the issue, change nothing

Needs GH_TOKEN for the GitHub API (rate limit) and `gh` to open the issue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(os.environ.get("ASM_ROOT", Path(__file__).resolve().parent.parent))
STATE_FILE = ROOT / ".github" / "upstream-watch-state.json"
REPO = "loteran/Arctis-Sound-Manager"
LABEL = "upstream-watch"

# How many commits one path query looks back. A path that moves more than
# this between two daily runs is reported as "and more".
_COMMIT_PAGE = 30
_ARCTIS = r"steel ?series|arctis"


@dataclass
class Source:
    key: str
    group: str
    why: str
    kind: str                  # gh_release, gh_commits, gitlab_tag, pypi
    target: str                # repo, gitlab project or PyPI package
    path: str | None = None    # gh_commits: only commits touching this path
    match: str | None = None   # gh_commits: only commits whose message matches
    extra: dict = field(default_factory=dict)


SOURCES = [
    # ── Kernel ─────────────────────────────────────────────────────────────
    Source("kernel-hid-arctis", "Kernel", "Binds the same HID interfaces ASM detaches (Arctis 1 X, 7 2018, 9, Nova 5X / 7)",
           "gh_commits", "torvalds/linux", path="drivers/hid/hid-steelseries-arctis.c"),
    Source("kernel-hid-steelseries", "Kernel", "The older SteelSeries HID driver the Arctis one was split from",
           "gh_commits", "torvalds/linux", path="drivers/hid/hid-steelseries.c"),
    Source("kernel-usb-audio", "Kernel", "USB audio quirks: mixer, sample rates, the sound card ASM routes to",
           "gh_commits", "torvalds/linux", path="sound/usb", match=_ARCTIS),
    Source("kernel-hid-ids", "Kernel", "New SteelSeries product ids claimed by a kernel driver",
           "gh_commits", "torvalds/linux", path="drivers/hid/hid-ids.h", match=_ARCTIS),
    Source("kernel-release", "Kernel", "Mainline release",
           "gh_tag", "torvalds/linux", extra={"pattern": r"^v\d+\.\d+$"}),

    # ── Audio stack ────────────────────────────────────────────────────────
    Source("pipewire", "Audio stack", "Filter-chain, pw-loopback, links, permissions",
           "gitlab_tag", "pipewire/pipewire", extra={"url": "https://gitlab.freedesktop.org/pipewire/pipewire/-/tags/{tag}"}),
    Source("wireplumber", "Audio stack", "Restore-target, software-dsp hiding (#325), link policy",
           "gitlab_tag", "pipewire/wireplumber", extra={"url": "https://gitlab.freedesktop.org/pipewire/wireplumber/-/tags/{tag}"}),

    # ── USB / Python dependencies ──────────────────────────────────────────
    Source("libusb", "USB and Python dependencies", "Claim / detach / transfers under pyusb",
           "gh_release", "libusb/libusb"),
    Source("pyusb", "USB and Python dependencies", "ASM's USB layer", "pypi", "pyusb"),
    Source("pyside6", "USB and Python dependencies", "The GUI; a Qt mismatch breaks it (Qt_6_PRIVATE_API)", "pypi", "PySide6"),
    Source("pulsectl", "USB and Python dependencies", "Volume, ChatMix, stream moves", "pypi", "pulsectl"),
    Source("dbus-next", "USB and Python dependencies", "Daemon ↔ GUI D-Bus", "pypi", "dbus-next"),
    Source("pyudev", "USB and Python dependencies", "USB hotplug", "pypi", "pyudev"),
    Source("distrobox", "USB and Python dependencies", "Bazzite / SteamOS install path",
           "gh_release", "89luca89/distrobox"),

    # ── Other Arctis tools on Linux ────────────────────────────────────────
    Source("headsetcontrol", "Other Arctis tools", "Reverse-engineered SteelSeries protocols; the kernel driver builds on it",
           "gh_commits", "Sapd/HeadsetControl", path="lib/devices", match=_ARCTIS),
    Source("headsetcontrol-release", "Other Arctis tools", "Release",
           "gh_release", "Sapd/HeadsetControl"),
    Source("linux-arctis-manager", "Other Arctis tools", "The project ASM was forked from",
           "gh_commits", "elegos/Linux-Arctis-Manager"),
    Source("steelvoicemix", "Other Arctis tools", "Nova Pro ChatMix / EQ / OLED in Rust",
           "gh_release", "Ibrahim-Aldhaheri/SteelVoiceMix"),
    Source("ggoled", "Other Arctis tools", "Base station OLED images and animations",
           "gh_commits", "JerwuQu/ggoled"),
]


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _get(url: str, github: bool = False):
    headers = {"User-Agent": "asm-upstream-watch"}
    if github:
        headers["Accept"] = "application/vnd.github+json"
        if token := os.environ.get("GH_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return json.load(r)


def _gh(path: str):
    return _get(f"https://api.github.com/{path}", github=True)


# ── Checks: each returns (new state, list of markdown lines) ──────────────────

def check_gh_release(src: Source, seen: str | None):
    try:
        rel = _gh(f"repos/{src.target}/releases/latest")
        tag, url = rel["tag_name"], rel["html_url"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        tag = _gh(f"repos/{src.target}/tags?per_page=1")[0]["name"]
        url = f"https://github.com/{src.target}/releases/tag/{tag}"
    if seen is None or tag == seen:
        return tag, []
    return tag, [f"[{tag}]({url}) (was {seen})"]


def check_gh_tag(src: Source, seen: str | None):
    pattern = re.compile(src.extra["pattern"])
    tags = [t["name"] for t in _gh(f"repos/{src.target}/tags?per_page=100")]
    tag = next((t for t in tags if pattern.match(t)), seen)
    if seen is None or tag == seen:
        return tag, []
    return tag, [f"[{tag}](https://github.com/{src.target}/releases/tag/{tag}) (was {seen})"]


def check_gh_commits(src: Source, seen: str | None):
    query = {"per_page": _COMMIT_PAGE}
    if src.path:
        query["path"] = src.path
    commits = _gh(f"repos/{src.target}/commits?{urllib.parse.urlencode(query)}")
    if not commits:
        return seen, []
    head = commits[0]["sha"]
    if seen is None or head == seen:
        return head, []
    lines, more = [], True
    for c in commits:
        if c["sha"] == seen:
            more = False
            break
        title = c["commit"]["message"].split("\n", 1)[0]
        if src.match and not re.search(src.match, c["commit"]["message"], re.I):
            continue
        day = c["commit"]["committer"]["date"][:10]
        lines.append(f"{day} [{title}]({c['html_url']})")
    if more and lines:
        lines.append(f"…and possibly more since `{seen[:12]}`")
    return head, lines


def check_gitlab_tag(src: Source, seen: str | None):
    project = urllib.parse.quote(src.target, safe="")
    tags = _get(f"https://gitlab.freedesktop.org/api/v4/projects/{project}/repository/tags"
                "?order_by=version&per_page=1")
    tag = tags[0]["name"] if tags else seen
    if seen is None or tag == seen:
        return tag, []
    return tag, [f"[{tag}]({src.extra['url'].format(tag=tag)}) (was {seen})"]


def check_pypi(src: Source, seen: str | None):
    version = _get(f"https://pypi.org/pypi/{src.target}/json")["info"]["version"]
    if seen is None or version == seen:
        return version, []
    return version, [f"[{version}](https://pypi.org/project/{src.target}/{version}/) (was {seen})"]


CHECKS = {
    "gh_release": check_gh_release,
    "gh_tag": check_gh_tag,
    "gh_commits": check_gh_commits,
    "gitlab_tag": check_gitlab_tag,
    "pypi": check_pypi,
}


# ── Report ────────────────────────────────────────────────────────────────────

def build_body(found: list[tuple[Source, list[str]]], errors: list[str]) -> str:
    out = ["Changes upstream since the last check. Read each against ASM: "
           "does it change what ASM relies on, break a workaround, or make one unnecessary?", ""]
    group = None
    for src, lines in found:
        if src.group != group:
            group = src.group
            out += [f"### {group}", ""]
        out.append(f"**{src.key}** — {src.why}")
        out += [f"- {line}" for line in lines]
        out.append("")
    if errors:
        out += ["### Checks that failed", ""] + [f"- {e}" for e in errors] + [""]
    out.append("_Opened by `scripts/upstream-watch.py`. Close once triaged._")
    return "\n".join(out)


def open_issue(title: str, body: str) -> None:
    subprocess.run(["gh", "label", "create", LABEL, "-R", REPO, "--color", "c5def5",
                    "--description", "Upstream change that may affect ASM", "--force"],
                   check=True, capture_output=True)
    subprocess.run(["gh", "issue", "create", "-R", REPO, "--title", title,
                    "--label", LABEL, "--body", body], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the issue, write nothing")
    args = parser.parse_args()

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    found, errors, adopted = [], [], []
    for src in SOURCES:
        seen = state.get(src.key)
        try:
            new, lines = CHECKS[src.kind](src, seen)
        except Exception as e:  # one dead source must not hide the others
            errors.append(f"{src.key}: {type(e).__name__}: {e}")
            continue
        if seen is None:
            adopted.append(src.key)
        if lines:
            found.append((src, lines))
        if new is not None:
            state[src.key] = new

    if adopted:
        print(f"adopted silently: {', '.join(adopted)}")
    if not found and not errors:
        print("no upstream change")
    else:
        title = f"Upstream watch {date.today().isoformat()}: " + ", ".join(s.key for s, _ in found) \
            if found else f"Upstream watch {date.today().isoformat()}: checks failing"
        body = build_body(found, errors)
        if args.dry_run:
            print(f"--- {title}\n{body}")
        elif found or len(errors) == len(SOURCES):
            # A lone failed check is retried tomorrow; only open an issue for
            # it when nothing else works (network, token).
            open_issue(title, body)
        else:
            print("\n".join(errors), file=sys.stderr)

    if not args.dry_run:
        STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
