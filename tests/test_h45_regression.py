"""Hermetic regression for holding 45 (Mind Training: The Great Collection).

Rather than load the 700-page PDF, this pins the engine against a saved fixture
(`tests/data/h45_peek.json`) holding the OCR'd Contents pages + the located first-
page ("peek") text of every work. We re-derive Sections exactly as the locator's
`_finish` does, so the full segmentation/author path runs deterministically with no
PDF, no DB, no LLM. It locks the behaviours built for this book:
  • degenerate page-label outline → printed Contents (`parse_contents_index`, 43 entries);
  • per-work authors from the Contents `Name (dates)` line, the first-page attribution
    (incl. OCR-garbled "Ailrib'uted to Atifa" → Atifa), and the "<Name>'s <title>"
    possessive gated on not-an-English-word (Atisa/Kusulu split, "The Peacock's" not);
  • no fragment/English-word "authors" leaking in.
"""
from __future__ import annotations

import json
from pathlib import Path

from catalogue.services.book_analysis import analyze_book_sections
from catalogue.services.locator import _finish
from catalogue.services.toc import parse_contents_index

_FIXTURE = json.loads(
    (Path(__file__).parent / "data" / "h45_peek.json").read_text(encoding="utf-8"))


def _works():
    secs = [_finish(s["title"], s["text"], toc_author=s["toc_author"],
                    source=s["source"]) for s in _FIXTURE["sections"]]
    return analyze_book_sections(secs, ladder=None)


def test_h45_contents_parses_to_43_entries():
    ents = parse_contents_index(_FIXTURE["contents"])
    assert len(ents) == 43
    assert sum(1 for e in ents if e.author) >= 15      # ~17 carry an author + dates


def test_h45_segments_to_multi_work():
    a = _works()
    assert a.structure == "multi_work"
    assert len(a.contained_texts) == 40
    assert sum(1 for w in a.contained_texts if w.authors) == 23


def test_h45_per_work_authors():
    by_sub = {}
    for w in _works().contained_texts:
        by_sub[w.title] = (w.authors[0] if w.authors else None)

    def author_of(substr):
        exact = [a for t, a in by_sub.items() if t.strip() == substr]
        if exact:
            return exact[0]
        hits = [a for t, a in by_sub.items() if substr in t]
        return hits[0] if len(hits) == 1 else ("AMBIGUOUS" if hits else "MISSING")

    # OCR-garbled first-page attribution ("Ailrib'uted to Atifa")
    assert author_of("Voyage to Sumatra") == "Atifa"
    # clean first-page attribution; the "Peacock's" possessive must NOT split
    assert author_of("Peacock's Neutralizing") == "Dharmarakfita"
    # possessive, diacritic-less names that aren't English words → split + author
    assert author_of("Seven-Point Mind Training") == "Atisa"      # was "Atisa's …"
    assert author_of("Accumulation Mind Training") == "Kusulu"     # was "Kusulu's …"
    assert author_of("Ear-Whispered Mind Training") == "Sumpa Lotsawa"
    # Contents "Name (dates)" author
    assert author_of("Bodhisattva's Jewel Garland").startswith("Ati")


def test_h45_no_fragment_or_english_word_authors():
    # guard against the re.I bug class: no author is a lowercase fragment or a bare
    # English word ("the great and", "Peacock", "Story").
    bad = {"the", "a", "an", "of", "and", "peacock", "poison", "story", "great"}
    for w in _works().contained_texts:
        for a in w.authors:
            head = a.split()[0].lower()
            assert head not in bad, f"fragment/English author leaked: {a!r} on {w.title!r}"
            assert a[:1].isupper(), f"author not capitalised: {a!r}"
