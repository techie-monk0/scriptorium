"""v_holding_files — the stable external read-contract (reorg Phase 2).

Out-of-process consumers (../ocr_pipeline, ../BuddhistLLM) read THIS view, never the base
`holding` table, so internal schema churn can't break them. Unit: the view's shape and
filter. System: the actual query shapes those consumers use, end-to-end through init_db.
See docs/access/entity_api_model.md §7.
"""
from __future__ import annotations

from catalogue.db_store import init_db

# The external contract grows ADDITIVELY: provenance_kind joined in with the v4 digitization
# work; pub_id (the stable opaque edition identity, v11) joined from edition for the stability
# contract (S1–S3, see docs/access/external_tool_dependency_contract.md). Consumers selecting the
# original columns are unaffected. Pinned exactly so an *accidental* drift is still caught — an
# intentional addition updates this list.
GUARANTEED = ["edition_id", "pub_id", "file_path", "content_hash", "text_status", "provenance_kind"]


def _seed(conn):
    eid = conn.execute("INSERT INTO edition (title) VALUES ('Bk')").lastrowid
    conn.execute(
        "INSERT INTO holding (edition_id, file_path, content_hash, text_status) "
        "VALUES (?, '/lib/a.pdf', 't:abc', 'ocr_good')", (eid,))
    # a holding with no file_path must NOT surface (e.g. a physical/inbox row)
    conn.execute(
        "INSERT INTO holding (edition_id, file_path, text_status) "
        "VALUES (?, NULL, 'none')", (eid,))
    conn.commit()
    return eid


# ── unit ──────────────────────────────────────────────────────────────────────
def test_view_exposes_exactly_the_guaranteed_columns(tmp_path):
    conn = init_db(tmp_path / "t.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(v_holding_files)").fetchall()]
    assert cols == GUARANTEED, "the external contract's column set/order must not drift"


def test_view_excludes_holdings_without_a_file_path(tmp_path):
    conn = init_db(tmp_path / "t.db")
    _seed(conn)
    assert [r[0] for r in conn.execute("SELECT file_path FROM v_holding_files")] == ["/lib/a.pdf"]


def test_view_present_on_a_reopened_existing_db(tmp_path):
    # Idempotent + reaches an existing DB on next open (schema.sql is executescript'd each init).
    p = tmp_path / "t.db"
    init_db(p).close()
    conn = init_db(p)
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='view' AND name='v_holding_files'"
    ).fetchone()[0] == 1


# ── system (the real consumer query shapes) ─────────────────────────────────────
def test_external_consumer_queries_run_against_the_view(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid = _seed(conn)
    # ../ocr_pipeline (reocr_run.py / audit_born_digital.py)
    assert conn.execute(
        "SELECT DISTINCT file_path FROM v_holding_files WHERE edition_id=?", (eid,)
    ).fetchall() == [("/lib/a.pdf",)]
    # ../BuddhistLLM (RAG ingest)
    assert conn.execute(
        "SELECT edition_id, file_path, content_hash, text_status FROM v_holding_files"
    ).fetchone() == (eid, "/lib/a.pdf", "t:abc", "ocr_good")
