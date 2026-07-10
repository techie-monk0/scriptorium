"""Reader-state store — per-copy reader state that syncs across the user's devices
(reader_module_plan.md §4/§5).

A reader-OWNED persistence module: it lives in `db_store` (the data layer — where raw SQL
belongs), NOT in access-api (which stays the carve layer for the core entity tables —
person/edition/work). It is the *only* code that touches its own tables (`bookmark`,
`annotation`, `sync_state`) — the /sync/reader route, the PWA, a future native client all go
through the store here, never hand-written SQL. Shaped after the `scan_ocr` access slice
(own-your-DDL, typed, no-commit), kept reader-local. (It was relocated out of `catalogue.webui`
so the webui application layer holds zero raw SQL; the longer-term plan extracts it into its own
`catalogue-reader` package — see reader_module_plan.md.)

Layered, like the access-api entity stores (`HoldingStore`/`SqliteHoldingStore`): callers depend
on the abstract `ReaderStateStore` PORT, never a concrete implementation. `SqliteReaderStateStore`
is the SQLite ADAPTER; `InMemoryReaderStateStore` is a dict-backed fake for tests; a remote/HTTP
store could be a third adapter — the /sync/reader route and the PWA are unaffected. Each store
wraps an open connection (a sqlite3 connection or the web app's `Store`, which proxies
`execute`/`executescript`):

  * Owns its own DDL (`ensure_schema`, idempotent) — its tables are NOT in the central
    schema.sql, so this concern's storage stays self-contained.
  * Reads return frozen dataclasses; writes return the stored row.
  * Commits NOTHING — the caller owns the transaction (the route commits after a push).

Offline-first model:
  * `id` is a CLIENT-generated uuid (never a recycled autoincrement int) so a row made
    offline on two devices can't collide.
  * `rev` is a monotonic per-DB counter (`sync_state`); a device pulls `rev > its cursor`, so
    it catches exactly what changed since it last synced — a logical clock that doesn't rely
    on second-granular, device-skewed wall-clocks for ordering.
  * Conflicts resolve last-write-wins on the client `updated_at`.
  * Deletes are soft: a `deleted_at` tombstone propagates the removal on sync instead of the
    row reappearing from a device that still has it. The app never hard-deletes a row.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class Bookmark:
    """One bookmark, as stored + synced. `locator` is the same opaque scheme as
    reading_position (a PDF page number "42" or an EPUB CFI). `created_at`/`updated_at` are
    the CLIENT's wall-clock (ISO-8601); `rev` is the server-assigned sync sequence."""
    id: str
    holding_id: "int | None"
    locator: "str | None"
    fraction: "float | None"
    label: "str | None"
    created_at: "str | None"
    updated_at: "str | None"
    deleted_at: "str | None"
    rev: int
    # Content identity of the marked file (holding.content_hash) — the DURABLE link.
    # A holding delete sets holding_id NULL (not cascade-delete), so the mark survives
    # as an orphan keyed by content_hash and re-links when the same file re-imports.
    content_hash: "str | None" = None


@dataclass(frozen=True)
class Annotation:
    """One annotation, as stored + synced. Anchoring is format-specific (reader_module_plan
    §4.3): EPUB text marks (highlight/underline/strikeout/note) anchor to a `cfi_range`; PDF
    regions to `page` + `rect` (JSON [x,y,w,h] in normalised page coords); freehand `ink` is
    JSON vector strokes in page-relative coords. Only the columns a given `kind` needs are set.
    `rev` is the server sync sequence (shared with bookmarks)."""
    id: str
    holding_id: "int | None"
    kind: "str | None"            # 'highlight' | 'underline' | 'strikeout' | 'note' | 'ink'
    cfi_range: "str | None"       # EPUB text range
    page: "int | None"            # PDF page, or EPUB spine index for ink
    rect: "str | None"            # JSON region (PDF)
    color: "str | None"
    note_text: "str | None"
    ink: "str | None"             # JSON vector strokes (kind='ink')
    created_at: "str | None"
    updated_at: "str | None"
    deleted_at: "str | None"
    rev: int
    # Durable content identity — see Bookmark.content_hash. Survives a holding delete
    # (holding_id → NULL) and re-links on re-import of the same file.
    content_hash: "str | None" = None


