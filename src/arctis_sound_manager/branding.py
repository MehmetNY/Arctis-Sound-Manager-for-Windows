# Copyright (C) 2026 loteran — Windows portability layer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Where this build lives: its repository, the page for donations, and the projects it stands on.

One place, so the About page, the sidebar, the bug reporter and the updater cannot disagree.
"""
from __future__ import annotations

from arctis_sound_manager.constants import IS_WINDOWS

#: The Windows port (this program when it runs on Windows).
REPO = "MehmetNY/Arctis-Sound-Manager-for-Windows"
REPO_URL = f"https://github.com/{REPO}"
ISSUES_URL = f"{REPO_URL}/issues"
RELEASES_URL = f"{REPO_URL}/releases"

#: Public donation page (BoomFi). It is only a link: no key or secret is needed - or kept - here.
DONATION_URL = "https://pay.boomfi.xyz/lite/3LDETXyLVlVUrjbQwydY5dPZGYY"

#: The projects this one is built on, in the order of the thanks.
UPSTREAM_ASM = "loteran/Arctis-Sound-Manager"
UPSTREAM_ASM_URL = f"https://github.com/{UPSTREAM_ASM}"
UPSTREAM_LAM = "elegos/Linux-Arctis-Manager"
UPSTREAM_LAM_URL = f"https://github.com/{UPSTREAM_LAM}/"


def issue_repo() -> str:
    """The repository bug reports go to: the Windows port's own on Windows (the Linux original's maintainer
    cannot help with it), the original's elsewhere."""
    return REPO if IS_WINDOWS else UPSTREAM_ASM
