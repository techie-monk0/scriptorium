"""Regression tests for the proposal promoter (catalogue/promote.py).

Each test pins an invariant the §8-step-5 cataloguing payoff rests on:
promotion materialises the right canonical rows, name dedup collapses spelling/
mojibake variants onto one person, revert is exact (deletes only what it
created, never a shared person), and both are idempotent.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import init_db, fold_key
from catalogue.services import promote


# ── Fixtures / helpers ───────────────────────────────────────────────────────
@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "promote.db")
    yield conn
    conn.close()


def _book(db, *, title="A Book", holding_id=None):
    """Create an edition + holding; return holding_id."""
    eid = db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    cur = db.execute(
        "INSERT INTO holding (edition_id, form, text_status) VALUES (?, 'electronic', 'ocr_good')",
        (eid,),
    )
    return cur.lastrowid, eid


def _queue(db, payload):
    return db.execute(
        "INSERT INTO review_queue (item_type, payload_json) VALUES ('book_toc_pattern', ?)",
        (json.dumps(payload),),
    ).lastrowid


def test_get_or_create_person_skips_tombstoned(db):
    """Fold-key dedup is live-only: a tombstoned person owning the fold is NOT reused —
    get_or_create_person mints a fresh row instead of resurrecting the deleted one."""
    from catalogue.services import contributor_edit as CE
    from catalogue.db_store import add_alias
    old = db.execute("INSERT INTO person (primary_name) VALUES ('Nāgārjuna')").lastrowid
    add_alias(db, "person", old, "Nāgārjuna", "english")
    db.commit()
    assert promote.get_or_create_person(db, "Nāgārjuna") == (old, False)   # reuse while live
    CE.apply_delete(db, old)                                               # tombstone
    pid2, created2 = promote.get_or_create_person(db, "Nāgārjuna")
    assert created2 is True and pid2 != old                               # fresh row, not the tombstone


def _single(holding_id, *, author="Nāgārjuna", title="Root Verses",
            structure="single_work", translators=None):
    return {
        "holding_id": holding_id,
        "structure": structure,
        "source": "epub-nav",
        "book_authors": [author] if author else [],
        "book_translators": translators or [],
        "contributors_verified": True,
        "contributors_confidence": 0.9,
        "works": [{
            "title": title, "authors": [author] if author else [],
            "translators": translators or [], "kind": "work",
            "locator": "", "section_titles": [], "whole_book": True,
        }],
    }


def _count(db, table):
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ── Promote one proposal ──────────────────────────────────────────────────────
def test_promote_single_work_creates_rows(db):
    hid, eid = _book(db)
    rid = _queue(db, _single(hid))
    res = promote.promote_proposal(db, rid)

    assert res.status == "promoted"
    assert _count(db, "work") == 1
    assert _count(db, "person") == 1
    assert _count(db, "edition_work") == 1
    # author role recorded on the work
    role = db.execute(
        "SELECT role FROM work_author WHERE work_id = ?", (res.work_ids[0],)
    ).fetchone()[0]
    assert role == "author"
    # work title became an alias with the fold-key invariant
    text, key = db.execute(
        "SELECT text, normalized_key FROM work_alias WHERE work_id = ?",
        (res.work_ids[0],),
    ).fetchone()
    assert text == "Root Verses" and key == fold_key("Root Verses")
    # edition_work links the book's edition
    assert db.execute(
        "SELECT edition_id FROM edition_work WHERE work_id = ?", (res.work_ids[0],)
    ).fetchone()[0] == eid
    # queue item flipped to promoted with a provenance row
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (rid,)).fetchone()[0] == "promoted"
    assert db.execute("SELECT 1 FROM promotion WHERE review_item_id=?", (rid,)).fetchone()


def test_translator_recorded_and_linked(db):
    hid, eid = _book(db)
    rid = _queue(db, _single(hid, author="Candrakīrti", translators=["Some Translator"]))
    res = promote.promote_proposal(db, rid)
    # author on the work, translator on the edition (FRBR homes)
    assert {r[0] for r in db.execute(
        "SELECT role FROM work_author WHERE work_id=?", (res.work_ids[0],))} == {"author"}
    eid_w = db.execute("SELECT edition_id FROM edition_work WHERE work_id=?",
                       (res.work_ids[0],)).fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM edition_translator WHERE edition_id=?",
                      (eid_w,)).fetchone()[0] == 1


def test_author_not_also_recorded_as_own_translator(db):
    # The contributor resolver sometimes mirrors the author into book_translators
    # (real corpus: Kathleen McDonald). Promotion must not record her as her own
    # translator.
    hid, _ = _book(db)
    rid = _queue(db, _single(hid, author="Kathleen McDonald",
                             translators=["Kathleen McDonald"]))
    res = promote.promote_proposal(db, rid)
    roles = [r[0] for r in db.execute(
        "SELECT role FROM work_author WHERE work_id=?", (res.work_ids[0],))]
    assert roles == ["author"]
    assert _count(db, "person") == 1
    # not recorded as her own translator on the edition either
    eid_w = db.execute("SELECT edition_id FROM edition_work WHERE work_id=?",
                       (res.work_ids[0],)).fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM edition_translator WHERE edition_id=?",
                      (eid_w,)).fetchone()[0] == 0


def test_multi_work_creates_two_works_one_sequence_each(db):
    hid, eid = _book(db)
    payload = _single(hid, structure="multi_work")
    payload["works"] = [
        {"title": "Text A", "authors": ["Dharmarakṣita"], "translators": [],
         "kind": "root", "locator": "sec1", "section_titles": [], "whole_book": False},
        {"title": "Text B", "authors": ["Dharmarakṣita"], "translators": [],
         "kind": "commentary", "locator": "sec2", "section_titles": [], "whole_book": False},
    ]
    res = promote.promote_proposal(db, rid := _queue(db, payload))
    assert len(res.work_ids) == 2
    seqs = sorted(r[0] for r in db.execute(
        "SELECT sequence FROM edition_work WHERE edition_id=?", (eid,)))
    assert seqs == [1, 2]
    # one shared author across both works → a single person row
    assert _count(db, "person") == 1
    # kind landed in work.notes
    notes = sorted(r[0] for r in db.execute("SELECT notes FROM work"))
    assert notes == ["commentary", "root"]


# ── Name dedup ────────────────────────────────────────────────────────────────
def test_person_dedup_across_books_by_fold_key(db):
    # Two books, same author spelled differently — must collapse to one person.
    # They also share the default title ("Root Verses"), so with work-identity
    # (English title + author overlap) they now resolve to ONE work too — the
    # e310/e312 fork the catalogue used to mint per promotion. See
    # test_work_identity / test_promote_reuses_work_same_title_author.
    h1, _ = _book(db, title="Book 1")
    h2, _ = _book(db, title="Book 2")
    promote.promote_proposal(db, _queue(db, _single(h1, author="Śāntideva")))
    promote.promote_proposal(db, _queue(db, _single(h2, author="Shantideva")))
    assert _count(db, "person") == 1
    assert _count(db, "work") == 1


def test_mojibake_variant_dedups_onto_clean_name(db):
    h1, _ = _book(db, title="B1")
    h2, _ = _book(db, title="B2")
    # fold_key strips diacritics, so the clean and decomposed forms collapse.
    promote.promote_proposal(db, _queue(db, _single(h1, author="Atiśa")))
    promote.promote_proposal(db, _queue(db, _single(h2, author="Atisa")))
    assert _count(db, "person") == 1


# ── Revert ────────────────────────────────────────────────────────────────────
def test_revert_deletes_created_rows_and_restores_queue(db):
    hid, _ = _book(db)
    rid = _queue(db, _single(hid))
    promote.promote_proposal(db, rid)
    promote.revert_proposal(db, rid)
    assert _count(db, "work") == 0
    assert _count(db, "person") == 0
    assert _count(db, "edition_work") == 0
    assert _count(db, "promotion") == 0
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (rid,)).fetchone()[0] == "pending"


def test_revert_keeps_person_shared_with_another_promotion(db):
    h1, _ = _book(db, title="B1")
    h2, _ = _book(db, title="B2")
    r1 = _queue(db, _single(h1, author="Tsongkhapa"))
    r2 = _queue(db, _single(h2, author="Tsongkhapa"))
    promote.promote_proposal(db, r1)
    promote.promote_proposal(db, r2)  # reuses the person from r1
    assert _count(db, "person") == 1

    promote.revert_proposal(db, r1)   # must NOT delete the shared person
    assert _count(db, "person") == 1
    assert _count(db, "work") == 1    # r2 reused r1's work (same title+author) → survives

    promote.revert_proposal(db, r2)   # now nothing references it
    assert _count(db, "person") == 0
    assert _count(db, "work") == 0


# ── Work identity (get_or_create_work wired into promotion) ───────────────────
def test_promote_reuses_work_same_title_author(db):
    # Two editions of the same titled work by the same author → ONE work, two
    # edition_work links (the e310/e312 collapse).
    h1, e1 = _book(db, title="Ed 1")
    h2, e2 = _book(db, title="Ed 2")
    promote.promote_proposal(db, _queue(db, _single(h1, author="Nāgārjuna", title="MMK")))
    r2 = promote.promote_proposal(db, _queue(db, _single(h2, author="Nāgārjuna", title="MMK")))
    assert _count(db, "work") == 1
    assert _count(db, "edition_work") == 2
    assert r2.created_work_ids == []                 # the second reused the first
    assert r2.merge_candidate_work_ids == []


def test_promote_distinct_titles_keep_separate_works(db):
    h1, _ = _book(db, title="Ed 1")
    h2, _ = _book(db, title="Ed 2")
    promote.promote_proposal(db, _queue(db, _single(h1, author="Nāgārjuna", title="Alpha")))
    promote.promote_proposal(db, _queue(db, _single(h2, author="Nāgārjuna", title="Beta")))
    assert _count(db, "work") == 2


def test_promote_same_title_different_author_flags_merge_candidate(db):
    # Same English title, non-overlapping authors → NOT auto-merged; a new work
    # is created and flagged for human review (homonym safety).
    h1, _ = _book(db, title="Ed 1")
    h2, _ = _book(db, title="Ed 2")
    promote.promote_proposal(db, _queue(db, _single(h1, author="Nāgārjuna", title="Root")))
    r2 = promote.promote_proposal(db, _queue(db, _single(h2, author="Candrakīrti", title="Root")))
    assert _count(db, "work") == 2
    assert r2.merge_candidate_work_ids == r2.created_work_ids != []


def test_revert_reuser_keeps_the_shared_work(db):
    # r1 creates the work; r2 reuses it. Reverting r2 detaches r2's edition but
    # must keep the work (r1 still owns it).
    h1, e1 = _book(db, title="Ed 1")
    h2, e2 = _book(db, title="Ed 2")
    r1 = _queue(db, _single(h1, author="Nāgārjuna", title="MMK"))
    r2 = _queue(db, _single(h2, author="Nāgārjuna", title="MMK"))
    promote.promote_proposal(db, r1)
    promote.promote_proposal(db, r2)
    promote.revert_proposal(db, r2)
    assert _count(db, "work") == 1
    assert _count(db, "person") == 1
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE edition_id=?", (e2,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE edition_id=?", (e1,)).fetchone()[0] == 1
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (r1,)).fetchone()[0] == "promoted"


def test_promote_is_idempotent(db):
    hid, _ = _book(db)
    rid = _queue(db, _single(hid))
    promote.promote_proposal(db, rid)
    res2 = promote.promote_proposal(db, rid)
    assert res2.status == "already"
    assert _count(db, "work") == 1


def test_revert_unpromoted_is_noop(db):
    hid, _ = _book(db)
    rid = _queue(db, _single(hid))
    res = promote.revert_proposal(db, rid)
    assert res.status == "already"


# ── Buckets / segments ────────────────────────────────────────────────────────
def test_bucket_classification():
    assert promote.bucket(_single(1)) == "single_work"
    assert promote.bucket(_single(1, structure="multi_work")) == "multi_work"
    assert promote.bucket(_single(1, structure="collection_unsegmented")) == "unsegmented"
    # no author anywhere → review bucket, regardless of structure
    p = _single(1, author=None, structure="multi_work")
    assert promote.bucket(p) == "no_author"


def test_promote_segment_only_touches_its_bucket(db):
    h1, _ = _book(db, title="single")
    h2, _ = _book(db, title="unseg")
    rid_single = _queue(db, _single(h1))
    rid_unseg = _queue(db, _single(h2, structure="collection_unsegmented"))
    summary = promote.promote_segment(db, "single_work")
    assert summary["promoted"] == 1 and summary["ids"] == [rid_single]
    # the unsegmented one is untouched
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (rid_unseg,)).fetchone()[0] == "pending"


def test_segment_counts(db):
    h1, _ = _book(db, title="a")
    h2, _ = _book(db, title="b")
    _queue(db, _single(h1))
    rid2 = _queue(db, _single(h2, structure="collection_unsegmented"))
    counts = promote.segment_counts(db)
    assert counts["single_work"]["pending"] == 1
    assert counts["unsegmented"]["pending"] == 1
    promote.promote_proposal(db, rid2)
    counts = promote.segment_counts(db)
    assert counts["unsegmented"]["promoted"] == 1
    assert counts["unsegmented"]["pending"] == 0


def test_no_edition_proposal_skips_cleanly(db):
    # A proposal whose holding doesn't exist must not create rows or crash.
    rid = _queue(db, _single(999))
    res = promote.promote_proposal(db, rid)
    assert res.status == "no_edition"
    assert _count(db, "work") == 0


def test_promote_segment_verify_runs_ingest_match(db):
    """promote_segment(verify=True) binds HARD authority hits on the rows it just
    created — ingest-time matching for a batch add."""
    from catalogue.services import promote as P
    from catalogue.services import verify as V
    hid, eid = _book(db, title="X")
    _queue(db, _single(hid, author="Atisha", title="A Lamp"))
    db.commit()

    class _Stub:
        name = "stub"
        def verify(self, db, kind, text):
            if kind == "person" and text == "Atisha":
                return V.Match("Q320150", "wikidata", "Atisha", [], "stub")
            return None
    # swap default_verifiers so the batch uses our offline stub
    orig = V.default_verifiers
    V.default_verifiers = lambda **k: [_Stub()]
    try:
        summary = P.promote_segment(db, "single_work", verify=True)
    finally:
        V.default_verifiers = orig
    assert summary["promoted"] == 1
    assert summary["verified"]["person"]["matched"] == 1
    pid = db.execute("SELECT person_id FROM work_author WHERE role='author'").fetchone()[0]
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] \
        == "Q320150"