__all__ = ["Bookmark", "Annotation", "ReaderStateStore", "SqliteReaderStateStore",
           "InMemoryReaderStateStore", "ensure_schema"]


# ── Schema ownership ─────────────────────────────────────────────────────────
_SCHEMA_SQL = """
-- holding_id is NULLABLE with ON DELETE SET NULL (not cascade): a hard-deleted holding
-- leaves its marks behind as orphans keyed by content_hash, to re-link on re-import
-- (reader plan N0 "survive and re-attach"). content_hash is the durable file identity.
CREATE TABLE IF NOT EXISTS bookmark (
  id           TEXT PRIMARY KEY,
  holding_id   INTEGER REFERENCES holding(id) ON DELETE SET NULL,
  content_hash TEXT,
  locator      TEXT,
  fraction     REAL,
  label        TEXT,
  created_at   TEXT,
  updated_at   TEXT,
  deleted_at   TEXT,
  rev          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS bookmark_holding_idx ON bookmark(holding_id);
CREATE INDEX IF NOT EXISTS bookmark_rev_idx     ON bookmark(rev);
CREATE INDEX IF NOT EXISTS bookmark_chash_idx   ON bookmark(content_hash);

-- Highlights / underline / strikeout / notes / handwritten ink. Same offline-first shape as
-- bookmark (client uuid, client timestamps, deleted_at tombstone, server rev). Anchoring per
-- kind (reader_module_plan §4.3): cfi_range for EPUB text; page+rect for a PDF region; ink (JSON
-- vector strokes) for freehand. Kept normalised in the DB (NOT in a PDF.js save blob) so a
-- native client renders the same records.
CREATE TABLE IF NOT EXISTS annotation (
  id           TEXT PRIMARY KEY,
  holding_id   INTEGER REFERENCES holding(id) ON DELETE SET NULL,
  content_hash TEXT,
  kind         TEXT,
  cfi_range    TEXT,
  page         INTEGER,
  rect         TEXT,
  color        TEXT,
  note_text    TEXT,
  ink          TEXT,
  created_at   TEXT,
  updated_at   TEXT,
  deleted_at   TEXT,
  rev          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS annotation_holding_idx ON annotation(holding_id);
CREATE INDEX IF NOT EXISTS annotation_rev_idx     ON annotation(rev);
CREATE INDEX IF NOT EXISTS annotation_chash_idx   ON annotation(content_hash);

-- Monotonic per-DB change counter that orders reader-sync writes. Single row, id = 1.
CREATE TABLE IF NOT EXISTS sync_state (
  id  INTEGER PRIMARY KEY CHECK (id = 1),
  rev INTEGER NOT NULL DEFAULT 0
);
"""

# NOTE: column order here matches the dataclass FIELD order (content_hash last), because
# rows are splatted positionally — `Bookmark(*row)` / `Annotation(*row)`. The physical
# schema column order is independent.
_BOOKMARK_COLS = (
    "id, holding_id, locator, fraction, label, created_at, updated_at, deleted_at, rev, "
    "content_hash"
)
_ANNOTATION_COLS = (
    "id, holding_id, kind, cfi_range, page, rect, color, note_text, ink, "
    "created_at, updated_at, deleted_at, rev, content_hash"
)


