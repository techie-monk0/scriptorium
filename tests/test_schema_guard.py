"""Schema-conformance guard (catalogue/db.py) + the regression that motivated it.

The bug: a long-lived DB predated `person.harvest_incomplete`. `schema.sql`
declared the column (so every freshly-built test DB had it), but the live DB did
not, so `verify.bind_person` wrote to a missing column, threw, and the bind was
silently rolled back. The whole test suite missed it because every test builds
its DB from current `schema.sql` — no test ever exercised an OLDER DB that
`_migrate` must bring forward. These do.
"""
from __future__ import annotations

import pytest

from catalogue.db_store import (
    SchemaDriftError, assert_schema_current, connect, expected_schema, init_db,
    schema_drift, schema_is_current,
)


def _legacy_person_db(path):
    """A DB whose `person` table is the pre-harvest_incomplete / pre-
    verification_status shape, exactly as a DB created before those columns
    existed would look. init_db's CREATE TABLE IF NOT EXISTS leaves this table
    in place, so only `_migrate` can bring it forward."""
    conn = connect(path)
    conn.executescript(
        "CREATE TABLE person ("
        "  id INTEGER PRIMARY KEY, primary_name TEXT NOT NULL, role_hint TEXT,"
        "  origin TEXT, dates TEXT, external_id TEXT);")
    conn.commit()
    conn.close()


# ── the reference + the diff ─────────────────────────────────────────────────
def test_expected_schema_has_the_disputed_column():
    es = expected_schema()
    assert "harvest_incomplete" in es["tables"]["person"]
    assert "verification_status" in es["tables"]["person"]


def test_fresh_db_is_conformant(tmp_path):
    conn = init_db(tmp_path / "fresh.db")
    assert schema_is_current(conn)
    assert_schema_current(conn)                   # no raise


def test_schema_drift_names_the_missing_column(tmp_path):
    init_db(tmp_path / "x.db").close()
    conn = connect(tmp_path / "x.db")
    conn.execute("ALTER TABLE holding DROP COLUMN notes")
    conn.commit()
    drift = schema_drift(conn)
    assert "notes" in drift["missing_columns"].get("holding", [])
    with pytest.raises(SchemaDriftError) as e:
        assert_schema_current(conn)
    assert "notes" in str(e.value)


# ── the regression: an OLD DB self-heals through init_db ─────────────────────
def test_init_db_migrates_legacy_person_forward(tmp_path):
    p = tmp_path / "legacy.db"
    _legacy_person_db(p)
    # Before migration: the column the bind writes to is absent.
    pre = connect(p)
    cols = {r[1] for r in pre.execute("PRAGMA table_info(person)")}
    assert "harvest_incomplete" not in cols
    pre.close()
    # init_db must bring it forward AND pass its own conformance assertion.
    conn = init_db(p)                             # raises if migrate is incomplete
    cols = {r[1] for r in conn.execute("PRAGMA table_info(person)")}
    assert {"harvest_incomplete", "verification_status"} <= cols


def test_bind_persists_on_a_migrated_legacy_db(tmp_path):
    """End-to-end: the exact failure. On a legacy DB brought forward by init_db,
    binding a person must actually persist (the write used to vanish)."""
    from catalogue.services.verify import bind_person
    p = tmp_path / "legacy2.db"
    _legacy_person_db(p)
    conn = init_db(p)
    pid = conn.execute(
        "INSERT INTO person (primary_name) VALUES ('Tsong-kha-pa')").lastrowid
    conn.commit()
    assert bind_person(conn, pid, "bdr:P64", "Tsong-kha-pa") is True
    # re-read through a FRESH connection: the change is on disk, not just in-session
    chk = connect(p)
    row = chk.execute("SELECT external_id, verification_status, harvest_incomplete "
                      "FROM person WHERE id = ?", (pid,)).fetchone()
    assert row == ("bdr:P64", "verified", 0)


# ── integrity surface reports drift (it used to silently skip it) ────────────
def test_check_integrity_reports_schema_drift(tmp_path):
    from catalogue.db_store import integrity
    init_db(tmp_path / "i.db").close()
    conn = connect(tmp_path / "i.db")
    conn.execute("ALTER TABLE holding DROP COLUMN notes")
    conn.commit()
    rep = integrity.check_integrity(conn)
    assert rep["ok"] is False
    assert any("schema drift" in e["check"] for e in rep["errors"])
