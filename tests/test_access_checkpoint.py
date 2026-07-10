"""Pre-destructive checkpoint — a destructive op snapshots the rows it HARD-removes before deleting
them, so `restore` brings back not just the tombstoned root but its hard-deleted children too.

An edition delete tombstones the edition (recoverable) but HARD-deletes its holdings; the checkpoint
captures those holdings so restore re-inserts them. See entity_api_model.md §6.
"""
from __future__ import annotations

import pytest

from catalogue.access_api import system_access
from catalogue.contracts import Ref, StaleWrite
from catalogue.db_store import init_db


def _seed(tmp_path):
    db = tmp_path / "t.db"
    c = init_db(db)
    eid = c.execute("INSERT INTO edition (title) VALUES ('Restore Me')").lastrowid
    h1 = c.execute("INSERT INTO holding (edition_id, form, file_path) "
                   "VALUES (?, 'electronic', '/lib/a.pdf')", (eid,)).lastrowid
    h2 = c.execute("INSERT INTO holding (edition_id, form, file_path) "
                   "VALUES (?, 'electronic', '/lib/b.pdf')", (eid,)).lastrowid
    c.commit(); c.close()
    return dict(db=db, eid=eid, hids=[h1, h2])


def test_delete_checkpoints_holdings_and_restore_brings_them_back(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.editions.writes.apply(acc.editions.writes.plan_delete(Ref("edition", s["eid"])))
        # edition tombstoned, holdings HARD-deleted
        assert acc.editions.reads.get(s["eid"]) is None
        assert acc.ro.execute("SELECT count(*) FROM holding WHERE edition_id=?",
                              (s["eid"],)).fetchone()[0] == 0
        # a checkpoint row captured the holdings
        snap = acc._latest_checkpoint("edition", s["eid"])
        assert snap and len(snap["holding"]) == 2

        # restore brings back BOTH the edition shell and its holdings (same ids + paths)
        acc.editions.writes.restore(Ref("edition", s["eid"]))
        assert acc.editions.reads.get(s["eid"]).title == "Restore Me"
        rows = acc.ro.execute(
            "SELECT id, file_path FROM holding WHERE edition_id=? ORDER BY id",
            (s["eid"],)).fetchall()
        assert [r[0] for r in rows] == s["hids"]
        assert [r[1] for r in rows] == ["/lib/a.pdf", "/lib/b.pdf"]


def test_restore_refuses_recycled_holding_id(tmp_path):
    """If a freed holding id was recycled to a DIFFERENT edition before restore, restore refuses
    (StaleWrite) rather than silently dropping the holding — then rolls back cleanly."""
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.editions.writes.apply(acc.editions.writes.plan_delete(Ref("edition", s["eid"])))
        other = acc.rw.execute("INSERT INTO edition (title) VALUES ('Other')").lastrowid
        acc.rw.execute("INSERT INTO holding (id, edition_id, form) VALUES (?, ?, 'electronic')",
                       (s["hids"][0], other))      # a new edition recycles the freed id
        acc.commit()
        with pytest.raises(StaleWrite):
            acc.editions.writes.restore(Ref("edition", s["eid"]))
        # rolled back: edition still tombstoned, the recycled holding untouched
        assert acc.editions.reads.get(s["eid"]) is None
        assert acc.rw.execute("SELECT edition_id FROM holding WHERE id=?",
                              (s["hids"][0],)).fetchone()[0] == other


def test_checkpoint_rolls_back_if_delete_fails(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        before = acc.rw.execute("SELECT count(*) FROM checkpoint").fetchone()[0]
        acc.rollback()
    # a clean delete then verify exactly one checkpoint was written (not orphaned/duplicated)
    with system_access(s["db"]) as acc:
        acc.editions.writes.apply(acc.editions.writes.plan_delete(Ref("edition", s["eid"])))
        n = acc.rw.execute("SELECT count(*) FROM checkpoint WHERE entity_id=?",
                           (s["eid"],)).fetchone()[0]
        assert n == 1