# ── Port ─────────────────────────────────────────────────────────────────────
class ReaderStateStore(abc.ABC):
    """The data operations the /sync/reader route (and the PWA / a future native client) needs,
    with no transport or transaction-ownership logic. Callers program against THIS; the SQLite,
    in-memory, or any future adapter is injected."""

    @abc.abstractmethod
    def ensure_schema(self) -> None:
        """Create the bookmark + annotation + sync_state tables (idempotent)."""

    # ── the sync rev (logical clock) ─────────────────────────────────────────
    @abc.abstractmethod
    def cursor(self) -> int:
        """The high-water rev a client stores and sends back as `since`. 0 if nothing synced."""

    @abc.abstractmethod
    def next_rev(self) -> int:
        """Claim the next monotonic rev for a write."""

    # ── reads (incl. tombstones, oldest-change first) ────────────────────────
    @abc.abstractmethod
    def bookmarks_since(self, since: int) -> "list[Bookmark]":
        """Every bookmark changed after rev `since` — INCLUDING tombstoned rows."""

    @abc.abstractmethod
    def annotations_since(self, since: int) -> "list[Annotation]":
        """Every annotation changed after rev `since` — INCLUDING tombstoned rows."""

    @abc.abstractmethod
    def annotations_for_holding(self, holding_id: int) -> "list[Annotation]":
        """Every LIVE (non-tombstoned) annotation for one copy, rev-ordered — the
        export-annotated-PDF source (the reader's marks to bake into a file)."""

    @abc.abstractmethod
    def bookmarks_for_holding(self, holding_id: int) -> "list[Bookmark]":
        """Every LIVE (non-tombstoned) bookmark for one copy, rev-ordered — the reader's
        holding-scoped list read (cheap; avoids a whole-table pull on open)."""

    @abc.abstractmethod
    def annotations_since_for_holding(self, holding_id: int, since: int) -> "list[Annotation]":
        """One copy's annotations changed after rev `since`, INCLUDING tombstones — the
        holding-scoped offline-sync delta (a native/PWA reader reconciles its own book without a
        whole-table pull, and learns of cross-device deletions)."""

    @abc.abstractmethod
    def bookmarks_since_for_holding(self, holding_id: int, since: int) -> "list[Bookmark]":
        """One copy's bookmarks changed after rev `since`, INCLUDING tombstones (see above)."""

    # ── writes (caller owns the transaction) ─────────────────────────────────
    @abc.abstractmethod
    def apply_bookmark(self, *, id: str, holding_id: int,
                       locator: "str | None" = None, fraction: "float | None" = None,
                       label: "str | None" = None, created_at: "str | None" = None,
                       updated_at: "str | None" = None, deleted_at: "str | None" = None,
                       content_hash: "str | None" = None) -> "Bookmark | None":
        """Idempotent last-write-wins upsert of one bookmark, keyed by the client `id`. Returns
        the stored row (with its rev), or None when the incoming edit is older than the stored
        one. `content_hash` tags the mark's file identity; when omitted, an adapter that can see
        the holding derives it. Raises ValueError on a malformed op. Does NOT commit."""

    @abc.abstractmethod
    def apply_annotation(self, *, id: str, holding_id: int, kind: "str | None" = None,
                         cfi_range: "str | None" = None, page: "int | None" = None,
                         rect: "str | None" = None, color: "str | None" = None,
                         note_text: "str | None" = None, ink: "str | None" = None,
                         created_at: "str | None" = None, updated_at: "str | None" = None,
                         deleted_at: "str | None" = None,
                         content_hash: "str | None" = None) -> "Annotation | None":
        """Idempotent last-write-wins upsert of one annotation, keyed by the client `id`. Same
        rules as apply_bookmark. Does NOT commit."""

    @abc.abstractmethod
    def relink_orphans(self, *, holding_id: int, content_hash: str) -> int:
        """Re-attach marks orphaned by a holding delete (holding_id NULL) to a re-imported
        file, matched by `content_hash`. Returns the count re-linked. The import/scan path calls
        this when a holding with that content_hash is (re)created. Does NOT commit."""


