"""Holding persistence — the storage PORT + its SQLite adapter.

The access layer (reads/writes) is storage-agnostic: it programs against `HoldingStore` — an
abstract port of pure data operations — and never writes SQL or touches a connection. The plan→apply
orchestration, the policy gate, Impact computation and fingerprints all live in the access layer;
*how* the bytes are stored is the implementation. `SqliteHoldingStore` is that implementation over
the gateway's RO/RW connections; a different backing (an HTTP client to a remote access-API, an
in-memory fake for tests) is a different adapter — no change to the access layer. This is the same
separation as a holding's *file* being one pluggable backing of the abstract holding.

Convention: read methods use the RO connection; staged mutations use RW and **do not commit** (the
access layer's `Session`/`apply` owns the transaction via `Access.commit`). `current_fingerprint`
reads via RW because it is the write-side TOCTOU check (the transaction-consistent view).
"""
from __future__ import annotations

import abc

from catalogue.contracts import Holding

_COLS = "id, edition_id, file_path, content_hash, text_status"


def _holding(row) -> Holding:
    return Holding(id=row[0], edition_id=row[1], file_path=row[2],
                   content_hash=row[3], text_status=row[4])


class HoldingStore(abc.ABC):
    """Port: the data operations the Holding access layer needs, with no policy or transaction logic."""

    # ── reads ──────────────────────────────────────────────────────────────────
    @abc.abstractmethod
    def get(self, holding_id: int) -> "Holding | None": ...
    @abc.abstractmethod
    def list_all(self) -> "list[Holding]":
        """Every holding, in id order — the bulk/maintenance read (e.g. the exclusion sweep)."""
    @abc.abstractmethod
    def list_by_edition(self, edition_id: int) -> "list[Holding]": ...
    @abc.abstractmethod
    def format_rows(self, edition_id: int) -> "list[tuple]":
        """(holding_type, form, file_path, archival_pdf_path) per holding of an edition, id-ordered —
        the raw facets the 'which formats do I hold' derivation needs (not on the Holding DTO)."""
    @abc.abstractmethod
    def by_file_path(self, path: str) -> "Holding | None":
        """The holding whose file_path == `path`, or None (the sidecar/relink lookup)."""
    @abc.abstractmethod
    def ocr_fields(self, holding_id: int) -> "tuple | None":
        """(file_path, text_status, file_hash) for a holding, or None — the digitize precondition read
        (file_hash is the cache key, not on the DTO)."""
    @abc.abstractmethod
    def primary_file(self, edition_id: int) -> "tuple | None":
        """(holding_id, file_path, file_hash) of an edition's first file-bearing holding, or None."""
    @abc.abstractmethod
    def process_fields(self, holding_id: int) -> "tuple | None":
        """(edition_id, file_path, file_hash, text_status) for a holding, or None — the process
        pipeline precondition read (file_hash not on the DTO)."""
    @abc.abstractmethod
    def paths_of(self, holding_id: int) -> "tuple | None":
        """(file_path, archival_pdf_path) for a holding, or None — the send/relink path lookup."""
    @abc.abstractmethod
    def ids_by_text_status(self, statuses) -> "list[int]":
        """Holding ids whose text_status is in `statuses`, id-ordered (the re-OCR worklist)."""
    @abc.abstractmethod
    def total(self) -> int:
        """How many holding rows exist (the settings dashboard stat)."""
    @abc.abstractmethod
    def count_by_root(self, root_id: int) -> int:
        """How many holdings are attributed to a library root."""
    @abc.abstractmethod
    def file_paths(self, root_id=None) -> "list[str]":
        """Every holding file_path (optionally scoped to a root) — the repoint-preview scan."""
    @abc.abstractmethod
    def with_file_path(self) -> "list[tuple]":
        """(id, file_path) for every holding carrying a non-empty file_path — the root-backfill scan."""
    @abc.abstractmethod
    def relocation_rows(self) -> "list[tuple]":
        """(id, file_path, file_hash, root_id) for every holding — the prefix-repoint scan."""
    @abc.abstractmethod
    def with_files(self) -> "list[tuple]":
        """(id, edition_id, file_path, file_hash, content_hash) for every holding carrying a
        non-empty file_path — the broken-link / relink scan."""
    @abc.abstractmethod
    def reconcile_index(self) -> "list[tuple]":
        """(id, file_path, file_hash, content_hash, edition_id) for every holding — the reconcile
        by-hash/by-path/by-content dedup index."""
    @abc.abstractmethod
    def by_file_hash(self, file_hash: str) -> "tuple | None":
        """(id, file_path, edition_id) for the holding carrying `file_hash`, or None — the sweep
        idempotent-upsert key."""
    @abc.abstractmethod
    def cover_handle(self, edition_id: int) -> "tuple | None":
        """(id, file_path, archival_pdf_path) for an edition's first holding (by id), or None — the
        open-in-viewer / cover handle the Library tiles use."""
    @abc.abstractmethod
    def edition_card_rows(self, edition_id: int) -> "list[tuple]":
        """(id, form, text_status, file_path, shelf_location) per holding of an edition — the edition
        record's copies list."""
    @abc.abstractmethod
    def formats(self, edition_id: int) -> "list[str]":
        """The distinct non-null holding_type values across an edition's holdings, code-ordered."""
    @abc.abstractmethod
    def fields_card(self, holding_id: int) -> "tuple | None":
        """(id, edition_id, form, text_status, holding_type, shelf_location, ocr_quality_score, notes,
        file_path) for the per-holding fields editor, or None."""
    @abc.abstractmethod
    def shares_file(self, path: str, exclude_holding_id: int) -> bool:
        """Whether any OTHER holding references `path` (as file_path or archival_pdf_path) — the
        delete-to-trash safety check."""
    @abc.abstractmethod
    def display_rows(self, edition_id: int) -> "list[tuple]":
        """(id, form, holding_type, file_path, archival_pdf_path) for every holding of an edition — the
        work/edition-detail holdings list."""
    @abc.abstractmethod
    def full_rows(self, edition_id: int) -> "list[tuple]":
        """(id, form, file_path, archival_pdf_path, shelf_location, holding_type, text_status) per
        holding of an edition — the export record's holdings."""
    @abc.abstractmethod
    def earliest_added(self, edition_id: int):
        """The earliest holding.date_added across an edition's holdings (when it entered), or None."""
    @abc.abstractmethod
    def detect_paths(self, edition_id: int) -> "list[str]":
        """Every distinct file_path + archival_pdf_path of an edition's holdings (non-null) — the
        filename-detect input."""
    @abc.abstractmethod
    def read_target(self, holding_id: int) -> "tuple | None":
        """(file_path, archival_pdf_path, edition_id, edition_title) for the in-app reader, or None."""
    @abc.abstractmethod
    def cover_source(self, holding_id: int, edition_id: int) -> "tuple | None":
        """(file_path, archival_pdf_path) for a holding scoped to its edition (cover-from-page), or None."""
    @abc.abstractmethod
    def mark_opened(self, holding_id: int) -> None:
        """Stamp holding.last_opened = now (the 'Recently opened' ordering). Staged."""
    @abc.abstractmethod
    def text_status_counts(self) -> dict:
        """{text_status: count} over every holding (a None key for NULL) — the sweep status dashboard."""
    @abc.abstractmethod
    def by_text_status(self, statuses, include_null: bool) -> "list[tuple]":
        """(file_path, file_hash) for holdings whose text_status is in `statuses` (and/or NULL) — the
        reprocess worklist."""
    @abc.abstractmethod
    def ocr_review_holding(self, file_hash: str) -> "tuple | None":
        """(id, edition_id, file_path, text_status, ocr_quality_score) for any holding carrying
        `file_hash` — the low_quality_ocr review-detail link."""
    @abc.abstractmethod
    def set_text_status_by_hash(self, file_hash: str, status: str) -> None:
        """Set text_status for every holding carrying `file_hash` (the OCR-quality override). Staged."""
    @abc.abstractmethod
    def text_status_by_hash(self, file_hash: str) -> "tuple | None":
        """(text_status,) for any holding carrying `file_hash`, or None when NO holding has it — the
        reconcile readability hint. A row tuple (not the bare value) so the caller can tell a holding
        with a NULL text_status apart from no holding at all."""
    @abc.abstractmethod
    def location_of(self, holding_id: int) -> "tuple | None":
        """(file_path, file_hash) for a holding, or None — the relink before/after read."""
    @abc.abstractmethod
    def openable(self, edition_id: int) -> "list[tuple]":
        """(id, file_path, archival_pdf_path, form) per holding of an edition, id-ordered — the
        replica-export 'openable copies' read."""
    @abc.abstractmethod
    def file_referenced(self, path: str) -> bool:
        """Whether ANY holding still points at `path` (file_path or archival_pdf_path) — the
        keep-a-shared-file guard before trashing a deleted edition's files."""
    @abc.abstractmethod
    def text_status_codes(self) -> "frozenset[str]": ...
    @abc.abstractmethod
    def delete_fields(self, holding_id: int) -> "dict | None":
        """{file_path, archival_pdf_path, file_hash, content_hash}, or None if the holding is absent."""
    @abc.abstractmethod
    def shares_file_hash(self, file_hash: str, exclude_ids: "frozenset[int]") -> bool: ...
    @abc.abstractmethod
    def shares_file_path(self, path: str, exclude_ids: "frozenset[int]") -> bool: ...

    # ── write-side check + staged mutations (no commit) ─────────────────────────
    @abc.abstractmethod
    def current_fingerprint(self, holding_id: int) -> "tuple[bool, str | None]":
        """(exists, content_hash) as the write transaction sees it — the TOCTOU/recycled-id guard."""
    @abc.abstractmethod
    def update(self, holding_id: int, changes: dict) -> None: ...
    @abc.abstractmethod
    def append_note(self, holding_id: int, note: str) -> None:
        """Append `note` to holding.notes on its own line (provenance trail; never clobbers)."""
    @abc.abstractmethod
    def set_file_path(self, holding_id: int, path: str) -> None:
        """Set holding.file_path only (a moved file, bytes unchanged). Staged."""
    @abc.abstractmethod
    def set_hashes(self, holding_id: int, file_hash, content_hash) -> None:
        """Set holding.file_hash + content_hash (annotated/in-place re-bytes). Staged."""
    @abc.abstractmethod
    def set_file_hash(self, holding_id: int, file_hash) -> None:
        """Set holding.file_hash alone (the rehash CLI's per-row update). Staged."""
    @abc.abstractmethod
    def set_path_hashes(self, holding_id: int, path: str, file_hash, content_hash) -> None:
        """Set holding.file_path + file_hash + content_hash (accept a superseding file). Staged."""
    @abc.abstractmethod
    def insert_holding(self, **cols) -> int:
        """Insert a holding from whitelisted columns; return the new id. Staged."""
    @abc.abstractmethod
    def set_root(self, holding_id: int, root_id: int) -> None:
        """Set holding.root_id (library-root attribution backfill). Staged."""
    @abc.abstractmethod
    def set_location(self, holding_id: int, file_path: str, file_hash) -> None:
        """Set holding.file_path + file_hash (a prefix repoint / re-hash). Staged."""
    @abc.abstractmethod
    def purge_cache(self, table: str, file_hash: str) -> None: ...
    @abc.abstractmethod
    def delete(self, holding_id: int) -> None: ...


