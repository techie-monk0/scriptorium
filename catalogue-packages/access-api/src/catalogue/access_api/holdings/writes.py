"""Holding write surface — plan → apply, storage-agnostic.

`plan_*` is a READ: it computes a serializable `Impact` and mutates nothing — an un-appliable Impact
(carrying `blocks`) when validation fails. `apply` is a WRITE (policy-gated): it refuses a blocked
plan, **re-checks integrity** against current state (TOCTOU + recycled-id guard via the target's
fingerprint → `StaleWrite`/`NotFound`), then executes in one transaction (DB-level non-FK purges
included), and performs filesystem effects after commit. All persistence goes through the
`HoldingStore`; this layer is policy + plan→apply orchestration, no SQL. See entity_api_model.md §5/§6.
"""
from __future__ import annotations

from catalogue.contracts import (
    AccessMode,
    Action,
    Block,
    FileOp,
    Impact,
    IntegrityViolation,
    NotFound,
    Ref,
    RefPurge,
    StaleWrite,
    ValidationError,
)

from ..registry import HOLDING_FILE_HASH_CACHES

# Columns a writer may set — guards against a crafted/deserialized Impact injecting column names.
_UPDATABLE = {"text_status", "shelf_location", "notes",
              "ocr_quality_score", "archival_pdf_path", "digitizer_used",
              "form", "holding_type"}


