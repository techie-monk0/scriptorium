"""edition.pub_id — the stable, opaque external identity + its S1 guarantees (write-once, no
reuse), enforced by DB triggers/index below the ORM/service layer so the ~906 legacy raw-SQL
sites can't violate it. Part of the catalogue ↔ external-tool stability contract (S1–S3); see
docs/access/external_tool_dependency_contract.md and citation_edition_contract_plan.md §3.
"""
from __future__ import annotations

import re

import pytest

from catalogue.db_store import init_db

_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _new_edition(conn, title="Bk"):
    eid = conn.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    return eid, conn.execute("SELECT pub_id FROM edition WHERE id=?", (eid,)).fetchone()[0]


# ── minting ─────────────────────────────────────────────────────────────────────
def test_insert_mints_a_v4_uuid_pub_id(tmp_path):
    conn = init_db(tmp_path / "t.db")
    _, tok = _new_edition(conn)
    assert tok and _UUID4.match(tok), f"expected a v4 UUID, got {tok!r}"


def test_every_edition_gets_a_distinct_token(tmp_path):
    conn = init_db(tmp_path / "t.db")
    toks = {_new_edition(conn, f"Bk{i}")[1] for i in range(50)}
    assert len(toks) == 50, "pub_ids must be unique across editions"


def test_explicit_pub_id_on_insert_is_honored(tmp_path):
    # The mint trigger fires only WHEN NEW.pub_id IS NULL, so a caller may pre-set it.
    conn = init_db(tmp_path / "t.db")
    fixed = "11111111-1111-4111-8111-111111111111"
    eid = conn.execute(
        "INSERT INTO edition (title, pub_id) VALUES ('X', ?)", (fixed,)).lastrowid
    assert conn.execute("SELECT pub_id FROM edition WHERE id=?", (eid,)).fetchone()[0] == fixed


# ── S1: write-once (immutability) ─────────────────────────────────────────────────
def test_pub_id_is_write_once(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid, tok = _new_edition(conn)
    with pytest.raises(Exception, match="write-once"):
        conn.execute("UPDATE edition SET pub_id=? WHERE id=?", ("22222222-2222-4222-8222-222222222222", eid))
    # unchanged
    assert conn.execute("SELECT pub_id FROM edition WHERE id=?", (eid,)).fetchone()[0] == tok


def test_setting_pub_id_to_null_is_rejected(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid, _ = _new_edition(conn)
    with pytest.raises(Exception, match="write-once"):
        conn.execute("UPDATE edition SET pub_id=NULL WHERE id=?", (eid,))


def test_non_pub_id_updates_are_unaffected(tmp_path):
    # The immutability trigger is scoped to UPDATE OF pub_id — ordinary edits still work.
    conn = init_db(tmp_path / "t.db")
    eid, tok = _new_edition(conn)
    conn.execute("UPDATE edition SET title='renamed', tradition='Gelug' WHERE id=?", (eid,))
    assert conn.execute("SELECT title, pub_id FROM edition WHERE id=?", (eid,)).fetchone() == ("renamed", tok)


# ── S1: no reuse (uniqueness) ─────────────────────────────────────────────────────
def test_two_editions_cannot_share_a_token(tmp_path):
    conn = init_db(tmp_path / "t.db")
    _, tok = _new_edition(conn)
    with pytest.raises(Exception):  # UNIQUE index violation
        conn.execute("INSERT INTO edition (title, pub_id) VALUES ('dup', ?)", (tok,))


def test_a_tombstoned_editions_token_is_not_reissued(tmp_path):
    # Soft-delete freezes the row (id + pub_id kept), so a later edition never inherits it.
    conn = init_db(tmp_path / "t.db")
    eid, tok = _new_edition(conn)
    conn.execute("UPDATE edition SET deleted_at='2026-01-01' WHERE id=?", (eid,))
    _, tok2 = _new_edition(conn, "later")
    assert tok2 != tok
    # the frozen token still points at the original (now-tombstoned) row
    assert conn.execute("SELECT id FROM edition WHERE pub_id=?", (tok,)).fetchone()[0] == eid


# ── the migration is idempotent for pub_id too ────────────────────────────────────
def test_reopen_keeps_tokens_stable(tmp_path):
    p = tmp_path / "t.db"
    conn = init_db(p)
    eid, tok = _new_edition(conn)
    conn.commit(); conn.close()
    conn2 = init_db(p)   # re-runs _migrate (mint trigger IF NOT EXISTS, backfill finds no NULLs)
    assert conn2.execute("SELECT pub_id FROM edition WHERE id=?", (eid,)).fetchone()[0] == tok


# ── durability canary ("forever"): a token minted under an older schema still resolves ─────
# A committed fixture DB carrying a KNOWN pub_id. Open a copy through TODAY's init_db (every
# migration runs); the token must be unchanged and still resolve. A future migration that churns
# or drops pub_ids breaks THIS test — which is the whole point. See
# docs/access/external_tool_dependency_contract.md (durability canary) and citation_edition_contract_plan.md §3.
_CANARY_PUB_ID = "ca4a1e00-0000-4000-8000-000000000001"


def test_pub_id_durability_canary(tmp_path):
    import shutil
    from pathlib import Path

    from catalogue.access_api import external_deps as X

    src = Path(__file__).parent / "fixtures" / "pub_id_canary.db"
    dst = tmp_path / "canary.db"
    shutil.copy(src, dst)
    conn = init_db(dst)    # migrations run forward on the old fixture
    r = X.resolve(conn, _CANARY_PUB_ID)
    assert r is not None, "the canary pub_id vanished — a migration churned pub_ids"
    assert r.canonical == _CANARY_PUB_ID and r.status == "active"
    assert conn.execute(
        "SELECT title FROM edition WHERE pub_id=?", (_CANARY_PUB_ID,)).fetchone()[0] == "Canary Edition"
