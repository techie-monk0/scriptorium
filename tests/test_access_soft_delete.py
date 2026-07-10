"""Soft-delete foundation — roots tombstone (deleted_at), reads hide them, ids freeze (reorg Phase 3).

The catalog roots (edition/work/person/subject/collection/tradition) carry a nullable `deleted_at`:
a delete sets it (the row persists, the id is never reused → the recycled-id corruption class dies),
reads filter `deleted_at IS NULL`, and `restore` flips it back. Holdings are NOT soft-deleted — they
hard-delete. See entity_api_model.md §6.
"""
from __future__ import annotations

from catalogue.access_api import system_access
from catalogue.contracts import Ref
from catalogue.db_store import init_db


def _seed(tmp_path):
    db = tmp_path / "t.db"
    c = init_db(db)
    eid = c.execute("INSERT INTO edition (title, isbn) VALUES ('Bk', '111')").lastrowid
    subj = c.execute("INSERT INTO subject (name) VALUES ('Madhyamaka')").lastrowid
    c.commit()
    c.close()
    return dict(db=db, eid=eid, subj=subj)


def test_migration_added_deleted_at_to_every_root(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        for table in ("edition", "work", "person", "subject", "collection", "tradition"):
            cols = {r[1] for r in acc.ro.execute(f"PRAGMA table_info({table})").fetchall()}
            assert "deleted_at" in cols, f"{table} missing deleted_at"


def test_tombstone_persists_row_but_hides_from_reads(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.subjects.writes.apply(acc.subjects.writes.plan_delete(Ref("subject", s["subj"])))
        # hidden from the API…
        assert acc.subjects.reads.get(s["subj"]) is None
        # …but the row is still physically there, tombstoned (id frozen, never reused)
        row = acc.ro.execute("SELECT id, deleted_at FROM subject WHERE id=?", (s["subj"],)).fetchone()
        assert row is not None and row[1] is not None


def test_redelete_of_tombstone_is_blocked(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.subjects.writes.apply(acc.subjects.writes.plan_delete(Ref("subject", s["subj"])))
        # a tombstoned root reads as absent, so a second delete can't even be planned
        replan = acc.subjects.writes.plan_delete(Ref("subject", s["subj"]))
        assert not replan.appliable and any(b.code == "not_found" for b in replan.blocks)


def test_edition_tombstone_then_restore_shell(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.editions.writes.apply(acc.editions.writes.plan_delete(Ref("edition", s["eid"])))
        assert acc.editions.reads.get(s["eid"]) is None
        acc.editions.writes.restore(Ref("edition", s["eid"]))
        assert acc.editions.reads.get(s["eid"]).title == "Bk"        # shell back, flag flipped
