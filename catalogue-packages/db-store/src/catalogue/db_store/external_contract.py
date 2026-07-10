"""The catalogue's external read-contract — a *versioned, language-neutral descriptor* of the
stable surface external tools (BuddhistLLM, ocr_pipeline) read for edition identity.

The catalogue is the system-of-record; downstream tools must stay consistent with it WITHOUT
being coupled to its code. So the contract is published two ways, both language-neutral:

  * ``external_read_contract.json`` — the machine-readable spec of what each version guarantees
    (the read view + its columns, the resolve columns, the S1–S3 stability guarantees); and
  * ``schema_meta.external_read_contract_version`` — stamped into every DB, so a consumer learns
    the version the *live* DB actually provides with one SELECT over a connection it already holds.

A consumer's handshake is ~5 lines it owns (no import of this module): read the DB version, assert
it is ≥ the version the consumer was built for, and confirm the columns it needs are present. This
module is the *provider* side — ``verify()`` proves the running DB actually honours the published
descriptor, so the catalogue can never ship a descriptor that lies. See
docs/access/external_tool_dependency_contract.md.
"""
from __future__ import annotations

import json
from pathlib import Path

CONTRACT_PATH = Path(__file__).parent / "external_read_contract.json"


def descriptor() -> dict:
    """The published contract descriptor (parsed ``external_read_contract.json``)."""
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


CONTRACT_VERSION: int = descriptor()["version"]
_META_KEY = "external_read_contract_version"


def db_contract_version(conn) -> "int | None":
    """The external-read-contract version the given DB advertises
    (``schema_meta.external_read_contract_version``), or ``None`` if unstamped."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (_META_KEY,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _columns(conn, relation: str) -> "set[str]":
    return {r[1] for r in conn.execute(f"PRAGMA table_info({relation})").fetchall()}


def verify(conn) -> "list[str]":
    """Provider-side truthfulness check: does ``conn`` actually honour the published descriptor?

    Confirms (a) the DB stamps the descriptor's version, and (b) every column the descriptor
    declares — for the read view(s) and the resolve table — really exists on the live relation.
    This closes the gap the generic schema guard leaves open (it fingerprints view *names*, not
    view *columns*), so a future edit that drops ``pub_id`` from ``v_holding_files`` fails loudly
    instead of silently breaking every external consumer. Returns human-readable mismatches;
    an empty list means conformant."""
    d = descriptor()
    problems: list[str] = []

    db_ver = db_contract_version(conn)
    if db_ver != d["version"]:
        problems.append(
            f"DB stamps external_read_contract_version={db_ver!r}, descriptor is {d['version']!r}")

    for view, spec in d.get("views", {}).items():
        have = _columns(conn, view)
        if not have:
            problems.append(f"view {view!r} is absent from the DB")
            continue
        missing = [c for c in spec.get("columns", {}) if c not in have]
        if missing:
            problems.append(f"view {view!r} is missing declared columns: {missing}")

    resolve = d.get("resolve")
    if resolve:
        table = resolve["table"]
        have = _columns(conn, table)
        missing = [c for c in resolve.get("columns", {}) if c not in have]
        if missing:
            problems.append(f"resolve table {table!r} is missing declared columns: {missing}")

    return problems