class HoldingWriter:
    RESOURCE = "holding"

    def __init__(self, access, store):
        self._a = access
        self._s = store

    # ── direct commands (staged; caller commits) ──────────────────────────────
    def set_columns(self, holding_id: int, changes: dict) -> None:
        """Set whitelisted holding columns (OCR result, shelf, status). WRITE-gated; stages on the
        connection, the caller commits. Rejects any column outside `_UPDATABLE`."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        bad = set(changes) - _UPDATABLE
        if bad:
            raise ValidationError(f"refusing to set non-whitelisted holding columns: {sorted(bad)}")
        self._s.update(holding_id, changes)

    def append_note(self, holding_id: int, note: str) -> None:
        """Append a provenance note to holding.notes (own line; never clobbers). Staged."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.append_note(holding_id, note)

    def set_root(self, holding_id: int, root_id: int) -> None:
        """Set holding.root_id (library-root attribution backfill). WRITE-gated; staged."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.set_root(holding_id, root_id)

    def set_location(self, holding_id: int, file_path: str, file_hash) -> None:
        """Set holding.file_path + file_hash (prefix repoint / re-hash). WRITE-gated; staged."""
        self._a.authorize(Action(self.RESOURCE, "relocate", AccessMode.WRITE))
        self._s.set_location(holding_id, file_path, file_hash)

    def set_file_path(self, holding_id: int, path: str) -> None:
        """Set holding.file_path only (a moved file). WRITE-gated; staged."""
        self._a.authorize(Action(self.RESOURCE, "relocate", AccessMode.WRITE))
        self._s.set_file_path(holding_id, path)

    def mark_opened(self, holding_id: int) -> None:
        """Stamp holding.last_opened = now (the 'Recently opened' ordering). WRITE-gated; staged."""
        self._a.authorize(Action(self.RESOURCE, "mark_opened", AccessMode.WRITE))
        self._s.mark_opened(holding_id)

    def set_text_status_by_hash(self, file_hash: str, status: str) -> None:
        """Set text_status for every holding carrying `file_hash` (OCR-quality override). WRITE-gated;
        staged."""
        self._a.authorize(Action(self.RESOURCE, "set_text_status", AccessMode.WRITE))
        self._s.set_text_status_by_hash(file_hash, status)

    def set_hashes(self, holding_id: int, file_hash, content_hash) -> None:
        """Set holding.file_hash + content_hash (in-place re-bytes). WRITE-gated; staged."""
        self._a.authorize(Action(self.RESOURCE, "relocate", AccessMode.WRITE))
        self._s.set_hashes(holding_id, file_hash, content_hash)

    def set_file_hash(self, holding_id: int, file_hash) -> None:
        """Set holding.file_hash alone (the rehash CLI's per-row update). WRITE-gated; staged."""
        self._a.authorize(Action(self.RESOURCE, "relocate", AccessMode.WRITE))
        self._s.set_file_hash(holding_id, file_hash)

    def set_path_hashes(self, holding_id: int, path: str, file_hash, content_hash) -> None:
        """Set holding.file_path + file_hash + content_hash (a superseding file). WRITE-gated; staged."""
        self._a.authorize(Action(self.RESOURCE, "relocate", AccessMode.WRITE))
        self._s.set_path_hashes(holding_id, path, file_hash, content_hash)

    def insert_holding(self, **cols) -> int:
        """Insert a holding from whitelisted columns; return its id. WRITE-gated; staged."""
        self._a.authorize(Action(self.RESOURCE, "create", AccessMode.WRITE))
        return self._s.insert_holding(**cols)

    def set_filed(self, holding_id: int, file_path: str, root_id) -> None:
        """Set holding.file_path + root_id after a filing move (bytes unchanged → file_hash kept).
        WRITE-gated; staged."""
        self._a.authorize(Action(self.RESOURCE, "relocate", AccessMode.WRITE))
        self._s.update(holding_id, {"file_path": file_path, "root_id": root_id})

    # ── plan (read) ──────────────────────────────────────────────────────────
    def plan_set_text_status(self, ref: Ref, status: str) -> Impact:
        """Plan setting holding.text_status. Un-appliable (with a block) if the holding is gone
        or `status` is not a known text_status code."""
        self._a.authorize(Action(self.RESOURCE, "set_text_status", AccessMode.READ))
        holding = self._s.get(ref.id)
        if holding is None:
            return Impact("update", ref, blocks=(Block("not_found", f"holding {ref.id} not found"),))
        target = holding.ref()
        if status not in self._s.text_status_codes():
            return Impact("update", target, blocks=(
                Block("validation", f"text_status {status!r} not a known code"),))
        return Impact("update", target, changes={"text_status": status})

    def plan_delete(self, ref: Ref, also_deleting: "frozenset[int]" = frozenset()) -> Impact:
        """Plan deleting a holding. The Impact enumerates the **non-FK closure** the registry
        owns: the file_hash-keyed caches (purged only if no other holding shares the hash) and
        the on-disk files to trash (only if no other holding references the path).

        `also_deleting` are sibling holding ids being deleted in the SAME operation (e.g. all
        holdings of an edition being deleted) — they're excluded from the last-reference checks,
        so a hash/file shared only among the doomed siblings is still purged, not falsely kept."""
        self._a.authorize(Action(self.RESOURCE, "delete", AccessMode.READ))
        f = self._s.delete_fields(ref.id)
        if f is None:
            return Impact("delete", ref, blocks=(Block("not_found", f"holding {ref.id} not found"),))
        target = Ref("holding", ref.id, f["content_hash"])
        exclude = frozenset({ref.id}) | also_deleting

        purges: tuple[RefPurge, ...] = ()
        if f["file_hash"] and not self._s.shares_file_hash(f["file_hash"], exclude):
            purges = tuple(RefPurge("cache_row", f"{t}:{f['file_hash']}", target)
                           for t in HOLDING_FILE_HASH_CACHES)

        file_ops: tuple[FileOp, ...] = ()
        for path in (f["file_path"], f["archival_pdf_path"]):
            if path and not self._s.shares_file_path(path, exclude):
                file_ops += (FileOp("trash", path),)

        return Impact("delete", target, ref_purges=purges, file_ops=file_ops)

    # ── stage / apply (write) ─────────────────────────────────────────────────
    def _stage(self, impact: Impact):
        """Authorize + validate + recheck + stage the mutations (no commit). Returns the file_ops
        to run after the transaction commits — so a `Session` can defer them past its single
        commit (a bare `apply` is just a one-op session)."""
        self._a.authorize(Action(self.RESOURCE, impact.op, AccessMode.WRITE))
        if not impact.appliable:
            raise IntegrityViolation("; ".join(b.message for b in impact.blocks))
        self._recheck(impact.target)
        self._a._audit(impact)
        if impact.op == "update":
            self._apply_update(impact)
            return ()
        if impact.op == "delete":
            self._apply_delete(impact)
            return impact.file_ops
        raise IntegrityViolation(f"unsupported holding op: {impact.op!r}")

    def apply(self, impact: Impact) -> Impact:
        """Execute a planned Impact in one transaction; filesystem effects after commit. Returns
        the applied Impact (the receipt)."""
        try:
            file_ops = self._stage(impact)
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise
        self._a.backing.run(file_ops, self._a.trash_dir)   # non-transactional; after the row is gone
        return impact

    # ── internals ────────────────────────────────────────────────────────────
    def _recheck(self, target: Ref) -> None:
        """TOCTOU + recycled-id guard: the row must still exist and its fingerprint match the
        plan's — else it moved under us (`StaleWrite`) or vanished (`NotFound`)."""
        exists, fingerprint = self._s.current_fingerprint(target.id)
        if not exists:
            raise NotFound(f"holding {target.id} no longer exists")
        if target.fingerprint is not None and fingerprint != target.fingerprint:
            raise StaleWrite(f"holding {target.id} changed since plan (fingerprint mismatch)")

    def _apply_update(self, impact: Impact) -> None:
        bad = set(impact.changes) - _UPDATABLE
        if bad:
            raise ValidationError(f"holding columns not updatable: {sorted(bad)}")
        self._s.update(impact.target.id, impact.changes)

    def _apply_delete(self, impact: Impact) -> None:
        for p in impact.ref_purges:
            if p.kind != "cache_row":
                raise IntegrityViolation(f"unknown ref_purge kind: {p.kind!r}")
            table, key = p.locator.split(":", 1)
            if table not in HOLDING_FILE_HASH_CACHES:    # whitelist — no injected table names
                raise IntegrityViolation(f"refusing to purge unregistered table {table!r}")
            self._s.purge_cache(table, key)
        self._s.delete(impact.target.id)
