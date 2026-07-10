"""Memoization caches that aren't entities — flat policy-gated repos over the caller's connection.

`classification_cache` (the §6 classify ladder result, keyed by content_hash + classify_version) and
`parsed_toc_cache` (a file's parsed table-of-contents, keyed by file_hash + parse_version) are pure
derived-data caches: no identity, no soft-delete. Each gets a thin repo so the service layer holds no
SQL while the cache stays below the entity model. Reads over RO; writes STAGE (caller commits). See
entity_api_model.md §8.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action


class ClassificationCacheRepo:
    """`acc.classification_cache` — the classify-ladder result memo."""

    def __init__(self, access):
        self._a = access

    def get(self, content_hash: str, classify_version: int):
        """(result_json, confidence, model_rung) for a settled entry, or None."""
        self._a.authorize(Action("classification_cache", "get", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT result_json, confidence, model_rung FROM classification_cache "
            "WHERE content_hash = ? AND classify_version = ?",
            (content_hash, classify_version)).fetchone()

    def put(self, content_hash: str, classify_version: int, result_json: str,
            confidence: float, model_rung: str) -> None:
        """Memoize a classify result (the winner, or the last attempt). Staged; caller commits."""
        self._a.authorize(Action("classification_cache", "put", AccessMode.WRITE))
        self._a.rw.execute(
            "INSERT OR REPLACE INTO classification_cache "
            "(content_hash, classify_version, result_json, confidence, model_rung) "
            "VALUES (?, ?, ?, ?, ?)",
            (content_hash, classify_version, result_json, confidence, model_rung))


class ResolverCacheRepo:
    """`acc.resolver_cache` — the shared external-lookup memo (work/person/edition authority chains).

    Keyed by (query_hash, resolver_version). A miss is `get() is None`; a CACHED-NULL stub (so a
    no-result lookup isn't re-queried) is `get() == (None,)` — callers distinguish the two, so `get`
    returns the raw row tuple or None (the `fetchone()` shape). Writes STAGE; the cache callers issue
    their own `conn.commit()` so an entry persists even if the surrounding op rolls back (desired for
    a cache)."""

    def __init__(self, access):
        self._a = access

    def get(self, query_hash: str, version: int):
        """`(parsed_json,)` if a row exists (parsed_json may be None for a cached-null stub), else
        None (a true miss)."""
        self._a.authorize(Action("resolver_cache", "get", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT parsed_json FROM resolver_cache WHERE query_hash = ? AND resolver_version = ?",
            (query_hash, version)).fetchone()

    def put(self, query_hash: str, version: int, source, parsed_json) -> None:
        """Upsert a cache row (raw_json is always NULL here). Staged; caller commits."""
        self._a.authorize(Action("resolver_cache", "put", AccessMode.WRITE))
        self._a.rw.execute(
            "INSERT OR REPLACE INTO resolver_cache "
            "(query_hash, resolver_version, source, raw_json, parsed_json) VALUES (?, ?, ?, ?, ?)",
            (query_hash, version, source, None, parsed_json))

    def delete_sources(self, sources) -> int:
        """Drop every cache row whose source is in `sources`; returns the rowcount. Staged."""
        self._a.authorize(Action("resolver_cache", "delete_sources", AccessMode.WRITE))
        sources = tuple(sources)
        ph = ",".join("?" * len(sources)) or "NULL"
        return self._a.rw.execute(
            f"DELETE FROM resolver_cache WHERE source IN ({ph})", sources).rowcount


class ParsedTocCacheRepo:
    """`acc.parsed_toc_cache` — a file's parsed table-of-contents memo."""

    def __init__(self, access):
        self._a = access

    def load(self, file_hash: str, parse_version: int):
        """The cached parsed_json for (file_hash, parse_version), or None."""
        self._a.authorize(Action("parsed_toc_cache", "load", AccessMode.READ))
        row = self._a.ro.execute(
            "SELECT parsed_json FROM parsed_toc_cache WHERE file_hash = ? AND parse_version = ?",
            (file_hash, parse_version)).fetchone()
        return row[0] if row and row[0] else None

    def store(self, file_hash: str, parse_version: int, parsed_json: str) -> None:
        """Memoize a file's parsed TOC. Staged; caller commits."""
        self._a.authorize(Action("parsed_toc_cache", "store", AccessMode.WRITE))
        self._a.rw.execute(
            "INSERT OR REPLACE INTO parsed_toc_cache (file_hash, parse_version, parsed_json) "
            "VALUES (?, ?, ?)", (file_hash, parse_version, parsed_json))
