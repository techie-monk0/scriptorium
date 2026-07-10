"""External-tool dependencies — the single access layer for which external tools depend on
which editions.

This module is the *only* code that should touch `edition_external_dependency`. A first-party
external tool (BuddhistLLM's RAG corpus first; the caller's own tools later) calls `claim()` when
it consumes an edition; that sets the "flag", which the `edition_purge_guard` trigger reads to
forbid a hard-delete — so a consumed edition's id + pub_id stay frozen (stability contract S1).
The dependency is MONOTONIC: never cleared, because a citation already emitted to a user can't be
recalled.

Contract for clients (same as scan_ocr): pass an open sqlite3.Connection (from
catalogue.db_store.db.connect / init_db); nothing here commits — the caller owns the transaction.
Call `ensure_schema(conn)` once if you might be on an un-migrated DB (idempotent).

See docs/access/external_tool_dependency_contract.md and citation_edition_contract_plan.md.
"""
from __future__ import annotations

from dataclasses import dataclass

from catalogue.contracts import Resolution   # {status, canonical} — the stability read shape

_CHAIN_BOUND = 64   # max forwarding hops before we call it a cycle (S2: chains terminate)

# Idempotent DDL — mirrors schema.sql exactly, so this module stands alone (tests / a tool's
# claim step don't depend on the catalogue's normal init having run).
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS edition_external_dependency (
  edition_id INTEGER NOT NULL REFERENCES edition(id),
  tool       TEXT NOT NULL,
  corpus     TEXT,
  claimed_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (edition_id, tool)
);
CREATE INDEX IF NOT EXISTS edition_external_dependency_tool_idx
  ON edition_external_dependency(tool);
CREATE TRIGGER IF NOT EXISTS edition_purge_guard
BEFORE DELETE ON edition
WHEN EXISTS (SELECT 1 FROM edition_external_dependency WHERE edition_id = OLD.id)
BEGIN
  SELECT RAISE(ABORT,
    'edition has external-tool dependencies; tombstone (deleted_at), do not hard-delete (stability contract)');
END;
"""


@dataclass(frozen=True)
class Dependency:
    """One tool's dependency on one edition."""
    edition_id: int
    tool: str
    corpus: "str | None"
    claimed_at: "str | None"


def ensure_schema(conn) -> None:
    """Create the dependency table, its index, and the purge-guard trigger — idempotent.
    A no-op on a DB already carrying them (schema.sql / init_db)."""
    conn.executescript(_SCHEMA_SQL)


def claim(conn, *, pub_id: str, tool: str, corpus: "str | None" = None) -> Dependency:
    """Record that `tool` depends on the edition identified by `pub_id`, and flag it against
    hard-delete. Idempotent + monotonic: a re-claim of the same (edition, tool) keeps the row
    and only refreshes `corpus`. Raises ValueError if no edition carries `pub_id`."""
    row = conn.execute("SELECT id FROM edition WHERE pub_id = ?", (pub_id,)).fetchone()
    if row is None:
        raise ValueError(f"no edition with pub_id {pub_id!r}")
    eid = row[0]
    conn.execute(
        "INSERT INTO edition_external_dependency (edition_id, tool, corpus) VALUES (?, ?, ?) "
        "ON CONFLICT(edition_id, tool) DO UPDATE SET corpus = excluded.corpus",
        (eid, tool, corpus))
    return _dep(conn.execute(
        "SELECT edition_id, tool, corpus, claimed_at FROM edition_external_dependency "
        "WHERE edition_id = ? AND tool = ?", (eid, tool)).fetchone())


def is_flagged(conn, edition_id: int, *, tool: "str | None" = None) -> bool:
    """Whether any tool (or a specific `tool`) depends on this edition — i.e. it's un-deletable."""
    if tool is None:
        return conn.execute(
            "SELECT 1 FROM edition_external_dependency WHERE edition_id = ? LIMIT 1",
            (edition_id,)).fetchone() is not None
    return conn.execute(
        "SELECT 1 FROM edition_external_dependency WHERE edition_id = ? AND tool = ? LIMIT 1",
        (edition_id, tool)).fetchone() is not None


