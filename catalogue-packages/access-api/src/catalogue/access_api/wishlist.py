"""Wishlist — the gateway-bound access surface (`acc.wishlist`).

`wishlist_item` is the library-wide list of books WANTED but not yet owned (§wishlist). It is kept
OUT of the edition graph (so catalogue search/browse/replica keep meaning "books I own") until the
book is acquired, when it converts to a real edition+holding. Unlike `capture_staging` it IS a
soft-deletable root with a `rev` counter, so this flat policy-gated repo honors both: reads run over
RO and filter tombstones via `v_live_wishlist_item`; writes STAGE on the caller's connection (the
route commits) and bump `rev` for optimistic concurrency. Resolution of the raw input into the
snapshot is a SERVICE concern (catalogue.services.wishlist_resolve), not here — this repo only
persists what it is handed. See db_store/schema.sql and catalogue-webui wishlist routes.
"""
from __future__ import annotations

import json as _json

from catalogue.contracts import AccessMode, Action, NotFound, StaleWrite, WishlistItem

_RESOURCE = "wishlist_item"

# Full column list in DTO order — one source of truth for SELECT + row mapping.
_COLS = (
    "id", "source", "status", "raw_isbn", "raw_title", "raw_author", "raw_cip_text",
    "resolved_title", "resolved_subtitle", "resolved_authors", "resolved_publisher",
    "resolved_year", "resolved_isbn", "ol_work_key", "lccn", "cover_url", "candidates_json",
    "matched_edition_id", "priority", "notes", "added_at", "updated_at", "acquired_at", "rev",
)
_SELECT = f"SELECT {', '.join(_COLS)} FROM v_live_wishlist_item"

# Snapshot fields a resolve may set, mapped DTO-key → DB column.
_SNAPSHOT_MAP = {
    "title": "resolved_title", "subtitle": "resolved_subtitle", "publisher": "resolved_publisher",
    "year": "resolved_year", "isbn": "resolved_isbn", "ol_work_key": "ol_work_key",
    "lccn": "lccn", "cover_url": "cover_url", "matched_edition_id": "matched_edition_id",
}


def _row_to_item(row) -> WishlistItem:
    """Map a `_COLS`-ordered row to a `WishlistItem`, decoding the JSON authors/candidates blobs."""
    d = dict(zip(_COLS, row))
    authors = _json.loads(d["resolved_authors"]) if d["resolved_authors"] else []
    candidates = _json.loads(d["candidates_json"]) if d["candidates_json"] else []
    return WishlistItem(
        id=d["id"], source=d["source"], status=d["status"], raw_isbn=d["raw_isbn"],
        raw_title=d["raw_title"], raw_author=d["raw_author"], raw_cip_text=d["raw_cip_text"],
        title=d["resolved_title"], subtitle=d["resolved_subtitle"], authors=tuple(authors),
        publisher=d["resolved_publisher"], year=d["resolved_year"], isbn=d["resolved_isbn"],
        ol_work_key=d["ol_work_key"], lccn=d["lccn"], cover_url=d["cover_url"],
        candidates=tuple(candidates), matched_edition_id=d["matched_edition_id"],
        priority=d["priority"], notes=d["notes"], added_at=d["added_at"],
        updated_at=d["updated_at"], acquired_at=d["acquired_at"], rev=d["rev"])


