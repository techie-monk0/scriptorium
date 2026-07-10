"""Regression guard: no raw SQL above the data layer.

The 2026-06-27 reorg routed every application layer (`services`, `webui`, `cli`) through the
`access_api` engine — all SQL now lives in `access-api` (the engine) + `db_store` (the data layer).
This test LOCKS THAT IN: it scans every `.py` under the three application packages and fails if any
line issues a raw `.execute(` / `.executemany(`, EXCEPT for a small, explicitly-justified allowlist
of below-the-data-layer code (connection shims, DB-file operations, standalone export-DB artifacts,
transaction-control primitives, and PRAGMAs).

If this test fails, you've added raw SQL to a service / route / CLI — route it through `acc.*`
(`system_conn(db)` / `system_access(db_path)`) instead. If the new site is itself a legitimate
below-data-layer exception, add it to `_ALLOWED_FILES` (with a reason) or `_ALLOWED_LINE_TOKENS`.
"""
from __future__ import annotations

import pathlib
import re

_REPO = pathlib.Path(__file__).resolve().parent.parent

# The three application layers that must hold NO raw SQL (their SQL goes through the engine).
_SCAN_ROOTS = [
    _REPO / "catalogue-packages" / "services" / "src" / "catalogue" / "services",
    _REPO / "catalogue-webui" / "src" / "catalogue" / "webui",
    _REPO / "catalogue-cli" / "src" / "catalogue" / "cli",
]

_SQL_CALL = re.compile(r"\.execute(?:many)?\s*\(")

# Files where raw SQL is legitimately BELOW the data layer (with the reason). These are not service
# logic — they are the data layer / DB-file ops / standalone artifacts the engine deliberately
# doesn't model. Keyed by path suffix (".../<pkg>/<file>").
_ALLOWED_FILES = {
    "services/staging.py": "StagingConn — a connection shim that wraps/replays staged write specs",
    "services/sandbox.py": "DB-file ops on a forked copy (wal_checkpoint / VACUUM)",
    "services/person_dedup.py": "CREATE INDEX DDL (index maintenance, below the entity model)",
    "services/export_content_index.py": "writes a standalone export-DB artifact via new_export_db",
    "services/export_replica.py": "writes a standalone replica-DB artifact via new_export_db",
    "cli/backup.py": "DB-file backup (VACUUM INTO) + a verification connect() count",
    "cli/build_content_index.py": "builds a standalone content-index DB artifact (not the catalogue)",
}

# Line-level tokens that are below-the-data-layer regardless of file: PRAGMAs and explicit
# transaction-control primitives the engine can't express (the dry-run/apply savepoint dance).
_ALLOWED_LINE_TOKENS = (
    "PRAGMA",
    "SAVEPOINT",
    "ROLLBACK TO",
    "RELEASE SAVEPOINT",
)


def _suffix(path: pathlib.Path) -> str:
    # ".../catalogue/<pkg>/<...>/<file>.py" → "<pkg>/<file>.py" (pkg = services|webui|cli)
    parts = path.parts
    for pkg in ("services", "webui", "cli"):
        if pkg in parts:
            i = parts.index(pkg)
            return f"{pkg}/{parts[-1]}"
    return path.name


def test_no_raw_sql_in_application_layers():
    violations: list[str] = []
    for root in _SCAN_ROOTS:
        assert root.is_dir(), f"scan root missing: {root}"
        for py in sorted(root.rglob("*.py")):
            suffix = _suffix(py)
            if suffix in _ALLOWED_FILES:
                continue
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if not _SQL_CALL.search(line):
                    continue
                stripped = line.lstrip()
                if stripped.startswith("#"):          # a comment mentioning .execute(...)
                    continue
                if any(tok in line for tok in _ALLOWED_LINE_TOKENS):
                    continue
                violations.append(f"{suffix}:{lineno}: {stripped}")

    assert not violations, (
        "Raw SQL found above the data layer — route it through the access-API engine "
        "(`system_conn(db)` / `system_access`), or, if it is a legitimate below-data-layer "
        "exception, add it to the allowlist in this test with a reason:\n  "
        + "\n  ".join(violations)
    )
