"""Unit tests for get_or_create_work — the work-identity resolver.

Lookup order: canonical# → folded original-language alias → folded English alias
guarded by author overlap → create. Non-destructive: an English-title collision
without author agreement creates a flagged merge-candidate, never an auto-merge.
"""
import pytest

from catalogue.db_store import init_db, fold_key
from catalogue.services import work_identity as wi


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "wi.db")
    yield conn
    conn.close()


def _person(db, name):
    return db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid


def _aliases(db, wid):
    return {(r[0], r[1]) for r in db.execute(
        "SELECT scheme, text FROM work_alias WHERE work_id = ?", (wid,)).fetchall()}


def test_create_fresh_adds_english_alias(db):
    wid, created, mc = wi.get_or_create_work(db, english_title="Root Verses")
    assert created and not mc
    assert ("english", "Root Verses") in _aliases(db, wid)


def test_reuse_by_canonical_number(db):
    a, *_ = wi.get_or_create_work(db, canonical=("toh", "3824"), english_title="X")
    b, created, mc = wi.get_or_create_work(db, canonical=("toh", "3824"), english_title="Totally Different")
    assert b == a and not created and not mc


def test_reuse_by_original_language_title_fills_columns(db):
    a, *_ = wi.get_or_create_work(db, english_title="Fundamental Wisdom",
                                  original_titles={"sanskrit": "Mūlamadhyamakakārikā"})
    # A Tibetan edition with the same Sanskrit title resolves to the same work,
    # and its Tibetan title backfills the empty column.
    b, created, mc = wi.get_or_create_work(
        db, original_titles={"sanskrit": "Mulamadhyamakakarika",   # OCR/diacritic variant
                             "tibetan": "dbu ma rtsa ba"})
    assert b == a and not created
    row = db.execute("SELECT sanskrit_title, tibetan_title FROM work WHERE id=?", (a,)).fetchone()
    assert row[0] == "Mūlamadhyamakakārikā"        # original kept (COALESCE never overwrites)
    assert row[1] == "dbu ma rtsa ba"              # empty column filled


def test_reuse_by_english_title_requires_author_overlap(db):
    p = _person(db, "Nāgārjuna")
    a, *_ = wi.get_or_create_work(db, english_title="MMK", author_pids=[p])
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (a, p))
    b, created, mc = wi.get_or_create_work(db, english_title="MMK", author_pids=[p])
    assert b == a and not created and not mc


def test_english_title_collision_without_author_is_merge_candidate(db):
    p1, p2 = _person(db, "Nāgārjuna"), _person(db, "Candrakīrti")
    a, *_ = wi.get_or_create_work(db, english_title="Root", author_pids=[p1])
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (a, p1))
    b, created, mc = wi.get_or_create_work(db, english_title="Root", author_pids=[p2])
    assert b != a and created and mc                # new work, flagged for review


def test_no_author_no_merge_on_title_alone(db):
    a, *_ = wi.get_or_create_work(db, english_title="Anonymous Treatise")
    b, created, mc = wi.get_or_create_work(db, english_title="Anonymous Treatise")
    assert b != a and created and mc                # title-only is too weak to merge


def test_find_helpers(db):
    wid, *_ = wi.get_or_create_work(db, canonical=("bdrc", "W123"), english_title="T")
    assert wi.find_work_by_canonical(db, "bdrc", "W123") == wid
    assert wi.find_work_by_canonical(db, "bdrc", "nope") is None
    assert wid in wi.find_works_by_title_key(db, fold_key("T"))