class SqliteHoldingStore(HoldingStore):
    """SQLite adapter over an `Access`'s RO/RW connections."""

    def __init__(self, access):
        self._a = access

    def get(self, holding_id):
        row = self._a.ro.execute(
            f"SELECT {_COLS} FROM holding WHERE id = ?", (holding_id,)).fetchone()
        return _holding(row) if row else None

    def list_all(self):
        return [_holding(r) for r in self._a.ro.execute(
            f"SELECT {_COLS} FROM holding ORDER BY id").fetchall()]

    def list_by_edition(self, edition_id):
        return [_holding(r) for r in self._a.ro.execute(
            f"SELECT {_COLS} FROM holding WHERE edition_id = ? ORDER BY id",
            (edition_id,)).fetchall()]

    def format_rows(self, edition_id):
        return self._a.ro.execute(
            "SELECT holding_type, form, file_path, archival_pdf_path "
            "FROM holding WHERE edition_id = ? ORDER BY id", (edition_id,)).fetchall()

    def by_file_path(self, path):
        row = self._a.ro.execute(
            f"SELECT {_COLS} FROM holding WHERE file_path = ?", (path,)).fetchone()
        return _holding(row) if row else None

    def ocr_fields(self, holding_id):
        return self._a.ro.execute(
            "SELECT file_path, text_status, file_hash FROM holding WHERE id = ?",
            (holding_id,)).fetchone()

    def primary_file(self, edition_id):
        return self._a.ro.execute(
            "SELECT id, file_path, file_hash FROM holding WHERE edition_id = ? "
            "AND file_path IS NOT NULL ORDER BY id LIMIT 1", (edition_id,)).fetchone()

    def process_fields(self, holding_id):
        return self._a.ro.execute(
            "SELECT edition_id, file_path, file_hash, text_status FROM holding WHERE id = ?",
            (holding_id,)).fetchone()

    def paths_of(self, holding_id):
        return self._a.ro.execute(
            "SELECT file_path, archival_pdf_path FROM holding WHERE id = ?",
            (holding_id,)).fetchone()

    def ids_by_text_status(self, statuses):
        statuses = tuple(statuses)
        ph = ",".join("?" * len(statuses)) or "NULL"
        return [r[0] for r in self._a.ro.execute(
            f"SELECT id FROM holding WHERE text_status IN ({ph}) ORDER BY id",
            statuses).fetchall()]

    def total(self):
        return self._a.ro.execute("SELECT COUNT(*) FROM holding").fetchone()[0]

    def count_by_root(self, root_id):
        return self._a.ro.execute(
            "SELECT COUNT(*) FROM holding WHERE root_id = ?", (root_id,)).fetchone()[0]

    def file_paths(self, root_id=None):
        if root_id is None:
            rows = self._a.ro.execute("SELECT file_path FROM holding").fetchall()
        else:
            rows = self._a.ro.execute(
                "SELECT file_path FROM holding WHERE root_id = ?", (root_id,)).fetchall()
        return [r[0] for r in rows]

    def with_file_path(self):
        return self._a.ro.execute(
            "SELECT id, file_path FROM holding "
            "WHERE file_path IS NOT NULL AND TRIM(file_path) <> ''").fetchall()

    def relocation_rows(self):
        return self._a.ro.execute(
            "SELECT id, file_path, file_hash, root_id FROM holding").fetchall()

    def with_files(self):
        return self._a.ro.execute(
            "SELECT id, edition_id, file_path, file_hash, content_hash FROM holding "
            "WHERE file_path IS NOT NULL AND file_path <> ''").fetchall()

    def reconcile_index(self):
        return self._a.ro.execute(
            "SELECT id, file_path, file_hash, content_hash, edition_id FROM holding").fetchall()

    def by_file_hash(self, file_hash):
        return self._a.ro.execute(
            "SELECT id, file_path, edition_id FROM holding WHERE file_hash = ?",
            (file_hash,)).fetchone()

    def cover_handle(self, edition_id):
        return self._a.ro.execute(
            "SELECT id, file_path, archival_pdf_path FROM holding "
            "WHERE edition_id = ? ORDER BY id LIMIT 1", (edition_id,)).fetchone()

    def edition_card_rows(self, edition_id):
        return self._a.ro.execute(
            "SELECT id, form, text_status, file_path, shelf_location FROM holding "
            "WHERE edition_id = ?", (edition_id,)).fetchall()

    def formats(self, edition_id):
        return [r[0] for r in self._a.ro.execute(
            "SELECT DISTINCT holding_type FROM holding WHERE edition_id = ? "
            "AND holding_type IS NOT NULL ORDER BY holding_type", (edition_id,)).fetchall()]

    def fields_card(self, holding_id):
        return self._a.ro.execute(
            "SELECT id, edition_id, form, text_status, holding_type, shelf_location, "
            "ocr_quality_score, notes, file_path FROM holding WHERE id = ?", (holding_id,)).fetchone()

    def shares_file(self, path, exclude_holding_id):
        return self._a.ro.execute(
            "SELECT 1 FROM holding WHERE (file_path = ? OR archival_pdf_path = ?) AND id != ? LIMIT 1",
            (path, path, exclude_holding_id)).fetchone() is not None

    def display_rows(self, edition_id):
        return self._a.ro.execute(
            "SELECT id, form, holding_type, file_path, archival_pdf_path FROM holding "
            "WHERE edition_id = ? ORDER BY id", (edition_id,)).fetchall()

    def full_rows(self, edition_id):
        return self._a.ro.execute(
            "SELECT id, form, file_path, archival_pdf_path, shelf_location, holding_type, text_status "
            "FROM holding WHERE edition_id = ? ORDER BY id", (edition_id,)).fetchall()

    def earliest_added(self, edition_id):
        r = self._a.ro.execute(
            "SELECT MIN(date_added) FROM holding WHERE edition_id = ?", (edition_id,)).fetchone()
        return r[0] if r and r[0] else None

    def detect_paths(self, edition_id):
        return [r[0] for r in self._a.ro.execute(
            "SELECT file_path FROM holding WHERE edition_id = ? AND file_path IS NOT NULL "
            "UNION SELECT archival_pdf_path FROM holding "
            "WHERE edition_id = ? AND archival_pdf_path IS NOT NULL",
            (edition_id, edition_id)).fetchall()]

    def read_target(self, holding_id):
        return self._a.ro.execute(
            "SELECT h.file_path, h.archival_pdf_path, e.id, e.title "
            "FROM holding h JOIN edition e ON e.id = h.edition_id WHERE h.id = ?",
            (holding_id,)).fetchone()

    def cover_source(self, holding_id, edition_id):
        return self._a.ro.execute(
            "SELECT file_path, archival_pdf_path FROM holding WHERE id = ? AND edition_id = ?",
            (holding_id, edition_id)).fetchone()

    def mark_opened(self, holding_id):
        self._a.rw.execute(
            "UPDATE holding SET last_opened = CURRENT_TIMESTAMP WHERE id = ?", (holding_id,))

    def text_status_counts(self):
        return {row[0]: row[1] for row in self._a.ro.execute(
            "SELECT text_status, COUNT(*) FROM holding GROUP BY text_status").fetchall()}

    def by_text_status(self, statuses, include_null):
        where, params = [], []
        statuses = list(statuses)
        if statuses:
            where.append(f"text_status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        if include_null:
            where.append("text_status IS NULL")
        if not where:
            return []
        return self._a.ro.execute(
            f"SELECT file_path, file_hash FROM holding WHERE {' OR '.join(where)}",
            params).fetchall()

    def ocr_review_holding(self, file_hash):
        return self._a.ro.execute(
            "SELECT id, edition_id, file_path, text_status, ocr_quality_score FROM holding "
            "WHERE file_hash = ?", (file_hash,)).fetchone()

    def set_text_status_by_hash(self, file_hash, status):
        self._a.rw.execute(
            "UPDATE holding SET text_status = ? WHERE file_hash = ?", (status, file_hash))

    def text_status_by_hash(self, file_hash):
        return self._a.ro.execute(
            "SELECT text_status FROM holding WHERE file_hash = ? LIMIT 1", (file_hash,)).fetchone()

    def location_of(self, holding_id):
        return self._a.ro.execute(
            "SELECT file_path, file_hash FROM holding WHERE id = ?", (holding_id,)).fetchone()

    def openable(self, edition_id):
        return self._a.ro.execute(
            "SELECT id, file_path, archival_pdf_path, form FROM holding "
            "WHERE edition_id = ? ORDER BY id", (edition_id,)).fetchall()

    def file_referenced(self, path):
        return self._a.ro.execute(
            "SELECT 1 FROM holding WHERE (file_path = ? OR archival_pdf_path = ?) LIMIT 1",
            (path, path)).fetchone() is not None

    def text_status_codes(self):
        return frozenset(c for (c,) in self._a.ro.execute("SELECT code FROM text_status"))

    def delete_fields(self, holding_id):
        row = self._a.ro.execute(
            "SELECT file_path, archival_pdf_path, file_hash, content_hash "
            "FROM holding WHERE id = ?", (holding_id,)).fetchone()
        if row is None:
            return None
        return {"file_path": row[0], "archival_pdf_path": row[1],
                "file_hash": row[2], "content_hash": row[3]}

    def shares_file_hash(self, file_hash, exclude_ids):
        return self._shares("file_hash = ?", (file_hash,), exclude_ids)

    def shares_file_path(self, path, exclude_ids):
        return self._shares("file_path = ? OR archival_pdf_path = ?", (path, path), exclude_ids)

    def _shares(self, where, args, exclude_ids):
        ph = ",".join("?" * len(exclude_ids)) or "NULL"
        return self._a.ro.execute(
            f"SELECT 1 FROM holding WHERE ({where}) AND id NOT IN ({ph}) LIMIT 1",
            (*args, *exclude_ids)).fetchone() is not None

    def current_fingerprint(self, holding_id):
        row = self._a.rw.execute(
            "SELECT content_hash FROM holding WHERE id = ?", (holding_id,)).fetchone()
        return (False, None) if row is None else (True, row[0])

    def update(self, holding_id, changes):
        cols, vals = zip(*changes.items())
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        self._a.rw.execute(
            f"UPDATE holding SET {set_clause} WHERE id = ?", (*vals, holding_id))

    def append_note(self, holding_id, note):
        self._a.rw.execute(
            "UPDATE holding SET notes = TRIM(COALESCE(notes || char(10), '') || ?) WHERE id = ?",
            (note, holding_id))

    def set_file_path(self, holding_id, path):
        self._a.rw.execute("UPDATE holding SET file_path = ? WHERE id = ?", (path, holding_id))

    def set_hashes(self, holding_id, file_hash, content_hash):
        self._a.rw.execute("UPDATE holding SET file_hash = ?, content_hash = ? WHERE id = ?",
                           (file_hash, content_hash, holding_id))

    def set_file_hash(self, holding_id, file_hash):
        self._a.rw.execute("UPDATE holding SET file_hash = ? WHERE id = ?", (file_hash, holding_id))

    def set_path_hashes(self, holding_id, path, file_hash, content_hash):
        self._a.rw.execute(
            "UPDATE holding SET file_path = ?, file_hash = ?, content_hash = ? WHERE id = ?",
            (path, file_hash, content_hash, holding_id))

    _INSERTABLE = ("edition_id", "form", "file_path", "file_hash", "content_hash",
                   "holding_type", "text_status", "ocr_quality_score", "root_id", "shelf_location")

    def insert_holding(self, **cols):
        bad = set(cols) - set(self._INSERTABLE)
        if bad:
            raise ValueError(f"refusing to insert unknown holding columns: {sorted(bad)}")
        keys = list(cols)
        return self._a.rw.execute(
            f"INSERT INTO holding ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
            tuple(cols[k] for k in keys)).lastrowid

    def set_root(self, holding_id, root_id):
        self._a.rw.execute("UPDATE holding SET root_id = ? WHERE id = ?", (root_id, holding_id))

    def set_location(self, holding_id, file_path, file_hash):
        self._a.rw.execute("UPDATE holding SET file_path = ?, file_hash = ? WHERE id = ?",
                           (file_path, file_hash, holding_id))

    def purge_cache(self, table, file_hash):
        self._a.rw.execute(f"DELETE FROM {table} WHERE file_hash = ?", (file_hash,))

    def delete(self, holding_id):
        self._a.rw.execute("DELETE FROM holding WHERE id = ?", (holding_id,))
