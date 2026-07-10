"""Step-2 regression tests for OCR quality scoring (§4.8c, §4.8d, §6)."""
from __future__ import annotations

import unicodedata

from catalogue.services.quality import REPLACEMENT_CHAR, score_text


# ── Sanity ────────────────────────────────────────────────────────────────
def test_empty_text_scores_zero():
    r = score_text("")
    assert r.score == 0.0
    assert r.char_count == 0


def test_clean_english_scores_high():
    text = (
        "The Bodhicaryāvatāra, the way of the bodhisattva, is a Mahāyāna "
        "Buddhist text composed by the eighth-century Indian master Śāntideva. "
        "It is widely studied and commented upon across traditions."
    ) * 4
    r = score_text(text)
    assert r.score >= 0.7, r


# ── §4.8c step 2: scoring runs on raw NFC text, NOT on folded text ────────
def test_diacritics_are_not_penalized():
    """The score must judge a diacriticked passage and its ASCII-folded
    equivalent similarly — folding is an INDEX-time operation, not a
    quality signal (§4.5 / §4.8c)."""
    diac = "Śāntideva discusses bodhicitta and the pāramitās." * 6
    flat = unicodedata.normalize(
        "NFKD",
        diac,
    )
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    s_diac = score_text(diac).score
    s_flat = score_text(flat).score
    # Within 0.1 of each other — diacritics aren't garbage.
    assert abs(s_diac - s_flat) < 0.1, (s_diac, s_flat)


# ── Garbage detection ─────────────────────────────────────────────────────
def test_replacement_chars_drop_the_score():
    garbage = (REPLACEMENT_CHAR + "x" + REPLACEMENT_CHAR + " ") * 200
    r = score_text(garbage)
    assert r.garbage_ratio > 0.1
    assert r.score < 0.5


def test_control_chars_count_as_garbage():
    text = "Some prose \x00\x01\x02 with control noise" * 20
    r = score_text(text)
    assert r.garbage_ratio > 0.0


# ── §4.8c step 2 — systematic-substitution sentinel (ś→'s) ────────────────
def test_systematic_apostrophe_substitution_is_flagged():
    """A Tesseract artifact where ś prints as `'s` shows up as apostrophes
    wedged inside otherwise-Latin words. Sentinel must count them."""
    text = (
        "the as'rama tradition and the ku's'a grass were studied "
        "and the ri's'i sages preserved them"
    ) * 5
    r = score_text(text)
    assert r.suspect_substitutions > 0


def test_english_contractions_are_no_longer_flagged():
    """Regression: `it's`, `don't`, `we're`, `you'll`, `she'd`, `they've`,
    `I'm` used to look like the Tesseract `ś→'s` artifact and trip the
    per-1000-char halving on every clean English book. The contraction
    tail set is now excluded so publisher-quality PDFs score correctly.
    """
    text = (
        "It's a fine day and they don't disagree, we're sure you'll see "
        "that she'd have come if I'm right and they've been here. "
    ) * 5
    r = score_text(text)
    assert r.suspect_substitutions == 0    # the whole point of the fix
    assert r.score >= 0.7                  # clean prose, no halving


def test_publisher_pdf_with_many_contractions_stays_clean():
    """Regression for the observed Tier-B false positives: ~1 contraction
    per 1000 chars in normal English prose used to drop the score to 0.50
    via the apostrophe-penalty halving. With the contraction set excluded,
    a 5000-char passage with realistic contraction density now lands well
    above the 0.6 threshold (= Tier C, clean)."""
    body = (
        "The teacher explained that it's important to remember what "
        "we've learned. Students don't always agree, but they're "
        "encouraged to ask. He'll remind them she'd already covered "
        "this. I'm certain we'll review it again. "
    )
    text = body * 12         # ~5 KB, realistic contraction density
    r = score_text(text)
    assert r.suspect_substitutions == 0
    assert r.score >= 0.7


def test_real_ocr_substitution_pattern_still_penalized():
    """The whole reason the apostrophe heuristic exists. `as'rama`-style
    artifacts (≥2 letters either side, tail NOT a contraction) must
    still trip the per-1000-char halving."""
    artifact_run = (
        "the as'rama tradition and the bo'di mind and the ku'sha grass "
        "were studied by the bra'hmana priests of old. " * 8
    )
    r = score_text(artifact_run)
    assert r.suspect_substitutions > 5     # many hits
    # The penalty halves the score; with this density it lands at/below
    # the 0.6 threshold = Tier B (ocr_poor, re-OCR queued).
    assert r.score <= 0.5