# ── SQLite adapter ───────────────────────────────────────────────────────────
class SqliteReaderStateStore(ReaderStateStore):
    """`ReaderStateStore` over an open connection — a sqlite3 connection or the web app's
    `Store` proxy (both expose `execute`/`executescript`). Owns the SQL; commits nothing."""

    def __init__(self, conn):
        self._c = conn

    # Pre-content_hash column sets — what a legacy table can still offer the rebuild copy.
    _LEGACY_COLS = {
        "bookmark": ["id", "holding_id", "locator", "fraction", "label",
                     "created_at", "updated_at", "deleted_at", "rev"],
        "annotation": ["id", "holding_id", "kind", "cfi_range", "page", "rect", "color",
                       "note_text", "ink", "created_at", "updated_at", "deleted_at", "rev"],
    }

    def ensure_schema(self) -> None:
        # A table predating content_hash / the ON DELETE SET NULL FK must be REBUILT (SQLite
        # can't alter a column's FK action in place). Rename the legacy tables aside, let
        # _SCHEMA_SQL recreate them in the current shape, copy rows back, drop the old.
        legacy = self._legacy_tables()
        for table, _ in legacy:
            self._c.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
        self._c.executescript(_SCHEMA_SQL)
        for table, common in legacy:
            self._c.execute(
                f"INSERT INTO {table} ({common}) SELECT {common} FROM {table}__old")
            self._c.execute(f"DROP TABLE {table}__old")

    def _legacy_tables(self) -> "list[tuple[str, str]]":
        """(table, comma-cols-to-copy) for each existing table that needs the rebuild — i.e.
        lacks content_hash or still cascades holding deletes. Empty on a fresh / current DB."""
        out: "list[tuple[str, str]]" = []
        for table, legacy_cols in self._LEGACY_COLS.items():
            info = self._c.execute(f"PRAGMA table_info({table})").fetchall()
            if not info:
                continue  # fresh DB — _SCHEMA_SQL creates it new-shape
            names = {r[1] for r in info}
            fks = self._c.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            # foreign_key_list cols: id, seq, table, from, to, on_update, on_delete, match
            cascades = any(fk[2] == "holding" and (fk[6] or "").upper() != "SET NULL"
                           for fk in fks)
            if "content_hash" in names and not cascades:
                continue  # already current
            out.append((table, ", ".join(c for c in legacy_cols if c in names)))
        return out

    def cursor(self) -> int:
        row = self._c.execute("SELECT rev FROM sync_state WHERE id = 1").fetchone()
        return row[0] if row else 0

    def next_rev(self) -> int:
        self._c.execute("INSERT OR IGNORE INTO sync_state (id, rev) VALUES (1, 0)")
        self._c.execute("UPDATE sync_state SET rev = rev + 1 WHERE id = 1")
        return self._c.execute("SELECT rev FROM sync_state WHERE id = 1").fetchone()[0]

    def bookmarks_since(self, since: int) -> "list[Bookmark]":
        rows = self._c.execute(
            f"SELECT {_BOOKMARK_COLS} FROM bookmark WHERE rev > ? ORDER BY rev", (since,)
        ).fetchall()
        return [Bookmark(*r) for r in rows]

    def annotations_since(self, since: int) -> "list[Annotation]":
        rows = self._c.execute(
            f"SELECT {_ANNOTATION_COLS} FROM annotation WHERE rev > ? ORDER BY rev", (since,)
        ).fetchall()
        return [Annotation(*r) for r in rows]

    def annotations_for_holding(self, holding_id: int) -> "list[Annotation]":
        rows = self._c.execute(
            f"SELECT {_ANNOTATION_COLS} FROM annotation "
            "WHERE holding_id = ? AND deleted_at IS NULL ORDER BY rev", (holding_id,)
        ).fetchall()
        return [Annotation(*r) for r in rows]

    def bookmarks_for_holding(self, holding_id: int) -> "list[Bookmark]":
        rows = self._c.execute(
            f"SELECT {_BOOKMARK_COLS} FROM bookmark "
            "WHERE holding_id = ? AND deleted_at IS NULL ORDER BY rev", (holding_id,)
        ).fetchall()
        return [Bookmark(*r) for r in rows]

    def annotations_since_for_holding(self, holding_id, since):
        rows = self._c.execute(
            f"SELECT {_ANNOTATION_COLS} FROM annotation "
            "WHERE holding_id = ? AND rev > ? ORDER BY rev", (holding_id, since)
        ).fetchall()
        return [Annotation(*r) for r in rows]

    def bookmarks_since_for_holding(self, holding_id, since):
        rows = self._c.execute(
            f"SELECT {_BOOKMARK_COLS} FROM bookmark "
            "WHERE holding_id = ? AND rev > ? ORDER BY rev", (holding_id, since)
        ).fetchall()
        return [Bookmark(*r) for r in rows]

    def _holding_content_hash(self, holding_id):
        if holding_id is None:
            return None
        r = self._c.execute(
            "SELECT content_hash FROM holding WHERE id = ?", (holding_id,)).fetchone()
        return r[0] if r else None

    def apply_bookmark(self, *, id, holding_id, locator=None, fraction=None, label=None,
                       created_at=None, updated_at=None, deleted_at=None, content_hash=None):
        if not id or holding_id is None:
            raise ValueError("bookmark op needs id + holding_id")
        incoming = updated_at or ""
        existing = self._c.execute(
            "SELECT updated_at FROM bookmark WHERE id = ?", (id,)).fetchone()
        if existing is not None and (existing[0] or "") > incoming:
            return None  # our stored copy is newer → keep it (last-write-wins)
        if content_hash is None:
            content_hash = self._holding_content_hash(holding_id)
        rev = self.next_rev()
        self._c.execute(
            "INSERT INTO bookmark "
            "  (id, holding_id, content_hash, locator, fraction, label, "
            "   created_at, updated_at, deleted_at, rev) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  holding_id = excluded.holding_id, content_hash = excluded.content_hash, "
            "  locator = excluded.locator, "
            "  fraction = excluded.fraction, label = excluded.label, "
            "  created_at = excluded.created_at, updated_at = excluded.updated_at, "
            "  deleted_at = excluded.deleted_at, rev = excluded.rev",
            (id, holding_id, content_hash, locator, fraction, label,
             created_at, incoming, deleted_at, rev))
        row = self._c.execute(
            f"SELECT {_BOOKMARK_COLS} FROM bookmark WHERE id = ?", (id,)).fetchone()
        return Bookmark(*row)

    def apply_annotation(self, *, id, holding_id, kind=None, cfi_range=None, page=None,
                         rect=None, color=None, note_text=None, ink=None,
                         created_at=None, updated_at=None, deleted_at=None, content_hash=None):
        if not id or holding_id is None:
            raise ValueError("annotation op needs id + holding_id")
        incoming = updated_at or ""
        existing = self._c.execute(
            "SELECT updated_at FROM annotation WHERE id = ?", (id,)).fetchone()
        if existing is not None and (existing[0] or "") > incoming:
            return None  # stored copy is newer (last-write-wins)
        if content_hash is None:
            content_hash = self._holding_content_hash(holding_id)
        rev = self.next_rev()
        self._c.execute(
            "INSERT INTO annotation "
            "  (id, holding_id, content_hash, kind, cfi_range, page, rect, color, note_text, ink, "
            "   created_at, updated_at, deleted_at, rev) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  holding_id = excluded.holding_id, content_hash = excluded.content_hash, "
            "  kind = excluded.kind, "
            "  cfi_range = excluded.cfi_range, page = excluded.page, rect = excluded.rect, "
            "  color = excluded.color, note_text = excluded.note_text, ink = excluded.ink, "
            "  created_at = excluded.created_at, updated_at = excluded.updated_at, "
            "  deleted_at = excluded.deleted_at, rev = excluded.rev",
            (id, holding_id, content_hash, kind, cfi_range, page, rect, color, note_text, ink,
             created_at, incoming, deleted_at, rev))
        row = self._c.execute(
            f"SELECT {_ANNOTATION_COLS} FROM annotation WHERE id = ?", (id,)).fetchone()
        return Annotation(*row)

    def relink_orphans(self, *, holding_id, content_hash):
        if holding_id is None or not content_hash:
            return 0
        n = 0
        for table in ("annotation", "bookmark"):
            cur = self._c.execute(
                f"UPDATE {table} SET holding_id = ? "
                "WHERE holding_id IS NULL AND content_hash = ?", (holding_id, content_hash))
            n += max(cur.rowcount, 0)
        return n


