"""connect_ro / connect_rw — the read/write split at the connection chokepoint (Phase 2).

A reader gets an OS-enforced read-only connection (`mode=ro`), so a read/preview path
physically cannot write even with a bug. See db_store/connection.py + entity_api_model.md §9.
"""
from __future__ import annotations

import sqlite3

import pytest

from catalogue.db_store import connect, connect_ro, connect_rw, init_db


def test_connect_rw_is_the_default_connect():
    assert connect_rw is connect


def test_connect_rw_allows_writes(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    conn = connect_rw(p)
    conn.execute("INSERT INTO edition (title) VALUES ('X')")
    conn.commit()
    assert conn.execute("SELECT count(*) FROM edition").fetchone()[0] == 1


def test_connect_ro_can_read(tmp_path):
    p = tmp_path / "t.db"
    c = init_db(p)
    c.execute("INSERT INTO edition (title) VALUES ('Y')")
    c.commit()
    c.close()
    ro = connect_ro(p)
    assert ro.execute("SELECT title FROM edition").fetchone()[0] == "Y"


def test_connect_ro_rejects_writes(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    ro = connect_ro(p)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("INSERT INTO edition (title) VALUES ('Z')")


def test_connect_ro_refuses_an_open_connection(tmp_path):
    conn = init_db(tmp_path / "t.db")
    with pytest.raises(TypeError):
        connect_ro(conn)   # must be a path, not an open connection
