"""v14 regression tests — book-aware Step-4 classification.

Pins: text-layer TOC locator/fragment/translator heuristics (pure), the
TOC-region parse (object-wrapping json_object fix + lenient parse), and the
process_holding integration (text-layer TOC recovery + book_toc_pattern
review proposal). No Ollama — a routing fake transport stands in for the LLM.

The book-level structure/metadata extraction this file once covered was
superseded by the section-based path — see tests/test_step4_v15.py.
"""
from __future__ import annotations

import json

import pytest

from catalogue.services.classify import Rung, parse_toc_region, _is_front_back
from catalogue.db_store import init_db
from catalogue.services.llm import LLMClient
from catalogue.services.process import ProcessConfig, process_holding
from catalogue.services.toc import (
    TOCEntry, locate_toc_region, is_toc_fragment, has_translator,
)


# ── fake LLM ─────────────────────────────────────────────────────────────
def _transport_returning(content: str):
    def _t(url, body, timeout):
        return {"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    return _t


def _ladder_returning(content: str):
    return [Rung("fake", LLMClient(model="fake", transport=_transport_returning(content)))]


def _router_ladder(*, entry='{"kind":"other","confidence":0.9}',
                    toc='{"entries":[{"title":"Preface","page":7},{"title":"Chapter 1","page":12}]}'):
    """One rung whose response depends on which system prompt it sees, so a
    single ladder serves both the per-entry classify and the TOC-region parse."""
    def _t(url, body, timeout):
        sys = body["messages"][0]["content"]
        content = toc if "Table-of-Contents region" in sys else entry
        return {"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    return [Rung("fake", LLMClient(model="fake", transport=_t))]


# ── pure helpers ─────────────────────────────────────────────────────────
def test_locate_toc_region_handles_ocr_spaced_heading():
    text = "cover\npublisher junk\nCo n ten ts\nPreface 7\nChapter 1 12\nIndex 200\n"
    region = locate_toc_region(text)
    assert region is not None and "Preface 7" in region


def test_locate_toc_region_none_when_no_toc():
    assert locate_toc_region("just prose with no table of contents here. " * 50) is None


def test_is_toc_fragment():
    assert is_toc_fragment("tiny", "x")                        # < 5 KB
    assert is_toc_fragment("x" * 6000, "LTK — Back Matter")    # back-matter file
    assert not is_toc_fragment("x" * 6000, "A Normal Book")


def test_has_translator_precise():
    assert has_translator("Translated by Thupten Jinpa")
    assert has_translator("Translations by Sangye Khandro and B. Alan Wallace")
    assert has_translator("Dharmachakra Translation Committee")
    assert has_translator("Analyzed, translated, and edited by Jeffrey Hopkins")
    # a modern study that merely discusses translation must NOT trip it:
    assert not has_translator("A Biography. By Donald Lopez. A study of the sutra.")


def test_is_front_back_filters_apparatus_not_texts():
    assert _is_front_back("Index") and _is_front_back("Other Titles")
    assert _is_front_back("Acknowledgments")
    assert not _is_front_back("Part 1 The Root Text")


# ── parse_toc_region (the json_object → object-wrapping fix) ──────────────
def test_parse_toc_region_object_with_entries():
    ents = parse_toc_region("Contents\nPreface 7\nChapter 1 12",
                            ladder=_ladder_returning(
                                '{"entries":[{"title":"Preface","page":7},'
                                '{"title":"Chapter 1","page":12}]}'))
    assert [e.title for e in ents] == ["Preface", "Chapter 1"]
    assert ents[0].page == 7 and isinstance(ents[0], TOCEntry)


def test_parse_toc_region_lenient_loose_objects():
    # gemma sometimes emits comma-separated objects without array brackets.
    ents = parse_toc_region("…", ladder=_ladder_returning(
        '{"title":"A","page":1},\n{"title":"B","page":2}'))
    assert [e.title for e in ents] == ["A", "B"]


# ── process_holding integration ──────────────────────────────────────────
@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "v14.db")
    yield conn
    conn.close()


def _seed(db, *, file_path, file_hash="h", text_status="ocr_good", raw_text=None):
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'Sample Edition')")
    db.execute(
        "INSERT INTO holding (id, edition_id, form, file_path, file_hash, text_status) "
        "VALUES (1, 1, 'electronic', ?, ?, ?)", (file_path, file_hash, text_status))
    if raw_text is not None:
        db.execute("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
                   "VALUES (?, 1, ?)", (file_hash, raw_text))
    db.commit()


def test_text_layer_toc_recovers_when_no_outline(db, tmp_path):
    # .txt suffix → extract_structured_outline returns None; recovery must come
    # from the printed Contents page in raw_extract_cache.
    raw = ("Cover page. " * 40 + "\nCo n ten ts\nPreface 7\nChapter 1 12\n"
           + "body text. " * 600)               # > 5 KB so not a fragment
    _seed(db, file_path=str(tmp_path / "scan.txt"), raw_text=raw)
    rep = process_holding(db, 1, ProcessConfig(
        ladder=_router_ladder(), use_text_layer_toc=True, analyze_book=False))
    assert rep.extracted_entries == 2          # recovered "Preface", "Chapter 1"
    assert rep.queued_for_digitization is False


def test_analyze_book_queues_structure_proposal(db, tmp_path):
    epub = tmp_path / "b.epub"
    import zipfile
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("a.xhtml", "<h1>Chapter 1</h1><h1>Chapter 2</h1><h1>Chapter 3</h1>")
    _seed(db, file_path=str(epub), raw_text="Title. A modern study. " * 400)
    rep = process_holding(db, 1, ProcessConfig(
        ladder=_router_ladder(), analyze_book=True))
    # container model: no reproduced-text anchors → the whole book is one work.
    assert rep.book_structure == "single_work"
    rows = db.execute(
        "SELECT payload_json FROM review_queue WHERE item_type='book_toc_pattern'"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["structure"] == "single_work"
    assert payload["holding_id"] == 1
    assert len(payload["works"]) == 1 and payload["works"][0]["whole_book"] is True
