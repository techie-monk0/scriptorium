"""Step-4 regression tests — LLM client / ladder / budget, resolver stub,
TOC extraction + validation, orchestrator.

Pins the v8 invariants the pipeline must not regress: cache before LLM
call, escalate only on low confidence, unavailable rung skipped not
fatal, $20 cap applies only to api_key billing path, structured-outline
path validated before use, resolver-stub interface stable.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from catalogue.services.classify import (
    Rung, _content_hash, classify_entry,
)
from catalogue.db_store import init_db
from catalogue.services.llm import BudgetExceeded, BudgetTracker, LLMClient
from catalogue.services.process import ProcessConfig, process_holding
from catalogue.services.work_canonical_resolver import ResolverStub
from catalogue.services.toc import (
    TOCEntry, extract_epub_outline, extract_structured_outline,
    is_degenerate_outline, parse_contents_index, validate_toc,
)


# ── LLM client / transport ───────────────────────────────────────────────
def _fake_transport(content: str, tokens_in: int = 10, tokens_out: int = 5):
    """Return a callable that mimics an OpenAI-compatible /chat/completions
    response. Tests don't touch the network."""
    def _t(url, body, timeout):
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": tokens_in,
                      "completion_tokens": tokens_out},
        }
    return _t


def test_llm_client_posts_openai_compatible_body():
    captured = {}

    def t(url, body, timeout):
        captured["url"] = url
        captured["body"] = body
        return {"choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    c = LLMClient(model="qwen3:8b",
                  base_url="http://localhost:11434/v1",
                  transport=t)
    c.chat([{"role": "user", "content": "hi"}])

    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["body"]["model"] == "qwen3:8b"
    assert captured["body"]["temperature"] == 0.0     # deterministic for cache
    assert captured["body"]["response_format"]["type"] == "json_object"


# ── §4.9 budget — $20 cap applies ONLY to api_key billing path ──────────
def test_local_calls_cost_nothing_and_never_trip_the_cap():
    b = BudgetTracker(cap_usd=20.0, billing_path="api_key")
    c = LLMClient(model="qwen3:8b", budget=b,
                  transport=_fake_transport("{}", 100_000, 100_000))
    for _ in range(50):
        c.chat([])
    assert b.spent_usd == 0.0  # local rungs are free


def test_api_key_path_enforces_cap_haiku():
    b = BudgetTracker(cap_usd=0.01, billing_path="api_key")
    c = LLMClient(model="claude-haiku-4-5-20251001", budget=b,
                  transport=_fake_transport("{}", 10_000, 10_000))
    with pytest.raises(BudgetExceeded):
        c.chat([])
    assert b.spent_usd == 0.0   # nothing recorded after the raise


def test_local_billing_path_does_not_enforce_cap_even_for_haiku():
    """§4.9: cap is a guard for the raw-API-key path; the Max-5x
    programmatic credit and local Ollama are tracked elsewhere."""
    b = BudgetTracker(cap_usd=0.00, billing_path="local")
    c = LLMClient(model="claude-haiku-4-5-20251001", budget=b,
                  transport=_fake_transport("{}", 10_000, 10_000))
    c.chat([])   # must not raise


# ── §4.9 escalation ladder ──────────────────────────────────────────────
@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "s4.db")
    yield conn
    conn.close()


def _ladder(*responses, available_for_top=True):
    """Build a 3-rung ladder where each rung emits the given JSON content.
    Use `responses[i] = None` to make rung i unavailable."""
    rungs = []
    for i, (name, content) in enumerate(zip(
        ("qwen3:8b", "qwen3:14b", "claude-haiku-4-5-20251001"), responses
    )):
        if content is None:
            client = LLMClient(model=name, transport=_fake_transport("{}"))
            avail = (lambda: False)
        else:
            client = LLMClient(
                model=name, transport=_fake_transport(content)
            )
            avail = (lambda i=i: True)
        # Top rung gated separately for the API-key test:
        if i == 2 and not available_for_top:
            avail = (lambda: False)
        rungs.append(Rung(name=name, client=client, available=avail))
    return rungs


