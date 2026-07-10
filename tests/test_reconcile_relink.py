"""Reconcile re-attaches reader-mark orphans on re-import (reader plan N6 wiring)."""
from __future__ import annotations

from catalogue.test_kit import seed_minimal
from catalogue.db_store import reader_state as rs
from catalogue.services import reconcile


def test_relink_reattaches_orphans_by_content_hash(cat_conn):
    seed_minimal(cat_conn)
    cat_conn.execute("PRAGMA foreign_keys=ON")
    store = rs.SqliteReaderStateStore(cat_conn)
    store.ensure_schema()
    hid = cat_conn.execute("SELECT id FROM holding ORDER BY id LIMIT 1").fetchone()[0]
    eid = cat_conn.execute("SELECT edition_id FROM holding WHERE id = ?", (hid,)).fetchone()[0]
    cat_conn.execute("UPDATE holding SET content_hash = 'sig-x' WHERE id = ?", (hid,))
    store.apply_annotation(id="a1", holding_id=hid, kind="highlight", updated_at="2026-06-29T10:00:00Z")

    cat_conn.execute("DELETE FROM holding WHERE id = ?", (hid,))                 # orphan (SET NULL)
    cat_conn.execute("INSERT INTO holding (edition_id, content_hash) VALUES (?, 'sig-x')", (eid,))
    new_hid = cat_conn.execute("SELECT id FROM holding WHERE content_hash = 'sig-x'").fetchone()[0]

    reconcile._relink_reader_orphans(cat_conn, new_hid, "sig-x")
    assert cat_conn.execute("SELECT holding_id FROM annotation WHERE id = 'a1'").fetchone()[0] == new_hid


def test_relink_is_safe_without_reader_tables(cat_conn):
    seed_minimal(cat_conn)                       # no reader-state tables created → must not raise
    reconcile._relink_reader_orphans(cat_conn, 1, "sig-x")
