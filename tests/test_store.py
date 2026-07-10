"""The single guarded write API (catalogue/store.py).

These pin the two guarantees the layer exists to give: a write that doesn't
affect the intended rows RAISES (it never passes for success), and a Store
refuses to operate on a schema-behind DB.
"""
from __future__ import annotations

import sqlite3

import pytest

from catalogue.db_store import (
    DryRunConnection, SchemaDriftError, connect, init_db,
)
from catalogue.db_store import Store, WriteError, as_store


@pytest.fixture
def store(tmp_path):
    conn = init_db(tmp_path / "s.db")
    conn.execute("INSERT INTO person (id, primary_name) VALUES (1, 'X')")
    conn.commit()
    return Store(conn)


# ── write post-condition: the intended change took place ─────────────────────
def test_write_rows1_passes_when_one_row_changes(store):
    store.write("UPDATE person SET origin = ? WHERE id = ?", ("a", 1), rows=1)
    store.commit()
    assert store.execute("SELECT origin FROM person WHERE id = 1").fetchone()[0] == "a"


def test_write_rows1_raises_when_no_row_matches(store):
    # The silent-loss case: WHERE matches nothing → the change did NOT happen.
    with pytest.raises(WriteError):
        store.write("UPDATE person SET origin = ? WHERE id = ?", ("a", 999), rows=1)


def test_write_range_and_optout(store):
    store.execute("INSERT INTO person (id, primary_name) VALUES (2, 'Y')")
    # exactly-2
    store.write("UPDATE person SET origin = 'z' WHERE id IN (1, 2)", rows=2)
    # at-least-1
    store.write("UPDATE person SET dates = '1' WHERE origin = 'z'", rows=(1, None))
    # explicit opt-out: a write that may legitimately match nothing
    store.write("UPDATE person SET dates = '2' WHERE id = 999", rows=None)


def test_insert_returns_lastrowid_and_asserts_one_row(store):
    nid = store.insert("person", primary_name="Z")
    assert store.execute("SELECT primary_name FROM person WHERE id = ?",
                         (nid,)).fetchone()[0] == "Z"


# ── transparent proxy: a Store is a drop-in for the connection ───────────────
def test_store_proxies_reads_and_commit(store):
    # execute (read) + commit reach the underlying connection unchanged
    assert store.execute("SELECT count(*) FROM person").fetchone()[0] == 1
    store.execute("INSERT INTO person (id, primary_name) VALUES (3, 'P')")
    store.commit()
    assert store.execute("SELECT count(*) FROM person").fetchone()[0] == 2


def test_as_store_idempotent_and_wraps_bare_conn(tmp_path):
    conn = init_db(tmp_path / "a.db")
    s = as_store(conn)
    assert isinstance(s, Store)
    assert as_store(s) is s                       # already a Store → same object


def test_store_wraps_dry_run_connection(tmp_path):
    raw = init_db(tmp_path / "d.db")
    raw.execute("INSERT INTO person (id, primary_name) VALUES (1, 'X')")
    raw.commit()
    s = Store(DryRunConnection(raw))
    # the postcondition still sees the real rowcount inside the (uncommitted) txn
    s.write("UPDATE person SET origin = 'a' WHERE id = 1", rows=1)
    s.commit()                                    # swallowed by DryRunConnection
    s.rollback()
    chk = sqlite3.connect(tmp_path / "d.db")
    assert chk.execute("SELECT origin FROM person WHERE id = 1").fetchone()[0] is None


# ── schema guard at construction ─────────────────────────────────────────────
def test_store_refuses_schema_behind_db(tmp_path):
    init_db(tmp_path / "old.db").close()
    conn = connect(tmp_path / "old.db")
    conn.execute("ALTER TABLE holding DROP COLUMN notes")   # simulate drift
    conn.commit()
    with pytest.raises(SchemaDriftError):
        Store(conn)                                # check_schema=True default
    # the per-request escape hatch skips the check
    Store(conn, check_schema=False)