# ── In-memory fake (tests; the port-adapter split made injectable) ───────────
class InMemoryReaderStateStore(ReaderStateStore):
    """A `ReaderStateStore` backed by dicts — the same offline-first LWW semantics with no DB,
    mirroring `test_kit.InMemoryHoldingStore`. Lets a test exercise the /sync/reader route and
    the sync contract without a SQLite file."""

    def __init__(self):
        self._rev = 0
        self._bm: dict[str, Bookmark] = {}
        self._ann: dict[str, Annotation] = {}

    def ensure_schema(self) -> None:
        pass  # no tables to create

    def cursor(self) -> int:
        return self._rev

    def next_rev(self) -> int:
        self._rev += 1
        return self._rev

    def bookmarks_since(self, since):
        return sorted((b for b in self._bm.values() if b.rev > since), key=lambda b: b.rev)

    def annotations_since(self, since):
        return sorted((a for a in self._ann.values() if a.rev > since), key=lambda a: a.rev)

    def annotations_for_holding(self, holding_id):
        return sorted((a for a in self._ann.values()
                       if a.holding_id == holding_id and not a.deleted_at), key=lambda a: a.rev)

    def bookmarks_for_holding(self, holding_id):
        return sorted((b for b in self._bm.values()
                       if b.holding_id == holding_id and not b.deleted_at), key=lambda b: b.rev)

    def annotations_since_for_holding(self, holding_id, since):
        return sorted((a for a in self._ann.values()
                       if a.holding_id == holding_id and a.rev > since), key=lambda a: a.rev)

    def bookmarks_since_for_holding(self, holding_id, since):
        return sorted((b for b in self._bm.values()
                       if b.holding_id == holding_id and b.rev > since), key=lambda b: b.rev)

    def apply_bookmark(self, *, id, holding_id, locator=None, fraction=None, label=None,
                       created_at=None, updated_at=None, deleted_at=None, content_hash=None):
        if not id or holding_id is None:
            raise ValueError("bookmark op needs id + holding_id")
        incoming = updated_at or ""
        cur = self._bm.get(id)
        if cur is not None and (cur.updated_at or "") > incoming:
            return None
        rev = self.next_rev()
        bm = Bookmark(id=id, holding_id=holding_id, locator=locator, fraction=fraction,
                      label=label, created_at=created_at, updated_at=incoming,
                      deleted_at=deleted_at, rev=rev, content_hash=content_hash)
        self._bm[id] = bm
        return bm

    def apply_annotation(self, *, id, holding_id, kind=None, cfi_range=None, page=None,
                         rect=None, color=None, note_text=None, ink=None,
                         created_at=None, updated_at=None, deleted_at=None, content_hash=None):
        if not id or holding_id is None:
            raise ValueError("annotation op needs id + holding_id")
        incoming = updated_at or ""
        cur = self._ann.get(id)
        if cur is not None and (cur.updated_at or "") > incoming:
            return None
        rev = self.next_rev()
        a = Annotation(id=id, holding_id=holding_id, kind=kind, cfi_range=cfi_range, page=page,
                       rect=rect, color=color, note_text=note_text, ink=ink,
                       created_at=created_at, updated_at=incoming, deleted_at=deleted_at, rev=rev,
                       content_hash=content_hash)
        self._ann[id] = a
        return a

    def relink_orphans(self, *, holding_id, content_hash):
        if holding_id is None or not content_hash:
            return 0
        import dataclasses
        n = 0
        for store in (self._ann, self._bm):
            for key, row in list(store.items()):
                if row.holding_id is None and row.content_hash == content_hash:
                    store[key] = dataclasses.replace(row, holding_id=holding_id)
                    n += 1
        return n


# ── Bootstrap helper ─────────────────────────────────────────────────────────
def ensure_schema(conn) -> None:
    """Stand up the reader-state tables on `conn` (idempotent) — the startup hook in web.py.
    A thin wrapper over the SQLite adapter so the app bootstrap doesn't construct a store."""
    SqliteReaderStateStore(conn).ensure_schema()