def test_first_rung_confident_returns_without_climbing(db):
    ladder = _ladder(
        '{"kind":"root","confidence":0.95,"reasoning":""}',
        '{"kind":"commentary","confidence":0.95,"reasoning":""}',
        '{"kind":"other","confidence":0.95,"reasoning":""}',
    )
    r = classify_entry(db, "Bodhicaryāvatāra", ladder=ladder)
    assert r.kind == "root"
    assert r.rung == "qwen3:8b"
    assert r.cached is False


def test_low_confidence_escalates_to_next_rung(db):
    ladder = _ladder(
        '{"kind":"root","confidence":0.30,"reasoning":"unsure"}',
        '{"kind":"commentary","confidence":0.92,"reasoning":""}',
        '{"kind":"other","confidence":0.95,"reasoning":""}',
    )
    r = classify_entry(db, "A Subtle Title", ladder=ladder)
    assert r.rung == "qwen3:14b"
    assert r.kind == "commentary"


def test_haiku_skipped_when_unavailable(db):
    """No ANTHROPIC_API_KEY → Haiku rung skipped. The classifier returns
    the last local rung's answer (whatever its confidence), it does NOT
    raise."""
    ladder = _ladder(
        '{"kind":"root","confidence":0.30,"reasoning":""}',
        '{"kind":"commentary","confidence":0.40,"reasoning":""}',
        '{"kind":"other","confidence":0.99,"reasoning":""}',
        available_for_top=False,
    )
    r = classify_entry(db, "An Ambiguous Heading", ladder=ladder)
    assert r.rung == "qwen3:14b"
    assert r.kind == "commentary"
    assert r.confidence == 0.40


def test_cache_hit_short_circuits_the_whole_ladder(db):
    """§4.9: settled entries never re-climb the ladder and never re-bill."""
    # Pre-seed the cache as if 8B had answered.
    ch = _content_hash("Title", None)
    db.execute(
        "INSERT INTO classification_cache "
        "(content_hash, classify_version, result_json, confidence, model_rung) "
        "VALUES (?, 1, ?, 0.9, 'qwen3:8b')",
        (ch, json.dumps({"kind": "root"})),
    )
    db.commit()

    # Build a ladder whose every rung would *crash* if called.
    def boom_transport(url, body, timeout):
        raise AssertionError("LLM must not be called on cache hit")
    rungs = [
        Rung(name="qwen3:8b",
             client=LLMClient(model="qwen3:8b", transport=boom_transport)),
        Rung(name="qwen3:14b",
             client=LLMClient(model="qwen3:14b", transport=boom_transport)),
    ]
    r = classify_entry(db, "Title", ladder=rungs)
    assert r.cached is True
    assert r.kind == "root"


def test_version_bump_invalidates_cache(db):
    ch = _content_hash("Title", None)
    db.execute(
        "INSERT INTO classification_cache "
        "(content_hash, classify_version, result_json, confidence, model_rung) "
        "VALUES (?, 1, ?, 0.9, 'qwen3:8b')",
        (ch, json.dumps({"kind": "root"})),
    )
    db.commit()
    ladder = _ladder('{"kind":"commentary","confidence":0.9}', None, None)
    r = classify_entry(db, "Title", classify_version=2, ladder=ladder)
    assert r.cached is False
    assert r.kind == "commentary"
    # Old cache row (version 1) still present alongside new one (version 2).
    rows = db.execute(
        "SELECT classify_version, model_rung FROM classification_cache "
        "WHERE content_hash = ? ORDER BY classify_version", (ch,)
    ).fetchall()
    assert rows == [(1, "qwen3:8b"), (2, "qwen3:8b")]


def test_unparseable_llm_response_gets_low_confidence_not_a_crash(db):
    ladder = _ladder("not even close to json", None, None)
    r = classify_entry(db, "Title", ladder=ladder)
    assert r.confidence == 0.0