def dependents(conn, edition_id: int) -> "list[Dependency]":
    """Every tool that depends on this edition."""
    return [_dep(r) for r in conn.execute(
        "SELECT edition_id, tool, corpus, claimed_at FROM edition_external_dependency "
        "WHERE edition_id = ? ORDER BY tool", (edition_id,)).fetchall()]


def editions_for_tool(conn, tool: str) -> "list[int]":
    """All edition ids `tool` depends on (data segregation is a WHERE on `tool`)."""
    return [r[0] for r in conn.execute(
        "SELECT edition_id FROM edition_external_dependency WHERE tool = ? ORDER BY edition_id",
        (tool,)).fetchall()]


def _dep(row) -> Dependency:
    return Dependency(edition_id=row[0], tool=row[1], corpus=row[2], claimed_at=row[3])


# ── stability reads/writes (S2 forwarding) ──────────────────────────────────────
def resolve(conn, pub_id: str) -> "Resolution | None":
    """Resolve an opaque token to the edition it stands for (stability S2: total resolvability).
    Follows the `superseded_by` chain to the canonical live edition. Returns `Resolution(status,
    canonical)` where `canonical` is the terminal edition's pub_id, or None if `pub_id` was never
    minted. Raises on a dangling pointer or a cycle (both are contract violations)."""
    row = conn.execute(
        "SELECT id, pub_id, deleted_at, superseded_by FROM edition WHERE pub_id = ?",
        (pub_id,)).fetchone()
    if row is None:
        return None
    origin_deleted, origin_sup = row[2], row[3]
    cur_id, cur_pub, sup = row[0], row[1], row[3]
    seen: set[int] = set()
    while sup is not None:
        if cur_id in seen or len(seen) > _CHAIN_BOUND:
            raise ValueError(f"forwarding cycle resolving pub_id {pub_id!r}")
        seen.add(cur_id)
        nxt = conn.execute(
            "SELECT id, pub_id, superseded_by FROM edition WHERE id = ?", (sup,)).fetchone()
        if nxt is None:
            raise ValueError(f"dangling superseded_by ({sup}) resolving pub_id {pub_id!r}")
        cur_id, cur_pub, sup = nxt
    status = ("superseded" if origin_sup is not None
              else "withdrawn" if origin_deleted is not None
              else "active")
    return Resolution(status=status, canonical=cur_pub)


def supersede(conn, *, old_pub_id: str, new_pub_id: str) -> None:
    """Forward `old` to `new`: tombstone the old edition and set its `superseded_by` to the new
    edition's id. The identity-forwarding primitive a merge uses for a CITED loser instead of a
    hard delete, so resolve(old) lands on new. Raises if either token is unknown, the two are the
    same, or the link would create a cycle."""
    o = conn.execute("SELECT id FROM edition WHERE pub_id = ?", (old_pub_id,)).fetchone()
    n = conn.execute("SELECT id FROM edition WHERE pub_id = ?", (new_pub_id,)).fetchone()
    if o is None or n is None:
        raise ValueError(f"unknown pub_id in supersede({old_pub_id!r} -> {new_pub_id!r})")
    old_id, new_id = o[0], n[0]
    if old_id == new_id:
        raise ValueError("cannot supersede an edition by itself")
    # Cycle guard: `new` must not already forward (transitively) back to `old`.
    cur, hops = new_id, 0
    while cur is not None:
        if cur == old_id:
            raise ValueError(f"supersede would create a cycle ({old_id} <-> {new_id})")
        hops += 1
        if hops > _CHAIN_BOUND:
            raise ValueError("forwarding chain too long / cyclic")
        r = conn.execute("SELECT superseded_by FROM edition WHERE id = ?", (cur,)).fetchone()
        cur = r[0] if r else None
    conn.execute(
        "UPDATE edition SET superseded_by = ?, deleted_at = COALESCE(deleted_at, datetime('now')) "
        "WHERE id = ?", (new_id, old_id))
