"""Ingest-ignore list — the gateway-bound access surface (`acc.ingest_ignore`).

`ingest_ignore` is the operator's "never surface this file again" set (keyed by path, with the
file_hash kept so the ignore survives a move). A scanned file matching an ignored row by path OR
hash is dropped from classification. Not a catalogue entity — a small keyed state table — so this is
a flat policy-gated repo: reads over RO, writes STAGE on the caller's connection. See
db_store/schema.sql and reconcile.py.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

_RESOURCE = "ingest_ignore"


class IngestIgnoreRepo:
    def __init__(self, access):
        self._a = access

    # ── reads ────────────────────────────────────────────────────────────────────
    def paths(self) -> set:
        """Every ignored path."""
        self._a.authorize(Action(_RESOURCE, "paths", AccessMode.READ))
        return {r[0] for r in self._a.ro.execute(
            "SELECT path FROM ingest_ignore WHERE path IS NOT NULL").fetchall()}

    def hashes(self) -> set:
        """Every ignored file_hash."""
        self._a.authorize(Action(_RESOURCE, "hashes", AccessMode.READ))
        return {r[0] for r in self._a.ro.execute(
            "SELECT file_hash FROM ingest_ignore WHERE file_hash IS NOT NULL").fetchall()}

    # ── writes (staged) ──────────────────────────────────────────────────────────
    def add(self, path: str, file_hash=None, content_hash=None) -> None:
        """Ignore a file (upsert by path; refresh its hashes). Staged; caller commits."""
        self._a.authorize(Action(_RESOURCE, "add", AccessMode.WRITE))
        self._a.rw.execute(
            "INSERT INTO ingest_ignore (path, file_hash, content_hash) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET file_hash = excluded.file_hash, "
            "content_hash = excluded.content_hash", (path, file_hash, content_hash))
