"""Tests for catalogue/work_dedup.py — volume detection/grouping + dup classification.

The headline invariant: a multi-VOLUME set must be classified `volume_set` and
NEVER appear among the duplicate-merge candidates (the trap frbr_data_model.md warns
of — Lamrim Chenmo vols 1–3, etc.).
"""
from __future__ import annotations

from catalogue.db_store import add_alias, fold_key, init_db
from catalogue.cli import work_dedup as WD


def _person(db, name):
    return db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid


def _work(db, title, author):
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, title, "english")
    db.execute("INSERT INTO work_author (work_id, person_id, role) "
               "VALUES (?,?,'author')", (wid, author))
    return wid


def _edition(db, title, wid, volume=None, isbn=None):
    eid = db.execute("INSERT INTO edition (title, volume, isbn) VALUES (?,?,?)",
                     (title, volume, isbn)).lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
               (eid, wid))
    return eid


# ── volume token parsing ──────────────────────────────────────────────────────
def test_volume_number_parsing():
    assert WD.volume_number("The Great Treatise, Vol. 2") == 2
    assert WD.volume_number("In Clear Words, Volume II") == 2
    assert WD.volume_number("Something v. 3") == 3
    assert WD.volume_number("Part 1: Foundations") == 1
    assert WD.volume_number("A plain title") is None
    assert WD.volume_number(None) is None


# ── volume grouping ─────────────────────────────────────────────────────────────
def test_group_volume_set_orders_by_volume_number(tmp_path):
    db = init_db(tmp_path / "v.db")
    a = _person(db, "Tsongkhapa")
    w1 = _work(db, "The Great Treatise Vol. 1", a)
    w2 = _work(db, "The Great Treatise Vol. 2", a)
    w3 = _work(db, "The Great Treatise Vol. 3", a)
    e3 = _edition(db, "The Great Treatise", w3, volume="Vol. 3")
    e1 = _edition(db, "The Great Treatise", w1, volume="Vol. 1")
    e2 = _edition(db, "The Great Treatise", w2, volume="Vol. 2")
    db.commit()
    res = WD.group_volume_set(db, [e3, e1, e2])
    # set_id = lowest edition id; seq follows volume number, not insertion order
    assert res["volume_set_id"] == min(e1, e2, e3)
    seq = {m["edition_id"]: m["volume_seq"] for m in res["members"]}
    assert seq[e1] == 1 and seq[e2] == 2 and seq[e3] == 3
    # persisted
    assert db.execute("SELECT volume_seq FROM edition WHERE id=?", (e2,)).fetchone()[0] == 2


def test_group_volume_set_needs_two(tmp_path):
    db = init_db(tmp_path / "v1.db")
    assert "error" in WD.group_volume_set(db, [1])


# ── classification: the headline invariant ──────────────────────────────────────
def test_volume_set_not_classified_as_duplicate(tmp_path):
    db = init_db(tmp_path / "cls.db")
    a = _person(db, "Tsongkhapa")
    # SAME fold-key title (volume token differs only by number), SAME author
    w1 = _work(db, "The Great Treatise on the Stages of the Path", a)
    w2 = _work(db, "The Great Treatise on the Stages of the Path", a)
    _edition(db, "The Great Treatise", w1, volume="Vol. 1")
    _edition(db, "The Great Treatise", w2, volume="Vol. 2")
    db.commit()
    res = WD.run_dedup(db, fuzzy=False)
    assert res["summary"]["volume_sets"] == 1
    assert res["summary"]["duplicate_candidates"] == 0
    # the volume-set group is NOT in the duplicate list
    dup_works = {m["work_id"] for g in res["tier2_duplicates"] for m in g["members"]}
    assert w1 not in dup_works and w2 not in dup_works


def test_genuine_duplicate_classified_duplicate(tmp_path):
    db = init_db(tmp_path / "dup.db")
    a = _person(db, "Kamalaśīla")
    w1 = _work(db, "Stages of Meditation", a)
    w2 = _work(db, "Stages of Meditation", a)        # no volume/isbn → human-confirm
    _edition(db, "Stages of Meditation (Snow Lion)", w1)
    _edition(db, "Stages of Meditation (Shambhala)", w2)
    db.commit()
    res = WD.run_dedup(db, fuzzy=False)
    assert res["summary"]["duplicate_candidates"] == 1
    assert res["summary"]["volume_sets"] == 0
    assert res["summary"]["isbn_safe_merges"] == 0
    g = res["tier2_duplicates"][0]
    assert g["suggested_winner"] == min(w1, w2)       # lowest id wins


