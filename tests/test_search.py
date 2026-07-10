"""Step-1.5 regression tests for the search pipeline.

These pin §4.5/§12.6: shape exists from day one, expansion is a swappable
no-op, query and index share a normalizer, results keep diacritics.
"""
from __future__ import annotations

import pytest

from catalogue.db_store import init_db
from catalogue.services.search import (
    Hit,
    SearchService,
    expand_noop,
    match_fts,
    normalize_query,
    rank_passthrough,
)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "search.db")
    conn.execute("INSERT INTO edition (id, title) VALUES (1, 'e')")
    conn.execute(
        "INSERT INTO edition_text (edition_id, page, content) VALUES (1, 7, ?)",
        ("Śāntideva wrote the Bodhicaryāvatāra; tathāgatagarbha is discussed.",),
    )
    conn.commit()
    yield conn
    conn.close()


# ── v1 expansion contract ──────────────────────────────────────────────────
def test_expansion_step_is_a_noop_passthrough():
    """v1 contract: expansion returns the input verbatim, single-element.
    Deleted siblings (`test_pipeline_has_four_named_replaceable_stages`,
    `test_expansion_is_swappable_without_touching_callers`) asserted on
    attribute names / injection mechanics — see tests/system/AUDIT.md.
    Behavior coverage now lives in tests/system/test_search.py.
    """
    assert expand_noop("santideva") == ["santideva"]
    assert expand_noop("") == [""]


# ── §4.5 — query and index share the normalizer ────────────────────────────
def test_search_finds_diacritic_text_from_bare_query(db):
    hits = SearchService().search(db, "santideva")
    assert len(hits) == 1
    assert hits[0].edition_id == 1
    # Snippet must keep diacritics (folding is index-only).
    assert "Śāntideva" in hits[0].snippet


def test_search_finds_diacritic_text_from_diacritic_query(db):
    hits = SearchService().search(db, "Śāntideva")
    assert len(hits) == 1


def test_normalizer_aligns_with_fts5_index_fold():
    """§4.5 worked example: `tathagatagarbha` matches `tathāgatagarbha`.
    That requires the SEARCH normalizer to do what FTS5's index fold does
    (NFKD-strip diacritics, lowercase) — and explicitly NOT the §4.2
    resolver fold, which would collapse `th→t` and break the match.

    See `search_normalize` docstring for the plan-consistency note.
    """
    from catalogue.db_store import fold_key
    # Search normalize keeps digraphs (matches FTS5 behavior).
    assert normalize_query("tathāgatagarbha") == "tathagatagarbha"
    assert normalize_query("Bodhicaryāvatāra") == "bodhicaryavatara"
    # Resolver fold collapses digraphs (§4.2 — different job).
    assert fold_key("Bodhicaryāvatāra") == "bodicaryavatara"
    # The two folds must NOT be the same function — conflation breaks §4.5.
    assert normalize_query("Bodhicaryāvatāra") != fold_key("Bodhicaryāvatāra")


# ── §4.5 — phonetic↔Wylie NOT folded (no accidental expansion) ─────────────
def test_phonetic_query_does_not_match_wylie_via_noop_expansion(tmp_path):
    """In v1 (no-op expansion) `jangchub` must not surface `byang chub`.
    That linkage is the deferred query-expansion feature, not folding."""
    db = init_db(tmp_path / "p.db")
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'e')")
    db.execute(
        "INSERT INTO edition_text (edition_id, page, content) VALUES (1, 1, ?)",
        ("the term byang chub means awakening",),
    )
    db.commit()
    assert SearchService().search(db, "jangchub") == []
    db.close()


# ── Empty-input safety ─────────────────────────────────────────────────────
def test_empty_query_yields_empty_results(db):
    # Normalizer collapses to ""; match_fts short-circuits.
    assert SearchService().search(db, "") == []
