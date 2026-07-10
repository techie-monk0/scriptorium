"""Starred editions — the gateway-bound access surface (`acc.starred`).

`starred_edition` is the library-wide set of editions the operator has *starred* (favourited): the
curated Starred home rail and the highlighted star drawn on every cover. It is a plain toggle, not a
soft-delete root — `star` is idempotent, `unstar` HARD-deletes the row, and the FK CASCADEs so a hard
edition delete removes its star too. Reads JOIN to LIVE editions only, so a tombstoned (or recycled —
see the id-reuse hazard notes) id never resurrects a star. This flat policy-gated repo follows the
wishlist convention: writes STAGE on the caller's connection (the route commits); nothing here commits.

Distinct from reader bookmarks/annotations (per-position, inside a book): this marks a whole edition.
See db_store/schema.sql (starred_edition) and catalogue-webui starred routes.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action, NotFound

_RESOURCE = "starred_edition"


class StarredRepo:
    def __init__(self, access):
        self._a = access

    # ── reads (over RO; only LIVE editions, newest-starred first) ──────────────────
    def list(self) -> "list[int]":
        """Starred edition ids, most-recently-starred first. Joined to live editions so a tombstoned
        or recycled id is never returned. The presenter-agnostic source for `GET /api/v1/starred`."""
        self._a.authorize(Action(_RESOURCE, "list", AccessMode.READ))
        rows = self._a.ro.execute(
            "SELECT s.edition_id FROM starred_edition s "
            "JOIN edition e ON e.id = s.edition_id AND e.deleted_at IS NULL "
            "ORDER BY s.starred_at DESC, s.id DESC").fetchall()
        return [r[0] for r in rows]

    def is_starred(self, edition_id: int) -> bool:
        """Whether this edition is currently starred (live editions only)."""
        self._a.authorize(Action(_RESOURCE, "is_starred", AccessMode.READ))
        row = self._a.ro.execute(
            "SELECT 1 FROM starred_edition s "
            "JOIN edition e ON e.id = s.edition_id AND e.deleted_at IS NULL "
            "WHERE s.edition_id = ?", (edition_id,)).fetchone()
        return row is not None

    def count(self) -> int:
        """How many live editions are starred — the Starred rail count."""
        self._a.authorize(Action(_RESOURCE, "count", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT count(*) FROM starred_edition s "
            "JOIN edition e ON e.id = s.edition_id AND e.deleted_at IS NULL").fetchone()[0]

    def fingerprint(self) -> str:
        """A cheap state fingerprint (count + max id + max rev) for the `GET /api/v1/starred` ETag —
        changes whenever any star is added, removed, or re-stamped."""
        self._a.authorize(Action(_RESOURCE, "fingerprint", AccessMode.READ))
        n, max_id, max_rev = self._a.ro.execute(
            "SELECT count(*), COALESCE(max(id), 0), COALESCE(max(rev), 0) FROM starred_edition"
        ).fetchone()
        return f"{n}.{max_id}.{max_rev}"

    # ── writes (staged on the caller's connection; the route commits) ──────────────
    def star(self, edition_id: int) -> None:
        """Star an edition (idempotent — re-starring just bumps `rev`). Raises `NotFound` if the
        edition does not exist or is tombstoned. Staged — the caller commits."""
        self._a.authorize(Action(_RESOURCE, "star", AccessMode.WRITE))
        live = self._a.rw.execute(
            "SELECT 1 FROM edition WHERE id = ? AND deleted_at IS NULL", (edition_id,)).fetchone()
        if live is None:
            raise NotFound(f"edition {edition_id} does not exist")
        self._a.rw.execute(
            "INSERT INTO starred_edition (edition_id) VALUES (?) "
            "ON CONFLICT(edition_id) DO UPDATE SET rev = rev + 1", (edition_id,))

    def unstar(self, edition_id: int) -> None:
        """Un-star an edition. A no-op if it was not starred (the toggle is forgiving). Staged."""
        self._a.authorize(Action(_RESOURCE, "unstar", AccessMode.WRITE))
        self._a.rw.execute("DELETE FROM starred_edition WHERE edition_id = ?", (edition_id,))
