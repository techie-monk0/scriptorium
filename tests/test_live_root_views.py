"""v_live_<root> — the soft-delete live-row views (reorg Phase 4 foundation).

One view per soft-deletable root (edition / work / person / subject / collection / tradition) =
`SELECT * FROM <root> WHERE deleted_at IS NULL`. Reads that must not surface a tombstoned row select
FROM the view; this is what lets a delete route through the access-API (tombstone) without the row
leaking into reads that predate soft-delete. See docs/access/entity_api_model.md §6.
"""
from __future__ import annotations

import pytest

from catalogue.db_store import init_db

ROOTS = ["edition", "work", "person", "subject", "collection", "tradition"]


def test_every_root_has_a_live_view(tmp_path):
    conn = init_db(tmp_path / "t.db")
    views = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'").fetchall()}
    for r in ROOTS:
        assert f"v_live_{r}" in views


@pytest.mark.parametrize("root", ROOTS)
def test_view_columns_match_the_base_table(tmp_path, root):
    """`SELECT *` must expose the full current column set (incl. deleted_at, rev) — the DROP+CREATE
    each init keeps it from going stale after a column ALTER."""
    conn = init_db(tmp_path / "t.db")
    base = [r[1] for r in conn.execute(f"PRAGMA table_info({root})").fetchall()]
    view = [r[1] for r in conn.execute(f"PRAGMA table_info(v_live_{root})").fetchall()]
    assert view == base


def test_edition_view_hides_tombstones_and_keeps_live(tmp_path):
    conn = init_db(tmp_path / "t.db")
    live = conn.execute("INSERT INTO edition (title) VALUES ('Live')").lastrowid
    dead = conn.execute("INSERT INTO edition (title) VALUES ('Dead')").lastrowid
    conn.execute("UPDATE edition SET deleted_at = datetime('now') WHERE id = ?", (dead,))
    conn.commit()
    ids = {r[0] for r in conn.execute("SELECT id FROM v_live_edition").fetchall()}
    assert ids == {live}                                   # tombstone hidden
    assert conn.execute("SELECT COUNT(*) FROM edition").fetchone()[0] == 2   # base still has both


def test_work_view_hides_tombstones(tmp_path):
    conn = init_db(tmp_path / "t.db")
    live = conn.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    dead = conn.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    conn.execute("UPDATE work SET deleted_at = datetime('now') WHERE id = ?", (dead,))
    conn.commit()
    ids = {r[0] for r in conn.execute("SELECT id FROM v_live_work").fetchall()}
    assert ids == {live}
