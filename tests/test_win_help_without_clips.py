"""The Windows build has no Clips: its Help page must not mention it, and the Linux one must not change."""
import re

import pytest

from arctis_sound_manager.gui import help_page as H

CLIPS_WORD = re.compile(r"\bclips?\b", re.IGNORECASE)   # 'clipboard' is not Clips


@pytest.mark.parametrize("lang", sorted(H.HELP_CONTENT))
def test_no_help_text_mentions_clips_on_windows(lang):
    for heading, body in H.sections_for_platform(H.HELP_CONTENT[lang]["sections"], windows=True):
        assert not CLIPS_WORD.search(heading), (lang, heading)
        assert not CLIPS_WORD.search(body), (lang, heading, CLIPS_WORD.search(body).group(0))


@pytest.mark.parametrize("lang", sorted(H.HELP_CONTENT))
def test_the_linux_help_page_is_untouched(lang):
    sections = H.HELP_CONTENT[lang]["sections"]
    assert H.sections_for_platform(sections, windows=False) == [(s["heading"], s["body"]) for s in sections]


@pytest.mark.parametrize("lang", sorted(H.HELP_CONTENT))
def test_the_reworded_sentence_still_reads_as_a_list(lang):
    """Taking 'Clips' out of 'A, B, the router and Clips never talk to USB' must not leave 'A, B, the router never'."""
    sections = dict(H.sections_for_platform(H.HELP_CONTENT[lang]["sections"], windows=True))
    body = next(b for h, b in sections.items() if b.count("USB") and ("PipeWire" in b[:600]))
    first = body.split("\n")[0]
    assert re.search(r"(,| and| et| y) (the|le|el) (application router|routeur d'applications|enrutador de aplicaciones)", first), first
    assert ", the application router never" not in first
