"""Section-analysis cache — the gateway-bound access surface (`acc.section_cache`).

`section_cache` memoizes a book's section/structure analysis keyed by (file_hash, section_version) so
a re-run skips the expensive re-extract + per-section peek. A pure cache (not an entity). It is
SELF-BOOTSTRAPPING: `store` issues the `CREATE TABLE IF NOT EXISTS` itself (a real create on the
direct path; a JOURNALED create under the staging shim — replayed before the row insert), and `load`
tolerates the table's absence as a cache miss. So this stays a flat repo over the caller's connection
(`system_conn`, so RW == the caller's conn → the create journals correctly under staging).
See process.py and entity_api_model.md §8.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

_RESOURCE = "section_cache"
_DDL = (
    "CREATE TABLE IF NOT EXISTS section_cache ("
    "  file_hash TEXT NOT NULL,"
    "  section_version INTEGER NOT NULL,"
    "  result_json TEXT,"
    "  created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
    "  PRIMARY KEY (file_hash, section_version))"
)


class _Reads:
    def __init__(self, access):
        self._a = access

    def load(self, file_hash: str, section_version: int):
        """The cached result_json for (file_hash, section_version), or None — including when the
        table does not exist yet (first run before any store created it → cache miss)."""
        self._a.authorize(Action(_RESOURCE, "load", AccessMode.READ))
        try:
            row = self._a.ro.execute(
                "SELECT result_json FROM section_cache "
                "WHERE file_hash = ? AND section_version = ?",
                (file_hash, section_version)).fetchone()
        except Exception:
            return None        # no such table yet → cache miss, recompute
        return row[0] if row and row[0] else None


class _Writes:
    def __init__(self, access):
        self._a = access

    def store(self, file_hash: str, section_version: int, result_json: str) -> None:
        """Upsert the cached analysis. Issues the self-bootstrapping CREATE first (real on the direct
        path; journaled+replayed under the staging shim). Staged; caller commits."""
        self._a.authorize(Action(_RESOURCE, "store", AccessMode.WRITE))
        self._a.rw.execute(_DDL)
        self._a.rw.execute(
            "INSERT OR REPLACE INTO section_cache (file_hash, section_version, result_json) "
            "VALUES (?, ?, ?)", (file_hash, section_version, result_json))


class SectionCacheRepo:
    """`.reads.load` + `.writes.store` over a bound `Access` — a self-bootstrapping cache repo."""

    def __init__(self, access):
        self.reads = _Reads(access)
        self.writes = _Writes(access)