# ── Resolver stub ────────────────────────────────────────────────────────
def test_resolver_returns_none_and_caches_the_miss(db):
    r = ResolverStub()
    assert r.resolve_work(db, "Bodhicaryāvatāra") is None
    # Second call must not re-INSERT; idempotency via INSERT OR REPLACE.
    assert r.resolve_work(db, "Bodhicaryāvatāra") is None
    (n,) = db.execute(
        "SELECT count(*) FROM resolver_cache "
        "WHERE source = 'stub'"
    ).fetchone()
    assert n == 1


def test_resolver_caches_per_version(db):
    r = ResolverStub()
    r.resolve_work(db, "X")
    r.version = 2          # simulate a future live resolver bump
    r.resolve_work(db, "X")
    (n,) = db.execute("SELECT count(*) FROM resolver_cache").fetchone()
    assert n == 2          # two rows, one per version


# ── TOC outline extraction ───────────────────────────────────────────────
def _make_epub(path: Path, bodies: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        for i, body in enumerate(bodies):
            z.writestr(f"OEBPS/ch{i}.xhtml",
                       f"<html><body>{body}</body></html>")


def test_epub_outline_pulls_headings(tmp_path):
    p = tmp_path / "x.epub"
    _make_epub(p, [
        "<h1>Bodhicaryāvatāra</h1><p>text</p>",
        "<h1>Commentary by Patrul</h1><h2>Ch 1</h2>",
    ])
    entries = extract_epub_outline(p)
    assert entries is not None
    titles = [e.title for e in entries]
    assert "Bodhicaryāvatāra" in titles
    assert any("Commentary" in t for t in titles)


def test_outline_for_unknown_suffix_is_none(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("noop")
    assert extract_structured_outline(p) is None


# ── Validation (§6) ──────────────────────────────────────────────────────
def test_validator_tolerates_front_matter_page_reset():
    # [M8] roman prelims parsed to arabic (7, 9, 17) then the body restarts at 1
    # — one backward step is normal, NOT scrambled OCR. Also tolerate a couple of
    # multi-part resets.
    entries = [TOCEntry("Preface", 7), TOCEntry("Intro", 9), TOCEntry("Ch1", 1),
               TOCEntry("Ch2", 13), TOCEntry("Ch3", 23), TOCEntry("Ch4", 39)]
    rep = validate_toc(entries)
    assert "non_monotonic_pages" not in rep.issues


def test_validator_flags_pervasively_scrambled_pages():
    # [M8] genuinely scrambled OCR: a large fraction of transitions go backward.
    entries = [TOCEntry(c, p) for c, p in
               zip("abcdefghij", [1, 50, 2, 80, 3, 90, 4, 70, 5, 60])]
    rep = validate_toc(entries)
    assert "non_monotonic_pages" in rep.issues
    assert rep.ok is False


def test_validator_rejects_implausible_count():
    rep = validate_toc([TOCEntry("a", 1)])         # only 1 entry
    assert "entry_count_too_low (1 < 3)" in rep.issues


def test_validator_flags_toc_outside_document():
    entries = [TOCEntry("a", 1), TOCEntry("b", 50), TOCEntry("c", 500)]
    rep = validate_toc(entries, doc_page_count=100)
    assert "toc_page_beyond_document_end" in rep.issues


def test_validator_passes_clean_toc():
    # 10 chapters spaced across a 200-page book — realistic.
    entries = [TOCEntry(f"Chapter {i}", i * 20)
               for i in range(1, 11)]
    rep = validate_toc(entries, doc_page_count=210)
    assert rep.ok is True, rep.issues


# ── degenerate (page-label-only) outline detection ───────────────────────
def test_degenerate_outline_page_label_bookmarks():
    # book 45 Mind Training: 719 'page0001'…'page0719' auto-bookmarks, no work signal.
    assert is_degenerate_outline([f"page{i:04d}" for i in range(1, 720)]) is True


def test_degenerate_outline_accepts_label_variants():
    assert is_degenerate_outline(["p. 1", "p. 2", "folio 3", "4", "Sheet 5", "img12"])


def test_real_outline_is_not_degenerate():
    assert is_degenerate_outline(
        ["Introduction", "The Generation Stage", "Ritual Texts",
         "Glossary", "Index", "Notes"]) is False


def test_degenerate_outline_needs_high_label_fraction():
    # one stray page-label among real titles must NOT condemn the outline.
    assert is_degenerate_outline(
        ["Real Title One", "Real Title Two", "page0003",
         "Another Real Title", "Yet Another", "Sixth Title"]) is False


def test_degenerate_outline_too_few_to_judge():
    assert is_degenerate_outline(["page1", "page2"]) is False


def test_degenerate_outline_per_page_density():
    # book 22: ~one (distinct-titled) bookmark per page → a page-level dump, junk.
    titles = [f"Heading number {i}" for i in range(180)]   # distinct, NOT page-labels
    assert is_degenerate_outline(titles, page_count=188) is True   # 180 >= 0.5*188
    assert is_degenerate_outline(titles, page_count=400) is False  # 180 < 0.5*400
    assert is_degenerate_outline(titles) is False                  # no page_count → not junk


# ── deterministic numbered-Contents parser (2a) ──────────────────────────────
_CONTENTS = """\
Some front-matter prose that should be ignored entirely.
Mind Training: The Great Collection
1. Bodhisattva's Jewel Garland
Atisa Dipamkara (982-1054)
21
2. How Atisa Relinquished His Kingdom
and Sought Liberation
Dromtonpa (1005-64)
27
3. The Story of Atisa's Voyage to Sumatra
57
15.
An Instruction on Purifying Negative Karma
203
Notes
577
"""


def test_parse_contents_index_titles_authors_pages():
    ents = parse_contents_index(_CONTENTS)
    by_title = {e.title: e for e in ents}
    # wrapped title stitched; author + folio captured
    assert "How Atisa Relinquished His Kingdom and Sought Liberation" in by_title
    e2 = by_title["How Atisa Relinquished His Kingdom and Sought Liberation"]
    assert e2.author == "Dromtonpa" and e2.page == 27
    # authorless entry keeps title + page, author None
    assert by_title["The Story of Atisa's Voyage to Sumatra"].author is None
    # number-on-its-own-line, title on the next line, is recovered
    assert "An Instruction on Purifying Negative Karma" in by_title
    # structural keyword 'Notes' ends the list (not parsed as an entry)
    assert all("Notes" != e.title for e in ents)


def test_parse_contents_index_ignores_prose_without_numbered_list():
    assert parse_contents_index("Just a paragraph of prose.\nNo numbered index here.") == []


# ── Orchestrator (end-to-end) ────────────────────────────────────────────
def _seed_holding(db, *, file_path: str, file_hash="h", text_status="ocr_good"):
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'Sample Edition')")
    db.execute(
        "INSERT INTO holding (id, edition_id, form, file_path, file_hash, text_status) "
        "VALUES (1, 1, 'electronic', ?, ?, ?)",
        (file_path, file_hash, text_status),
    )
    db.commit()


def test_process_full_cascade_caches_and_classifies(db, tmp_path):
    epub = tmp_path / "book.epub"
    _make_epub(epub, [
        "<h1>The Way of the Bodhisattva</h1>",
        "<h1>Translator's Introduction</h1>",
        "<h1>Commentary by Patrul Rinpoche</h1>",
        "<h1>Appendix A</h1>",
    ])
    _seed_holding(db, file_path=str(epub))

    ladder = _ladder(
        '{"kind":"root","confidence":0.9}', None, None,
    )
    rep = process_holding(db, 1, ProcessConfig(ladder=ladder))

    assert rep.extracted_entries == 4
    assert rep.cached_toc is False
    assert rep.queued_for_digitization is False
    assert len(rep.classifications) == 4
    # parsed_toc_cache populated keyed by (file_hash, parse_version).
    (n_toc,) = db.execute(
        "SELECT count(*) FROM parsed_toc_cache WHERE file_hash='h' AND parse_version=1"
    ).fetchone()
    assert n_toc == 1


def test_reprocess_uses_caches_no_llm_calls(db, tmp_path):
    epub = tmp_path / "book.epub"
    _make_epub(epub, ["<h1>A</h1>", "<h1>B</h1>", "<h1>C</h1>"])
    _seed_holding(db, file_path=str(epub))

    # First run with a working transport.
    ladder = _ladder('{"kind":"root","confidence":0.9}', None, None)
    process_holding(db, 1, ProcessConfig(ladder=ladder))

    # Second run: any LLM call would CRASH — caches must serve everything.
    def boom_transport(url, body, timeout):
        raise AssertionError("LLM must not be called on a cached re-run")
    boom_ladder = [
        Rung(name="qwen3:8b",
             client=LLMClient(model="qwen3:8b", transport=boom_transport)),
    ]
    rep2 = process_holding(db, 1, ProcessConfig(ladder=boom_ladder))
    assert rep2.cached_toc is True
    assert all(c.cached for c in rep2.classifications)


def test_image_only_holdings_are_queued_not_extracted(db, tmp_path):
    """§4.7 step 3: no/unreadable text layer → queue for digitization."""
    _seed_holding(db, file_path=str(tmp_path / "scan.pdf"),
                  text_status="image_only")
    rep = process_holding(db, 1, ProcessConfig())
    assert rep.queued_for_digitization is True
    assert rep.extracted_entries == 0
    # Queue item recorded with the reason.
    (item_type, payload) = db.execute(
        "SELECT item_type, payload_json FROM review_queue"
    ).fetchone()
    assert item_type == "low_confidence_extraction"
    assert "image_only" in payload


def test_no_structured_outline_falls_through_to_vision_then_queues(db, tmp_path):
    """If outline extraction returns nothing AND vision-LLM is the v1 stub
    (returns None), the file is queued. The cascade order is honored."""
    epub = tmp_path / "bookless.epub"
    _make_epub(epub, ["<p>just prose, no headings</p>"])
    _seed_holding(db, file_path=str(epub))

    vision_called = {"n": 0}

    def fake_vision(_p):
        vision_called["n"] += 1
        return None

    rep = process_holding(db, 1, ProcessConfig(vision_toc=fake_vision))
    assert vision_called["n"] == 1
    assert rep.queued_for_digitization is True


def test_low_confidence_classification_enqueues_review_item(db, tmp_path):
    epub = tmp_path / "x.epub"
    _make_epub(epub, ["<h1>A</h1>", "<h1>B</h1>", "<h1>C</h1>"])
    _seed_holding(db, file_path=str(epub))

    ladder = _ladder(
        '{"kind":"other","confidence":0.20}',
        '{"kind":"other","confidence":0.30}',
        '{"kind":"other","confidence":0.40}',
        available_for_top=False,
    )
    rep = process_holding(db, 1, ProcessConfig(ladder=ladder))

    (n_class,) = db.execute(
        "SELECT count(*) FROM review_queue WHERE item_type='toc_classification'"
    ).fetchone()
    assert n_class == len(rep.classifications) == 3


def test_invalid_outline_queues_extraction_note_but_still_classifies(db, tmp_path):
    """A bookmark tree that fails §6 validation must surface an advisory
    `extraction_note` (the book still processes, so it's a heads-up not a real
    failure) — but the classifier should still see every entry, so a wrong outline
    doesn't silently swallow the whole file."""
    epub = tmp_path / "x.epub"
    _make_epub(epub, ["<h1>A</h1>"])     # 1 entry → fails min_entries=3
    _seed_holding(db, file_path=str(epub))
    ladder = _ladder('{"kind":"root","confidence":0.9}', None, None)
    rep = process_holding(db, 1, ProcessConfig(ladder=ladder))
    assert rep.queued_low_confidence is True
    assert len(rep.classifications) == 1

    types = [r[0] for r in db.execute(
        "SELECT item_type FROM review_queue"
    ).fetchall()]
    assert "extraction_note" in types
    assert "low_confidence_extraction" not in types   # advisory, not a real failure
