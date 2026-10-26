"""The About page, the donation links and where bug reports go."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from arctis_sound_manager import branding
from arctis_sound_manager.gui import about_page as ap


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _all_text(content: dict) -> str:
    parts = [content["title"], content["subtitle"]]
    for section in content["sections"]:
        parts += [section["heading"], section["body"]] + [b[0] + " " + b[1] for b in section.get("buttons", ())]
    return "\n".join(parts)


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_the_page_says_what_was_asked_in_both_languages(lang):
    text = _all_text(ap.ABOUT_CONTENT[lang])
    assert branding.REPO_URL in text                                        # his repository
    assert "https://github.com/MehmetNY/Arctis-Sound-Manager-for-Windows" in text
    assert "https://github.com/elegos/Linux-Arctis-Manager/" in text        # the two workhorses
    assert "https://github.com/loteran/Arctis-Sound-Manager" in text
    assert "GPL" in text and "fork" in text.lower()
    assert branding.DONATION_URL in text


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_the_donation_button_closes_the_page_right_under_its_explanation(lang):
    sections = ap.ABOUT_CONTENT[lang]["sections"]
    last = sections[-1]
    assert last["buttons"] == [(last["buttons"][0][0], branding.DONATION_URL, "donate")]
    assert [s for s in sections if any(b[2] == "donate" for b in s.get("buttons", ()))] == [last]


def test_both_languages_have_the_same_structure():
    en, tr = ap.ABOUT_CONTENT["en"], ap.ABOUT_CONTENT["tr"]
    assert len(en["sections"]) == len(tr["sections"])
    for a, b in zip(en["sections"], tr["sections"]):
        assert [x[1:] for x in a.get("buttons", ())] == [x[1:] for x in b.get("buttons", ())]   # same targets


def test_no_secret_is_in_the_program():
    """Only the public donation page is in the program: no account key of the payment service (they look like
    ``<name>-<20 random characters>``) and nothing but the public ``/lite/`` link on that service's domain."""
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    key_shape = re.compile(r"\b[a-z]{3,12}-[A-Za-z0-9]{20,}\b")
    texts = {p: p.read_text(encoding="utf-8", errors="ignore") for p in src.rglob("*")
             if p.is_file() and p.suffix in {".py", ".ini", ".json", ".yaml", ".svg"}}
    suspicious = [f"{p.name}: {m}" for p, t in texts.items() for m in key_shape.findall(t)
                  if not m.startswith(("arctis-", "asm-", "steelseries-"))]
    assert suspicious == []
    boomfi_urls = {m for t in texts.values() for m in re.findall(r"https?://[^\s\"')]*boomfi[^\s\"')]*", t)}
    assert boomfi_urls and all("/lite/" in url for url in boomfi_urls), boomfi_urls


def test_an_unknown_language_falls_back_to_english():
    assert ap.about_content("xx") is ap.ABOUT_CONTENT["en"]


def test_linkify_escapes_and_links():
    out = ap.linkify("a <b> & https://x.example/y?z=1\nnext", "#fff")
    assert "&lt;b&gt;" in out and "&amp;" in out and "<br>" in out
    assert '<a href="https://x.example/y?z=1"' in out


def test_the_page_builds_in_each_language_and_its_buttons_open_the_right_addresses(qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(ap, "open_url", opened.append)
    page = ap.AboutPage()
    for lang in ("en", "tr"):
        page._set_language(lang)
        buttons = page.findChildren(QPushButton)
        assert buttons, lang
        donate = [b for b in buttons if b.toolTip() == branding.DONATION_URL]
        assert len(donate) == 1
        donate[0].click()
        assert opened[-1] == branding.DONATION_URL
        titles = [label.text() for label in page.findChildren(QLabel)]
        assert ap.ABOUT_CONTENT[lang]["sections"][-1]["heading"] in titles


def test_the_donate_button_is_below_the_text_in_the_last_card(qapp):
    page = ap.AboutPage()
    page.resize(900, 1200)
    page.show()
    qapp.processEvents()
    donate = next(b for b in page.findChildren(QPushButton) if b.toolTip() == branding.DONATION_URL)
    intro = next(label for label in page.findChildren(QLabel) if "buy me a coffee" in label.text().lower()
                 and "donate any amount" in label.text())
    assert donate.mapTo(page, donate.rect().topLeft()).y() > intro.mapTo(page, intro.rect().bottomLeft()).y() - 1


# ── bug reports go to this repository on Windows ────────────────────────────

def test_bug_reports_go_to_the_windows_port_on_windows_and_to_the_original_elsewhere(monkeypatch):
    monkeypatch.setattr(branding, "IS_WINDOWS", True)
    assert branding.issue_repo() == "MehmetNY/Arctis-Sound-Manager-for-Windows"
    monkeypatch.setattr(branding, "IS_WINDOWS", False)
    assert branding.issue_repo() == "loteran/Arctis-Sound-Manager"


def test_the_gh_cli_submission_defaults_to_that_repository(monkeypatch, tmp_path):
    from arctis_sound_manager import bug_reporter
    monkeypatch.setattr(bug_reporter, "issue_repo", lambda: "MehmetNY/Arctis-Sound-Manager-for-Windows")
    calls = []

    class Done:
        def __init__(self, out):
            self.stdout = out

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Done("https://gist.github.com/x/1\n" if cmd[1] == "gist" else
                    "https://github.com/MehmetNY/Arctis-Sound-Manager-for-Windows/issues/7\n")
    monkeypatch.setattr(bug_reporter.subprocess, "run", run)
    report = tmp_path / "r.md"
    report.write_text("x")
    url = bug_reporter.submit_via_gh_cli("t", "b", report)
    assert url.endswith("/issues/7")
    issue_cmd = next(c for c in calls if c[1] == "issue")
    assert issue_cmd[issue_cmd.index("--repo") + 1] == "MehmetNY/Arctis-Sound-Manager-for-Windows"
