"""Phone/PWA capture inbox — the gateway-bound access surface (`acc.capture`).

`capture_staging` is the §14 capture inbox: one row per scanned ISBN / photo / CIP-page-OCR awaiting
desktop resolution (`status` 'raw' until resolved). It is not a catalogue entity (no identity
fingerprint, no soft-delete) — a keyed staging table — so this is a FLAT policy-gated repo: `.reads`
over RO, `.writes` STAGE on the caller's connection and the route commits. The idempotent-on-open-ISBN
insert relies on the `capture_staging_raw_isbn_uq` partial unique index. See catalogue-webui capture
routes and db_store/schema.sql.
"""
from __future__ import annotations

import json as _json

from catalogue.contracts import AccessMode, Action

_RESOURCE = "capture_staging"
# Columns a route may set on an insert — keeps an interpolated column list to known names.
_INSERTABLE = ("form", "raw_isbn", "source", "scanned_at", "status", "free_text_note",
               "metadata_json", "image_path", "in_catalogue")


class CaptureRepo:
    def __init__(self, access):
        self._a = access

    # ── reads ────────────────────────────────────────────────────────────────────
    def open_raw_id(self, isbn: str):
        """The id of the OPEN ('raw') capture row for `isbn`, or None — the §14.5 dedup lookup."""
        self._a.authorize(Action(_RESOURCE, "open_raw_id", AccessMode.READ))
        r = self._a.ro.execute(
            "SELECT id FROM capture_staging WHERE raw_isbn = ? AND status = 'raw' ORDER BY id LIMIT 1",
            (isbn,)).fetchone()
        return r[0] if r else None

    def raw_count(self) -> int:
        """How many capture rows are still open ('raw') — all of them, matched or not."""
        self._a.authorize(Action(_RESOURCE, "raw_count", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT count(*) FROM capture_staging WHERE status = 'raw'").fetchone()[0]

    def unresolved_count(self) -> int:
        """Open captures that still NEED a human to resolve them — the home Capture badge.
        Excludes scans whose at-capture verdict already matched an existing edition
        (`in_catalogue = 1`): those are recognized duplicates, not work to do. A NULL
        verdict (never checked — e.g. a no-ISBN scan) still counts as needing attention."""
        self._a.authorize(Action(_RESOURCE, "raw_count", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT count(*) FROM capture_staging "
            "WHERE status = 'raw' AND COALESCE(in_catalogue, 0) = 0").fetchone()[0]

    def raw_list(self):
        """(id, form, raw_isbn, free_text_note, created_at) for every OPEN ('raw') capture, oldest
        first — the staging worklist."""
        self._a.authorize(Action(_RESOURCE, "raw_list", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT id, form, raw_isbn, free_text_note, created_at FROM capture_staging "
            "WHERE status = 'raw' ORDER BY created_at").fetchall()

    def raw_with_meta(self, limit: int = 1000):
        """(id, raw_isbn, metadata_json) for every OPEN ('raw') capture — the input to the
        capture reconciler, which needs the ISBN + looked-up title/authors to decide whether
        the catalogue now holds the book."""
        self._a.authorize(Action(_RESOURCE, "raw_list", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT id, raw_isbn, metadata_json FROM capture_staging "
            "WHERE status = 'raw' ORDER BY id LIMIT ?", (limit,)).fetchall()

    def detail(self, staging_id: int):
        """(id, form, raw_isbn, image_path, free_text_note, status, metadata_json) for one row, or
        None — the staging detail/resolve source."""
        self._a.authorize(Action(_RESOURCE, "detail", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT id, form, raw_isbn, image_path, free_text_note, status, metadata_json "
            "FROM capture_staging WHERE id = ?", (staging_id,)).fetchone()

    def not_in_catalogue(self, limit: int = 200):
        """(id, raw_isbn, free_text_note, captured_at, status, metadata_json) for every phone ('ios')
        capture whose verdict was NOT 'already in catalogue', newest first — the capture log."""
        self._a.authorize(Action(_RESOURCE, "not_in_catalogue", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT id, raw_isbn, free_text_note, COALESCE(scanned_at, created_at) AS captured_at, "
            "       status, metadata_json "
            "FROM capture_staging WHERE source = 'ios' AND COALESCE(in_catalogue, 0) = 0 "
            "ORDER BY captured_at DESC, id DESC LIMIT ?", (limit,)).fetchall()

    # ── writes (staged) ──────────────────────────────────────────────────────────
    def stage_isbn(self, isbn: str, source: str, scanned_at=None) -> tuple:
        """Idempotent insert of one §14 ISBN capture, deduped on the open ('raw') row for the same
        ISBN. Returns (staging_id, duplicate). Staged; caller commits."""
        self._a.authorize(Action(_RESOURCE, "stage", AccessMode.WRITE))
        cur = self._a.rw.execute(
            "INSERT OR IGNORE INTO capture_staging (form, raw_isbn, source, scanned_at, status) "
            "VALUES ('physical', ?, ?, ?, 'raw')", (isbn, source, scanned_at))
        if cur.rowcount == 1:
            return cur.lastrowid, False
        return self.open_raw_id(isbn), True

    def insert(self, *, or_ignore: bool = False, **cols) -> "int | None":
        """Insert a capture row from whitelisted columns; returns the new id (or None when `or_ignore`
        hit an existing open-ISBN row). Staged."""
        self._a.authorize(Action(_RESOURCE, "insert", AccessMode.WRITE))
        bad = set(cols) - set(_INSERTABLE)
        if bad:
            raise ValueError(f"refusing to insert unknown capture columns: {sorted(bad)}")
        keys = list(cols)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        cur = self._a.rw.execute(
            f"{verb} INTO capture_staging ({', '.join(keys)}) "
            f"VALUES ({', '.join('?' * len(keys))})", tuple(cols[k] for k in keys))
        return cur.lastrowid if (not or_ignore or cur.rowcount == 1) else None

    def set_metadata_if_empty(self, staging_id: int, metadata) -> None:
        """Stamp metadata_json only when the row has none yet (an idempotent lookup write). Staged."""
        self._a.authorize(Action(_RESOURCE, "update", AccessMode.WRITE))
        self._a.rw.execute(
            "UPDATE capture_staging SET metadata_json = ? "
            "WHERE id = ? AND (metadata_json IS NULL OR metadata_json = '')",
            (metadata if isinstance(metadata, str) else _json.dumps(metadata), staging_id))

    def resolve(self, staging_id: int) -> None:
        """Mark a capture row resolved (it's been attached to a catalogue edition). Staged."""
        self._a.authorize(Action(_RESOURCE, "update", AccessMode.WRITE))
        self._a.rw.execute(
            "UPDATE capture_staging SET status = 'resolved' WHERE id = ?", (staging_id,))

    def discard(self, staging_id: int) -> None:
        """Delete a capture row outright — the operator's "this scan is junk, forget it".
        A capture is intake scratch (no catalogue identity, nothing FK-references it), so a
        hard delete is safe and frees the open-ISBN unique slot for a clean re-scan. Staged."""
        self._a.authorize(Action(_RESOURCE, "delete", AccessMode.WRITE))
        self._a.rw.execute("DELETE FROM capture_staging WHERE id = ?", (staging_id,))

    def set_in_catalogue(self, staging_id: int, in_catalogue: bool) -> None:
        """Stamp the cross-format verdict (in_catalogue 0/1) onto a staging row. Staged."""
        self._a.authorize(Action(_RESOURCE, "update", AccessMode.WRITE))
        self._a.rw.execute("UPDATE capture_staging SET in_catalogue = ? WHERE id = ?",
                           (1 if in_catalogue else 0, staging_id))

    def fill_note_meta_and_stamp(self, staging_id: int, note, metadata, in_catalogue: bool) -> None:
        """Keep any existing note/metadata (fill only when empty) and overwrite the verdict — the
        CIP-page ingest write-back. Staged."""
        self._a.authorize(Action(_RESOURCE, "update", AccessMode.WRITE))
        self._a.rw.execute(
            "UPDATE capture_staging SET free_text_note = COALESCE(free_text_note, ?), "
            "  metadata_json = COALESCE(metadata_json, ?), in_catalogue = ? WHERE id = ?",
            (note, metadata if isinstance(metadata, (str, type(None))) else _json.dumps(metadata),
             1 if in_catalogue else 0, staging_id))

    def merge_attachments(self, staging_id: int, image_path, note, metadata) -> None:
        """Attach a newly-supplied photo/note/metadata to an open row, keeping existing values when
        the new one is None (COALESCE(new, existing)) — the web-form dedup update. Staged."""
        self._a.authorize(Action(_RESOURCE, "update", AccessMode.WRITE))
        self._a.rw.execute(
            "UPDATE capture_staging SET image_path = COALESCE(?, image_path), "
            "  free_text_note = COALESCE(?, free_text_note), "
            "  metadata_json = COALESCE(?, metadata_json) WHERE id = ?",
            (image_path, note, metadata if isinstance(metadata, (str, type(None)))
             else _json.dumps(metadata), staging_id))
