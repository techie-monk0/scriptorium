"""Unit tests for catalogue.pagemap — the general folio→physical model.

These pin the GENERALITY requirements (no per-book tuning): interpolate undetected
folios, reject isolated OCR misreads, reject chapter-number false positives, and
survive a mid-book numbering reset."""
from __future__ import annotations

from catalogue.services.pagemap import PageMap, build_page_map, detect_folio, is_header_text


# ── folio detection off a running-header line ────────────────────────────────
def test_detect_folio_number_then_uppercase_header():
    assert detect_folio("123 LIFE OF TILOPA\nbody text") == 123


def test_detect_folio_number_then_titlecase_header():
    assert detect_folio("46 Mind Training\nmore") == 46
    assert detect_folio("How Atisa Relinquished His Kingdom 47\n…") == 47


def test_detect_folio_rejects_verse_line():
    # a number beside a SENTENCE (lowercase body) is a verse line, not a folio.
    assert detect_folio("307 If you do not see the nature of mind") is None


def test_detect_folio_in_footer():
    # Page number printed in the FOOTER, on its own line beside the running header
    # (book 78's layout) — both recto (number last) and verso (number first).
    recto = "body line one\nbody line two\nREFLECTIONS ON IMPERMANENCE\n27"
    assert detect_folio(recto) == 27
    verso = "body line one\nbody line two\n28\nTURNING THE MIND TO THE PATH"
    assert detect_folio(verso) == 28


def test_detect_folio_footer_letterspaced_digits():
    assert detect_folio("body\nTHE SPIRITUAL PATH\n2 8") == 28


def test_detect_folio_none_on_empty():
    assert detect_folio("") is None


def test_is_header_text():
    assert is_header_text("Mind Training")
    assert is_header_text("LIFE OF TILOPA")
    assert not is_header_text("if you do not see the nature")


# ── PageMap: interpolation, outlier/chapter rejection, resets ────────────────
def _anchors(pairs):
    return {f: i for f, i in pairs}


def test_estimate_interpolates_undetected_folio():
    pm = PageMap(_anchors([(10, 25), (11, 26), (12, 27), (13, 28), (20, 35)]))
    assert pm
    assert pm.estimate(15) == 30           # offset 15, slope 1
    assert pm.estimate(11) == 26


def test_rejects_isolated_ocr_misread():
    # a late page mis-read as 'folio 57' (huge offset) is not on the slope-1 chain.
    pm = PageMap(_anchors([(10, 25), (11, 26), (12, 27), (13, 28), (14, 29),
                           (57, 600)]))
    assert pm
    assert pm.estimate(12) == 27           # chain anchor, unaffected by the outlier
    assert pm.body_floor == 25             # first chain page, not the misread


def test_rejects_chapter_numbers():
    # chapter headings ('2 Reflections…' at p32, '3 From Seed…' at p44): ~12 pages
    # per +1 — NOT a folio sequence, so no slope-1 chain → map is not usable.
    pm = PageMap(_anchors([(2, 32), (3, 44), (4, 50), (5, 58), (6, 86)]))
    assert not pm


def test_survives_midbook_reset():
    # two slope-1 runs with a numbering reset between them: the longer run is kept,
    # so the bulk of the book still maps (a reset perturbs only its boundary).
    run_a = [(f, f + 15) for f in range(1, 8)]          # 7 anchors, offset 15
    run_b = [(f, f + 400) for f in range(1, 40)]        # 39 anchors, offset 400
    pm = PageMap(_anchors(run_a + run_b))
    assert pm
    assert pm.estimate(20) == 420          # estimated within the longer (B) run


def test_not_truthy_below_min_chain():
    assert not PageMap(_anchors([(1, 10), (5, 80)]))    # 2 inconsistent points


def test_build_page_map_from_page_texts():
    texts = (["front matter"] * 5
             + [f"{f} RUNNING HEADER\nbody" for f in range(1, 12)])
    pm = build_page_map(texts)
    assert pm
    # folio 1 detected at physical index 5 → offset 4
    assert pm.estimate(1) == 5
    assert pm.estimate(6) == 10
    assert pm.body_floor == 5