class WishlistRepo:
    def __init__(self, access):
        self._a = access

    # ── reads (over RO, tombstones hidden by v_live_wishlist_item) ─────────────────
    def list(self, *, status: "str | None" = None, limit: int = 1000) -> "list[WishlistItem]":
        """Live wishlist items, newest first (a NULL priority sorts last). `status` filters to one
        resolution state. The presenter-agnostic source for `GET /api/v1/wishlist`."""
        self._a.authorize(Action(_RESOURCE, "list", AccessMode.READ))
        where, args = [], []
        if status is not None:
            where.append("status = ?"); args.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._a.ro.execute(
            f"{_SELECT}{clause} ORDER BY priority IS NULL, priority, added_at DESC, id DESC LIMIT ?",
            (*args, limit)).fetchall()
        return [_row_to_item(r) for r in rows]

    def get(self, item_id: int) -> "WishlistItem | None":
        """One live wishlist item, or None (also None when soft-deleted — it's read via the view)."""
        self._a.authorize(Action(_RESOURCE, "get", AccessMode.READ))
        row = self._a.ro.execute(f"{_SELECT} WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def count(self) -> int:
        """How many live wishlist items — the wishlist nav badge."""
        self._a.authorize(Action(_RESOURCE, "count", AccessMode.READ))
        return self._a.ro.execute("SELECT count(*) FROM v_live_wishlist_item").fetchone()[0]

    def fingerprint(self) -> str:
        """A cheap state fingerprint (count + max id + max rev + latest mutation time) for the
        `GET /api/v1/wishlist` ETag — changes whenever any live item is added/updated/removed."""
        self._a.authorize(Action(_RESOURCE, "fingerprint", AccessMode.READ))
        n, max_id, max_rev, latest = self._a.ro.execute(
            "SELECT count(*), COALESCE(max(id), 0), COALESCE(max(rev), 0), "
            "       COALESCE(max(COALESCE(updated_at, added_at)), '') FROM v_live_wishlist_item"
        ).fetchone()
        return f"{n}.{max_id}.{max_rev}.{latest}"

    def match(self, *, isbn: "str | None" = None, ol_work_key: "str | None" = None,
              title: "str | None" = None) -> "WishlistItem | None":
        """The live, not-yet-acquired wishlist item this acquisition matches (by ISBN, then
        OpenLibrary work key, then folded title), or None — the capture acquisition-loop lookup."""
        self._a.authorize(Action(_RESOURCE, "match", AccessMode.READ))
        base = f"{_SELECT} WHERE status != 'acquired' AND "
        if isbn:
            r = self._a.ro.execute(base + "resolved_isbn = ? ORDER BY id LIMIT 1", (isbn,)).fetchone()
            if r:
                return _row_to_item(r)
        if ol_work_key:
            r = self._a.ro.execute(
                base + "ol_work_key = ? ORDER BY id LIMIT 1", (ol_work_key,)).fetchone()
            if r:
                return _row_to_item(r)
        if title:
            key = " ".join(title.split()).casefold()
            r = self._a.ro.execute(
                base + "lower(trim(resolved_title)) = ? ORDER BY id LIMIT 1", (key,)).fetchone()
            if r:
                return _row_to_item(r)
        return None

    # ── writes (staged on the caller's connection; the route commits) ──────────────
    def add(self, *, source: str, raw_isbn=None, raw_title=None, raw_author=None,
            raw_cip_text=None, status: str = "unresolved", snapshot: "dict | None" = None) -> int:
        """Insert a wishlist item from its raw input + (optional) resolved snapshot; returns the new
        id. Staged — the caller commits."""
        self._a.authorize(Action(_RESOURCE, "add", AccessMode.WRITE))
        cols = {"source": source, "status": status, "raw_isbn": raw_isbn, "raw_title": raw_title,
                "raw_author": raw_author, "raw_cip_text": raw_cip_text}
        cols.update(self._snapshot_cols(snapshot or {}))
        keys = list(cols)
        cur = self._a.rw.execute(
            f"INSERT INTO wishlist_item ({', '.join(keys)}) "
            f"VALUES ({', '.join('?' * len(keys))})", tuple(cols[k] for k in keys))
        return cur.lastrowid

    def resolve(self, item_id: int, snapshot: dict, status: str,
                *, expected_rev: "int | None" = None) -> None:
        """Write a resolved snapshot + new status onto an item (the resolver's write-back). Staged."""
        self._a.authorize(Action(_RESOURCE, "resolve", AccessMode.WRITE))
        cols = self._snapshot_cols(snapshot)
        cols["status"] = status
        self._update(item_id, cols, expected_rev)

    def update(self, item_id: int, *, notes=None, priority=None, status=None,
               expected_rev: "int | None" = None) -> None:
        """Edit operator-set fields (notes / priority / status). Only non-None args are written.
        Staged — the caller commits."""
        self._a.authorize(Action(_RESOURCE, "update", AccessMode.WRITE))
        cols = {}
        if notes is not None:
            cols["notes"] = notes
        if priority is not None:
            cols["priority"] = priority
        if status is not None:
            cols["status"] = status
        if not cols:
            return
        self._update(item_id, cols, expected_rev)

    def mark_acquired(self, item_id: int, edition_id: int,
                      *, expected_rev: "int | None" = None) -> None:
        """Close out a wishlist item the catalogue just gained: status='acquired', stamp the time and
        the fulfilling edition. The capture acquisition-loop write. Staged."""
        self._a.authorize(Action(_RESOURCE, "mark_acquired", AccessMode.WRITE))
        self._update(item_id,
                     {"status": "acquired", "matched_edition_id": edition_id,
                      "acquired_at": "__now__"}, expected_rev)

    def revert_acquired(self, item_id: int, status: str) -> None:
        """Un-acquire an item whose fulfilling book left the catalogue (edition deleted): clear the
        acquisition stamp + matched edition and return it to the active wishlist. Staged."""
        self._a.authorize(Action(_RESOURCE, "revert_acquired", AccessMode.WRITE))
        self._update(item_id, {"status": status, "acquired_at": None, "matched_edition_id": None},
                     expected_rev=None)

    def remove(self, item_id: int, *, expected_rev: "int | None" = None) -> None:
        """Soft-delete (tombstone) a wishlist item — the id is frozen, never reused. Staged."""
        self._a.authorize(Action(_RESOURCE, "remove", AccessMode.WRITE))
        self._update(item_id, {"deleted_at": "__now__"}, expected_rev, live_only=True)

    # ── internals ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _snapshot_cols(snapshot: dict) -> dict:
        """Translate a resolver snapshot (DTO keys) to DB columns, JSON-encoding authors/candidates."""
        cols = {}
        for k, col in _SNAPSHOT_MAP.items():
            if k in snapshot:
                cols[col] = snapshot[k]
        if "authors" in snapshot:
            cols["resolved_authors"] = _json.dumps(list(snapshot["authors"] or []))
        if "candidates" in snapshot:
            cols["candidates_json"] = _json.dumps(list(snapshot["candidates"] or []))
        return cols

    def _update(self, item_id: int, cols: dict, expected_rev: "int | None",
                *, live_only: bool = False) -> None:
        """Stage an UPDATE of `cols` on one item, always bumping `rev` and stamping `updated_at`.
        `"__now__"` values become datetime('now'). When `expected_rev` is given, the write is guarded
        on it (StaleWrite on mismatch); a missing row raises NotFound."""
        sets, args = [], []
        for col, val in cols.items():
            if val == "__now__":
                sets.append(f"{col} = datetime('now')")
            else:
                sets.append(f"{col} = ?"); args.append(val)
        sets.append("updated_at = datetime('now')")
        sets.append("rev = rev + 1")
        where = "id = ?"; args2 = [item_id]
        if live_only:
            where += " AND deleted_at IS NULL"
        if expected_rev is not None:
            where += " AND rev = ?"; args2.append(expected_rev)
        cur = self._a.rw.execute(
            f"UPDATE wishlist_item SET {', '.join(sets)} WHERE {where}", (*args, *args2))
        if cur.rowcount == 0:
            self._raise_for_missing(item_id, expected_rev)

    def _raise_for_missing(self, item_id: int, expected_rev: "int | None") -> None:
        """A 0-row update is either a stale `rev` (StaleWrite) or a non-existent/tombstoned row
        (NotFound) — distinguish so the client retries vs. gives up."""
        row = self._a.rw.execute(
            "SELECT rev, deleted_at FROM wishlist_item WHERE id = ?", (item_id,)).fetchone()
        if row is None or row[1] is not None:
            raise NotFound(f"wishlist item {item_id} does not exist")
        if expected_rev is not None and row[0] != expected_rev:
            raise StaleWrite(
                f"wishlist item {item_id} changed since read (rev {row[0]}, expected {expected_rev})")
        raise NotFound(f"wishlist item {item_id} does not exist")