def test_shared_isbn_is_safe_auto_merge(tmp_path):
    db = init_db(tmp_path / "isbn.db")
    a = _person(db, "Tsongkhapa")
    # same title, same author, SAME isbn across all 3 → one book entered thrice
    w1 = _work(db, "The Great Treatise", a)
    w2 = _work(db, "The Great Treatise", a)
    w3 = _work(db, "The Great Treatise", a)
    for w in (w1, w2, w3):
        _edition(db, "The Great Treatise", w, isbn="9781559391528")
    db.commit()
    res = WD.run_dedup(db, fuzzy=False)
    assert res["summary"]["isbn_safe_merges"] == 1
    assert res["summary"]["duplicate_candidates"] == 0
    # applying collapses 3 works → 1
    reports = WD.apply_safe_merges(db)
    assert len(reports) == 2                           # two losers folded into winner
    assert db.execute("SELECT COUNT(*) FROM work").fetchone()[0] == 1
    assert {r[0] for r in db.execute("SELECT id FROM work")} == {min(w1, w2, w3)}


def test_isbn_disagreement_not_safe(tmp_path):
    db = init_db(tmp_path / "isbn2.db")
    a = _person(db, "Author")
    w1 = _work(db, "Same Title", a)
    w2 = _work(db, "Same Title", a)
    _edition(db, "Same Title", w1, isbn="9781559391528")
    _edition(db, "Same Title", w2, isbn="9780877737025")   # different ISBN
    db.commit()
    res = WD.run_dedup(db, fuzzy=False)
    assert res["summary"]["isbn_safe_merges"] == 0         # disagree → not auto
    assert res["summary"]["duplicate_candidates"] == 1


def test_one_isbn_plus_null_not_safe(tmp_path):
    db = init_db(tmp_path / "isbn3.db")
    a = _person(db, "Author")
    w1 = _work(db, "Same Title", a)
    w2 = _work(db, "Same Title", a)
    _edition(db, "Same Title", w1, isbn="9781559391528")
    _edition(db, "Same Title", w2)                          # no isbn → confirm, not auto
    db.commit()
    res = WD.run_dedup(db, fuzzy=False)
    assert res["summary"]["isbn_safe_merges"] == 0
    assert res["summary"]["duplicate_candidates"] == 1


def test_different_authors_not_grouped(tmp_path):
    db = init_db(tmp_path / "diff.db")
    w1 = _work(db, "Introduction", _person(db, "Author One"))
    w2 = _work(db, "Introduction", _person(db, "Author Two"))   # same title, diff author
    db.commit()
    res = WD.run_dedup(db, fuzzy=False)
    assert res["summary"]["duplicate_candidates"] == 0          # author-set differs


# ── apply-volumes + Tier 3 ──────────────────────────────────────────────────────
def test_apply_volume_sets_persists_grouping(tmp_path):
    db = init_db(tmp_path / "av.db")
    a = _person(db, "Tsongkhapa")
    w1 = _work(db, "Great Treatise", a)
    w2 = _work(db, "Great Treatise", a)
    e1 = _edition(db, "Great Treatise", w1, volume="Vol. 1")
    e2 = _edition(db, "Great Treatise", w2, volume="Vol. 2")
    db.commit()
    WD.apply_volume_sets(db)
    sets = db.execute(
        "SELECT volume_set_id FROM edition WHERE id IN (?,?)", (e1, e2)).fetchall()
    assert sets[0][0] is not None and sets[0][0] == sets[1][0]


def test_enqueue_tier3_fuzzy_shared_author(tmp_path):
    db = init_db(tmp_path / "t3.db")
    a = _person(db, "Shared Author")
    # high token overlap, NOT identical fold-key, shared author → queued once
    _work(db, "The Way of the Bodhisattva path", a)
    _work(db, "The Way of the Bodhisattva", a)
    db.commit()
    n = WD.enqueue_tier3(db, threshold=0.5)
    assert n == 1
    assert WD.enqueue_tier3(db, threshold=0.5) == 0          # idempotent (no re-queue)
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE item_type='work_merge'").fetchone()[0] == 1
