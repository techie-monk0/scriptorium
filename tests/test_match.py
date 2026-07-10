"""Unit tests for match/dedup logic (§7.5).

Edge cases for the merge primitive. Behavior-level coverage of the full
match → tier flow is in tests/system/test_needs_work.py.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import init_db
from catalogue.services.match import (
    find_isbn_duplicates, find_title_candidates,
    merge_editions, run_match,
)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "m.db")
    yield conn
    conn.close()


# ── ISBN duplicates ──────────────────────────────────────────────────────
def test_isbn_duplicates_grouped(db):
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (1, 'A', '9780205309023')")
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (2, 'A2', '9780205309023')")
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (3, 'B', '9780374528379')")
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (4, 'A3', '9780205309023')")
    db.commit()
    pairs = find_isbn_duplicates(db)
    assert {(c, d) for c, d, _ in pairs} == {(1, 2), (1, 4)}


def test_empty_or_null_isbn_is_not_a_duplicate(db):
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (1, 'A', NULL)")
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (2, 'A2', NULL)")
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (3, 'A3', '')")
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (4, 'A4', '   ')")
    db.commit()
    assert find_isbn_duplicates(db) == []


# ── Merge primitive ──────────────────────────────────────────────────────
def test_merge_moves_holdings_and_deletes_duplicate(db):
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'Canon')")
    db.execute("INSERT INTO edition (id, title) VALUES (2, 'Dup')")
    db.execute("INSERT INTO holding (edition_id, form) VALUES (1, 'physical')")
    db.execute("INSERT INTO holding (edition_id, form) VALUES (2, 'electronic')")
    db.commit()

    merge_editions(db, 1, 2)
    db.commit()

    (n_dup,) = db.execute("SELECT count(*) FROM edition WHERE id=2").fetchone()
    assert n_dup == 0
    forms = sorted(r[0] for r in db.execute(
        "SELECT form FROM holding WHERE edition_id=1"
    ).fetchall())
    assert forms == ["electronic", "physical"]


def test_merge_is_idempotent(db):
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'A')")
    db.execute("INSERT INTO edition (id, title) VALUES (2, 'B')")
    db.commit()
    merge_editions(db, 1, 2)
    merge_editions(db, 1, 2)   # second call is a no-op, not an error


def test_merge_handles_edition_work_pk_collision(db):
    """Both editions might have (work=W, sequence=1). The merge must drop
    the duplicate's row and keep canonical's — composite PK won't allow
    a naive UPDATE."""
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'C')")
    db.execute("INSERT INTO edition (id, title) VALUES (2, 'D')")
    db.execute("INSERT INTO work (id) VALUES (10)")
    db.execute(
        "INSERT INTO edition_work (edition_id, work_id, sequence) "
        "VALUES (1, 10, 1)"
    )
    db.execute(
        "INSERT INTO edition_work (edition_id, work_id, sequence) "
        "VALUES (2, 10, 1)"
    )
    db.commit()

    merge_editions(db, 1, 2)
    db.commit()

    rows = db.execute(
        "SELECT edition_id, work_id, sequence FROM edition_work"
    ).fetchall()
    assert rows == [(1, 10, 1)]


# ── Title candidate bucketing ────────────────────────────────────────────
def test_title_candidates_use_fold_key(db):
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'Bodhicaryāvatāra')")
    db.execute("INSERT INTO edition (id, title) VALUES (2, 'Bodhicaryavatara')")
    db.execute("INSERT INTO edition (id, title) VALUES (3, 'Way of the Bodhisattva')")
    db.commit()
    pairs = find_title_candidates(db)
    assert pairs == [(1, 2, "bodicaryavatara")]


# ── Orchestrator ─────────────────────────────────────────────────────────
def test_run_match_isbn_auto_merges_titles_queue(db):
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (1, 'A', '9780205309023')")
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (2, 'A v2', '9780205309023')")
    db.execute("INSERT INTO edition (id, title) VALUES (3, 'Bodhicaryāvatāra')")
    db.execute("INSERT INTO edition (id, title) VALUES (4, 'Bodhicaryavatara')")
    db.commit()
    report = run_match(db)
    assert report.isbn_merges == 1
    assert report.title_candidates_queued == 1

    # Edition 2 is gone (merged into 1); 3 and 4 still distinct.
    (n,) = db.execute("SELECT count(*) FROM edition").fetchone()
    assert n == 3
    # A pending edition_dedup item exists.
    (n_q,) = db.execute(
        "SELECT count(*) FROM review_queue WHERE item_type='edition_dedup'"
    ).fetchone()
    assert n_q == 1


def test_run_match_does_not_double_queue_on_rerun(db):
    """Re-running the match pass must not duplicate review_queue items
    for the same candidate pair."""
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'Bodhicaryāvatāra')")
    db.execute("INSERT INTO edition (id, title) VALUES (2, 'Bodhicaryavatara')")
    db.commit()
    run_match(db)
    run_match(db)
    (n,) = db.execute(
        "SELECT count(*) FROM review_queue WHERE item_type='edition_dedup'"
    ).fetchone()
    assert n == 1
