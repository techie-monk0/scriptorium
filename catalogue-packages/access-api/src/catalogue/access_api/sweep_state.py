"""Corpus-sweep persistence — the gateway-bound access surface (`acc.sweep_state`).

The corpus sweep keeps several below-the-entity-model tables, all owned by the sweep and nothing else:
`sweep_state` (a (path, size, mtime, hash) resume CACHE so a re-run re-stats only changed files), the
`raw_extract_cache` + `page_text_cache` extract caches (so a re-chunk doesn't re-OCR), and the
`sweep_problem_log` audit trail. None is a catalogue entity (no identity, no soft-delete) — so this is
a flat policy-gated repo: `.reads` over RO, `.writes` STAGE on the caller's connection (the sweep owns
its transaction). Routing it keeps the SQL in the engine even though the tables are below the entity
model. See entity_api_model.md §8.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

_RESOURCE = "sweep_state"


class _Reads:
    def __init__(self, access):
        self._a = access

    def _read(self, verb: str) -> None:
        self._a.authorize(Action(_RESOURCE, verb, AccessMode.READ))

    def paths(self):
        """Every recorded resume path."""
        self._read("paths")
        return [r[0] for r in self._a.ro.execute("SELECT path FROM sweep_state").fetchall()]

    def unchanged(self, path: str, size: int, mtime: float) -> bool:
        """Whether a resume row exists for this exact (path, size, mtime) — the skip-unchanged gate."""
        self._read("unchanged")
        return self._a.ro.execute(
            "SELECT 1 FROM sweep_state WHERE path = ? AND size = ? AND mtime = ?",
            (str(path), size, mtime)).fetchone() is not None


class _Writes:
    def __init__(self, access):
        self._a = access

    def _write(self, verb: str) -> None:
        self._a.authorize(Action(_RESOURCE, verb, AccessMode.WRITE))

    def delete_path(self, path: str) -> None:
        """Drop one resume row (a moved/relocated file's stale cache). Staged; caller commits."""
        self._write("delete_path")
        self._a.rw.execute("DELETE FROM sweep_state WHERE path = ?", (path,))

    def delete_paths(self, paths) -> int:
        """Drop the resume rows for many paths (the reprocess clear); returns the rowcount. Staged."""
        self._write("delete_path")
        paths = list(paths)
        if not paths:
            return 0
        cur = self._a.rw.execute(
            f"DELETE FROM sweep_state WHERE path IN ({','.join('?' * len(paths))})", paths)
        return cur.rowcount

    def record(self, path: str, size: int, mtime: float, file_hash: str) -> None:
        """Record/refresh a file's resume row (so the next sweep skips it unchanged). Staged."""
        self._write("record")
        self._a.rw.execute(
            "INSERT OR REPLACE INTO sweep_state (path, size, mtime, file_hash, scanned_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)", (str(path), size, mtime, file_hash))

    def cache_extract(self, file_hash: str, extract_version: int, raw_text) -> None:
        """Persist the joined raw extract text per (file_hash, extract_version). Staged."""
        self._write("cache_extract")
        self._a.rw.execute(
            "INSERT OR REPLACE INTO raw_extract_cache (file_hash, extract_version, raw_text) "
            "VALUES (?, ?, ?)", (file_hash, extract_version, raw_text))

    def cache_pages(self, file_hash: str, extract_version: int, page_texts) -> None:
        """Persist per-page text (1-indexed) in page_text_cache; no-op when there is none. Staged."""
        self._write("cache_pages")
        if not page_texts:
            return
        self._a.rw.executemany(
            "INSERT OR REPLACE INTO page_text_cache (file_hash, extract_version, page_no, text) "
            "VALUES (?, ?, ?, ?)",
            [(file_hash, extract_version, i + 1, t) for i, t in enumerate(page_texts)])

    def delete_extract_cache(self, file_hashes) -> int:
        """Drop raw_extract_cache rows for many file_hashes (reprocess clear); returns rowcount. Staged."""
        self._write("delete_extract_cache")
        hashes = list(file_hashes)
        if not hashes:
            return 0
        cur = self._a.rw.execute(
            f"DELETE FROM raw_extract_cache WHERE file_hash IN ({','.join('?' * len(hashes))})", hashes)
        return cur.rowcount

    def log_problem(self, path: str, message: str) -> None:
        """Append a sweep problem (stat/hash/extract failure) to the audit log. Staged."""
        self._write("log_problem")
        self._a.rw.execute(
            "INSERT INTO sweep_problem_log (path, message) VALUES (?, ?)", (str(path), message))


class SweepStateRepo:
    """`.reads` (resume state) + `.writes` (resume rows, extract caches, problem log) over `Access`."""

    def __init__(self, access):
        self.reads = _Reads(access)
        self.writes = _Writes(access)
