"""Tests for catalogue/work_merge.py — folding a duplicate work into a canonical one."""
from __future__ import annotations

from catalogue.db_store import add_alias, fold_key, init_db
from catalogue.services.work_merge import apply_work_merge, author_set, plan_merge


def _person(db, name):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid
    add_alias(db, "person", pid, name, "english")
    return pid


def _work(db, title, *, canonical_system=None, canonical_number=None):
    wid = db.execute(
        "INSERT INTO work (canonical_system, canonical_number) VALUES (?, ?)",
        (canonical_system, canonical_number)).lastrowid
    add_alias(db, "work", wid, title, "english")
    return wid


def _edition_with_work(db, etitle, wid):
    eid = db.execute("INSERT INTO edition (title) VALUES (?)", (etitle,)).lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
               (eid, wid))
    return eid


def test_apply_merge_repoints_editions_and_deletes_loser(tmp_path):
    db = init_db(tmp_path / "m.db")
    auth = _person(db, "Kamalaśīla")
    win = _work(db, "Stages of Meditation")
    los = _work(db, "Stages of Meditation")
    for w in (win, los):
        db.execute("INSERT INTO work_author (work_id, person_id, role) "
                   "VALUES (?,?,'author')", (w, auth))
    e_win = _edition_with_work(db, "Stages of Meditation (A)", win)
    e_los = _edition_with_work(db, "Stages of Meditation (B)", los)
    db.commit()

    rep = apply_work_merge(db, los, win)
    assert "error" not in rep
    # loser gone; both editions now point at the winner
    assert db.execute("SELECT 1 FROM work WHERE id=?", (los,)).fetchone() is None
    eids = {r[0] for r in db.execute(
        "SELECT edition_id FROM edition_work WHERE work_id=?", (win,)).fetchall()}
    assert eids == {e_win, e_los}
    # loser's contributor row deduped against the winner's (same author) — one row
    assert db.execute("SELECT COUNT(*) FROM work_author WHERE work_id=?",
                      (win,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM work_author WHERE work_id=?",
                      (los,)).fetchone()[0] == 0


def test_apply_merge_dedups_aliases_on_foldkey(tmp_path):
    db = init_db(tmp_path / "a.db")
    win = _work(db, "The Lankavatara Sutra")
    los = _work(db, "The Lankavatara Sutra")           # identical fold-key → dropped
    add_alias(db, "work", los, "Laṅkāvatāra Sūtra", "iast")   # folds same as winner's? no
    add_alias(db, "work", los, "楞伽經", "other")               # distinct → moves
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM work_alias WHERE work_id=?", (win,)).fetchone()[0]

    apply_work_merge(db, los, win)
    keys = {r[0] for r in db.execute(
        "SELECT normalized_key FROM work_alias WHERE work_id=?", (win,)).fetchall()}
    # winner keeps its own title key; the distinct-script alias moved in; the exact
    # duplicate title alias was dropped (not duplicated)
    assert fold_key("The Lankavatara Sutra") in keys
    assert fold_key("楞伽經") in keys
    assert db.execute("SELECT COUNT(*) FROM work_alias WHERE work_id=?",
                      (win,)).fetchone()[0] > before
    # no two aliases on the winner share a fold-key
    allkeys = [r[0] for r in db.execute(
        "SELECT normalized_key FROM work_alias WHERE work_id=?", (win,)).fetchall()]
    assert len(allkeys) == len(set(allkeys))


def test_apply_merge_carries_canonical_into_empty_winner(tmp_path):
    db = init_db(tmp_path / "c.db")
    win = _work(db, "Bodhicaryāvatāra")                       # no canonical
    los = _work(db, "Bodhicaryāvatāra", canonical_system="toh", canonical_number="3871")
    db.commit()
    apply_work_merge(db, los, win)
    row = db.execute("SELECT canonical_system, canonical_number FROM work WHERE id=?",
                     (win,)).fetchone()
    assert row == ("toh", "3871")


def test_edition_work_collision_drops_to_winner(tmp_path):
    db = init_db(tmp_path / "coll.db")
    win = _work(db, "Same")
    los = _work(db, "Same")
    # BOTH works already linked to the SAME edition at the SAME sequence → collision
    eid = db.execute("INSERT INTO edition (title) VALUES ('Anthology')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
               (eid, win))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
               (eid, los))
    db.commit()
    rep = apply_work_merge(db, los, win)
    assert "error" not in rep
    # exactly one (edition, winner, seq) row survives; loser's collided row dropped
    assert db.execute(
        "SELECT COUNT(*) FROM edition_work WHERE edition_id=? AND work_id=?",
        (eid, win)).fetchone()[0] == 1


def test_relationship_endpoints_repointed(tmp_path):
    db = init_db(tmp_path / "rel.db")
    win = _work(db, "Root")
    los = _work(db, "Root")
    other = _work(db, "Commentary")
    db.execute("INSERT INTO relationship (from_work_id, relation, to_work_id) "
               "VALUES (?, 'comments_on', ?)", (other, los))
    db.commit()
    apply_work_merge(db, los, win)
    assert db.execute("SELECT to_work_id FROM relationship WHERE from_work_id=?",
                      (other,)).fetchone()[0] == win


def test_guards(tmp_path):
    db = init_db(tmp_path / "g.db")
    a = _work(db, "X", canonical_system="toh", canonical_number="1")
    b = _work(db, "X", canonical_system="toh", canonical_number="2")
    assert "error" in plan_merge(db, a, a)                    # self-merge
    assert "error" in plan_merge(db, a, 9999)                 # missing target
    assert "error" in plan_merge(db, a, b)                    # conflicting canonical


def test_author_set_helper(tmp_path):
    db = init_db(tmp_path / "as.db")
    w = _work(db, "W")
    p1, p2 = _person(db, "A"), _person(db, "B")
    db.execute("INSERT INTO work_author VALUES (?,?,'author')", (w, p1))
    db.execute("INSERT INTO work_author VALUES (?,?,'compiler')", (w, p2))
    db.commit()
    assert author_set(db, w) == frozenset({p1})               # non-author role excluded
