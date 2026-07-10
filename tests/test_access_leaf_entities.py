"""Leaf entities — Subject / Collection / Tradition (reorg Phase 3).

These roots own no parts and only edge to Work, so one generic Reader/Writer (a `LeafSpec`) serves
all three: read by id / by work, and a trivial cascade delete (the FK drops the link rows; nothing
non-FK to purge). System through a real DB. See entity_api_model.md §2/§3.
"""
from __future__ import annotations

import pytest

from catalogue.access_api import system_access
from catalogue.contracts import NotFound, Ref, StaleWrite
from catalogue.db_store import init_db


def _seed(tmp_path):
    db = tmp_path / "t.db"
    c = init_db(db)
    w = c.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    subj = c.execute("INSERT INTO subject (name, kind) VALUES ('Madhyamaka', 'topic')").lastrowid
    coll = c.execute("INSERT INTO collection (name) VALUES ('Lamrim set')").lastrowid
    # A name OUTSIDE the config-seeded vocab (vocab.json `_tradition`), which init_db now
    # seeds — so this fresh insert doesn't collide on the UNIQUE(name) constraint.
    trad = c.execute("INSERT INTO tradition (name) VALUES ('Bön')").lastrowid
    c.execute("INSERT INTO work_subject (work_id, subject_id) VALUES (?, ?)", (w, subj))
    c.execute("INSERT INTO collection_member (collection_id, work_id) VALUES (?, ?)", (coll, w))
    c.execute("INSERT INTO work_tradition (work_id, tradition_id) VALUES (?, ?)", (w, trad))
    c.commit()
    c.close()
    return dict(db=db, w=w, subj=subj, coll=coll, trad=trad)


def test_reads_get_and_by_work(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        assert acc.subjects.reads.get(s["subj"]).name == "Madhyamaka"
        assert acc.collections.reads.get(s["coll"]).name == "Lamrim set"
        assert acc.traditions.reads.get(s["trad"]).name == "Bön"
        assert {x.id for x in acc.subjects.reads.by_work(s["w"])} == {s["subj"]}
        assert {x.id for x in acc.collections.reads.by_work(s["w"])} == {s["coll"]}
        assert {x.id for x in acc.traditions.reads.by_work(s["w"])} == {s["trad"]}


def test_delete_tombstones_hides_but_keeps_row_and_link(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.subjects.writes.apply(acc.subjects.writes.plan_delete(Ref("subject", s["subj"])))
        assert acc.subjects.reads.get(s["subj"]) is None                 # hidden from reads
        # soft-delete: the row PERSISTS (tombstoned, id frozen) and its link stays — so restore is
        # a pure flag-flip. The work itself is untouched.
        assert acc.ro.execute("SELECT deleted_at FROM subject WHERE id=?", (s["subj"],)).fetchone()[0] is not None
        assert acc.ro.execute("SELECT count(*) FROM work_subject WHERE subject_id=?", (s["subj"],)).fetchone()[0] == 1
        assert acc.ro.execute("SELECT count(*) FROM work WHERE id=?", (s["w"],)).fetchone()[0] == 1


def test_restore_unhides(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.collections.writes.apply(acc.collections.writes.plan_delete(Ref("collection", s["coll"])))
        assert acc.collections.reads.get(s["coll"]) is None
        acc.collections.writes.restore(Ref("collection", s["coll"]))
        assert acc.collections.reads.get(s["coll"]).name == "Lamrim set"   # back, flag flipped


def test_delete_missing_is_blocked(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.collections.writes.plan_delete(Ref("collection", 99999))
        assert not plan.appliable and any(b.code == "not_found" for b in plan.blocks)


def test_fingerprint_mismatch_is_stale(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.traditions.writes.plan_delete(Ref("tradition", s["trad"]))
        acc.rw.execute("UPDATE tradition SET name='Bönpo' WHERE id=?", (s["trad"],))  # drift to a non-seeded name
        acc.rw.commit()
        with pytest.raises(StaleWrite):
            acc.traditions.writes.apply(plan)


def test_recheck_vanished_is_not_found(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.subjects.writes.plan_delete(Ref("subject", s["subj"]))
        acc.rw.execute("DELETE FROM subject WHERE id=?", (s["subj"],))
        acc.rw.commit()
        with pytest.raises(NotFound):
            acc.subjects.writes.apply(plan)
