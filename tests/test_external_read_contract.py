"""external_read_contract — the versioned, language-neutral descriptor the catalogue publishes
so out-of-process consumers (../ocr_pipeline, ../BuddhistLLM) verify identity-surface consistency
WITHOUT importing catalogue code.

These are the *provider-side* guarantees: the descriptor cannot lie about the live DB, the DB
advertises the descriptor's version, and the descriptor stays in lockstep with the view whose
column set `test_holding_files_view.py` already pins. See
docs/access/external_tool_dependency_contract.md.
"""
from __future__ import annotations

from catalogue.db_store import (
    EXTERNAL_READ_CONTRACT_VERSION,
    db_contract_version,
    external_read_contract,
    init_db,
    verify_external_read_contract,
)
from catalogue.db_store import external_contract

# Kept in sync with test_holding_files_view.py::GUARANTEED — the two must never disagree.
GUARANTEED = ["edition_id", "pub_id", "file_path", "content_hash", "text_status", "provenance_kind"]


def test_db_advertises_the_descriptor_version(tmp_path):
    conn = init_db(tmp_path / "t.db")
    assert db_contract_version(conn) == EXTERNAL_READ_CONTRACT_VERSION
    assert EXTERNAL_READ_CONTRACT_VERSION == external_read_contract()["version"]


def test_fresh_db_conforms_to_published_descriptor(tmp_path):
    conn = init_db(tmp_path / "t.db")
    assert verify_external_read_contract(conn) == []


def test_descriptor_columns_match_the_live_view(tmp_path):
    conn = init_db(tmp_path / "t.db")
    declared = list(external_read_contract()["views"]["v_holding_files"]["columns"])
    assert declared == GUARANTEED, "descriptor must track the pinned v_holding_files column set"
    live = [r[1] for r in conn.execute("PRAGMA table_info(v_holding_files)").fetchall()]
    assert set(declared) <= set(live)


def test_resolve_columns_exist_on_edition(tmp_path):
    conn = init_db(tmp_path / "t.db")
    resolve = external_read_contract()["resolve"]
    live = {r[1] for r in conn.execute(f"PRAGMA table_info({resolve['table']})").fetchall()}
    assert set(resolve["columns"]) <= live


def test_required_is_a_subset_of_declared():
    d = external_read_contract()
    vhf = d["views"]["v_holding_files"]
    assert set(vhf["required"]) <= set(vhf["columns"])
    assert d["identity_key"] in vhf["columns"]
    assert d["version_field"] in vhf["columns"]


def test_verify_catches_a_dropped_contract_column(tmp_path):
    # Simulate the drift the generic schema guard misses: rebuild the view WITHOUT pub_id.
    conn = init_db(tmp_path / "t.db")
    conn.execute("DROP VIEW v_holding_files")
    conn.execute(
        "CREATE VIEW v_holding_files AS "
        "SELECT h.edition_id, h.file_path, h.content_hash FROM holding h "
        "WHERE h.file_path IS NOT NULL")
    problems = verify_external_read_contract(conn)
    assert any("pub_id" in p for p in problems)


def test_verify_catches_a_version_mismatch(tmp_path):
    conn = init_db(tmp_path / "t.db")
    conn.execute(
        "UPDATE schema_meta SET value = '999' WHERE key = ?",
        (external_contract._META_KEY,))
    assert any("version" in p.lower() for p in verify_external_read_contract(conn))
