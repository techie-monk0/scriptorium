"""Edition write surface — plan → apply, storage-agnostic.

Edition is the first aggregate with a real **FK cascade** and **semantic orphans**, so its delete
plan covers three closures a holding's didn't:

  * the **holdings** it owns (children) — the plan lists them as `cascades`, and delegates to the
    Holding writer (sibling-aware) to enumerate their non-FK closure (`ref_purges` + `file_ops`),
    since under soft-delete nothing cascades automatically.
  * the **non-FK file closure** the edition itself owns: its `e<id>*` cover/spine/pin art
    (orphan-audit #3), enumerated as `file_ops` to trash.
  * **semantic orphans**: a live Work left with no other live edition. The mechanism (the store)
    finds them; the client's `OrphanPolicy` decides GC / FLAG / REFUSE per orphan. A REFUSE becomes
    a `block`, making the plan un-appliable.

`apply` re-checks the edition's identity fingerprint (TOCTOU + recycled-id guard), then in one
transaction purges holding caches, HARD-deletes the holdings, **tombstones** the edition (id frozen),
and tombstones the works the policy chose to GC; filesystem effects (cover art + holding files) run
after commit. All persistence goes through `EditionStore`; this layer is policy + orchestration.
See docs/access/entity_api_model.md §5/§6.
"""
from __future__ import annotations

from catalogue.contracts import (
    AccessMode,
    Action,
    BasicGate,
    Block,
    FieldRule,
    FileOp,
    FlagOrphans,
    Impact,
    IntegrityViolation,
    NotFound,
    Orphan,
    OrphanDecision,
    OrphanPolicy,
    Ref,
    StaleWrite,
)

from .. import _crud
from ..registry import HOLDING_FILE_HASH_CACHES

# Columns a create/update may set + their rules (the IntegrityGate). title is the identity anchor.
_GATE = BasicGate({"edition": {
    "title": FieldRule(required=True, max_len=500),
    "subtitle": FieldRule(max_len=500),
    "isbn": FieldRule(max_len=20),
    "year": FieldRule(),
    "publisher": FieldRule(max_len=300),
    "tradition": FieldRule(max_len=200),
}})


class EditionWriter:
    RESOURCE = "edition"

    def __init__(self, access, store):
        self._a = access
        self._s = store
        self._gate = _GATE

    # ── create / update (shared CRUD mechanics) ────────────────────────────────
    def plan_create(self, values: dict) -> Impact:
        """Plan creating an edition: gate the payload (title required). Un-appliable if malformed."""
        return _crud.plan_create(self._a, self.RESOURCE, self._gate, values)

    def create(self, values: dict, idempotency_key=None) -> Impact:
        """Create an edition in one transaction; returns the Impact with the assigned id. Standalone
        (not Session-staged), WRITE-gated. `idempotency_key` dedups a retried create."""
        return _crud.create(self._a, self.RESOURCE, self._s, self._gate, values, idempotency_key)

    def store_detection(self, edition_id: int, kind: str, payload_json: str) -> None:
        """Upsert an edition's work-detection cache (one row per edition). WRITE-gated; staged."""
        self._a.authorize(Action(self.RESOURCE, "store_detection", AccessMode.WRITE))
        self._s.store_detection(edition_id, kind, payload_json)

    def set_structure(self, edition_id: int, value) -> None:
        """Set/clear an edition's `structure` (single_work | multi_work | None). A direct scalar
        command (WRITE-gated); stages on the connection, the caller owns the commit."""
        self._a.authorize(Action(self.RESOURCE, "set_structure", AccessMode.WRITE))
        self._s.set_structure(edition_id, value)

    def set_columns(self, edition_id: int, values: dict) -> None:
        """Update whitelisted scalar metadata (title/subtitle/publisher/year/isbn) — a direct
        command for filename-detection backfill; stages on the connection, the caller commits."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.set_columns(edition_id, values)

    def add_isbn(self, edition_id: int, isbn: str, note: "str | None" = None) -> None:
        """Record an edition_isbn alias (WRITE-gated, staged; caller commits)."""
        self._a.authorize(Action(self.RESOURCE, "add_isbn", AccessMode.WRITE))
        self._s.add_isbn(edition_id, isbn, note)

    def set_ol_work_key(self, edition_id: int, key: str, *, only_if_empty: bool = False) -> None:
        """Store the resolved OpenLibrary work key on an edition (cross-format clustering). With
        `only_if_empty` it never clobbers an existing key. WRITE-gated, staged; caller commits."""
        self._a.authorize(Action(self.RESOURCE, "set_ol_work_key", AccessMode.WRITE))
        self._s.set_ol_work_key(edition_id, key, only_if_empty=only_if_empty)

    def set_review_status(self, edition_id: int, status: str) -> None:
        """Flag/clear an edition's review_status (e.g. 'needs_fix' on a fresh sidecar ingest).
        WRITE-gated; staged on the connection, the caller commits."""
        self._a.authorize(Action(self.RESOURCE, "set_review_status", AccessMode.WRITE))
        self._s.set_review_status(edition_id, status)

    def set_review_verdict(self, edition_id: int, status, flags_json, note, *, stamp: bool) -> None:
        """Write the curation review verdict (status/flags/note; `stamp` sets reviewed_at). Staged."""
        self._a.authorize(Action(self.RESOURCE, "set_review", AccessMode.WRITE))
        self._s.set_review_verdict(edition_id, status, flags_json, note, stamp=stamp)

    def set_volume_set(self, edition_id: int, set_id, volume_seq) -> None:
        """Mark an edition's membership in a multi-volume set (volume_set_id + volume_seq). Staged."""
        self._a.authorize(Action(self.RESOURCE, "set_volume_set", AccessMode.WRITE))
        self._s.set_volume_set(edition_id, set_id, volume_seq)

    def add_contained(self, edition_id: int, work_id: int, sequence: int, translator=None,
                      section=None, locator_type=None, note=None) -> None:
        """Insert a full edition_work contained-work link (all per-appearance fields). Staged."""
        self._a.authorize(Action(self.RESOURCE, "link_work", AccessMode.WRITE))
        self._s.add_contained(edition_id, work_id, sequence, translator, section, locator_type, note)

    def remove_contained(self, edition_id: int, work_id: int, sequence: int) -> None:
        """Delete the edition_work link keyed by (edition, work, sequence). Staged."""
        self._a.authorize(Action(self.RESOURCE, "link_work", AccessMode.WRITE))
        self._s.remove_contained(edition_id, work_id, sequence)

    def update_contained(self, edition_id: int, work_id: int, old_sequence: int, new_sequence: int,
                         translator=None, section=None, locator_type=None, note=None) -> int:
        """Edit a contained-work link in place (keyed by old sequence); returns rowcount. Staged."""
        self._a.authorize(Action(self.RESOURCE, "link_work", AccessMode.WRITE))
        return self._s.update_contained(edition_id, work_id, old_sequence, new_sequence, translator,
                                        section, locator_type, note)

    def add_modern_commentary(self, edition_id: int, work_id: int) -> None:
        """Record (idempotently) that the edition is a modern commentary on `work_id`. Staged."""
        self._a.authorize(Action(self.RESOURCE, "modern_commentary", AccessMode.WRITE))
        self._s.add_modern_commentary(edition_id, work_id)

    def remove_modern_commentary(self, edition_id: int, work_id: int) -> None:
        """Drop one edition→work modern-commentary edge. Staged."""
        self._a.authorize(Action(self.RESOURCE, "modern_commentary", AccessMode.WRITE))
        self._s.remove_modern_commentary(edition_id, work_id)

    def merge(self, loser_id: int, winner_id: int) -> None:
        """Fold edition `loser` into `winner` (manifestation dedup): re-point its holdings +
        edition-keyed edges onto the winner, then HARD-delete the loser row (FK cascades leftovers).
        A direct staged command — the CALLER owns the commit, the undo snapshot, and busting the
        loser's `e<id>` cover art (matching the legacy entity_undo/match/consolidate behavior). No-op
        on a self-merge; the caller checks endpoint existence (via `reads.get`) first. WRITE-gated."""
        self._a.authorize(Action(self.RESOURCE, "merge", AccessMode.WRITE))
        if loser_id == winner_id:
            return
        # Policy layer: consult each external tool that depends on the loser. A tool may DISALLOW the
        # merge (→ CapabilityRestricted) or just WARN + require the loser to forward (which merge_into
        # already does for a cited loser). Lazy import keeps this off the package-init path.
        from catalogue.contracts import Capability
        from .. import tool_policy
        tool_policy.enforce(self._a.rw, Capability.MERGE, loser_id)
        self._s.merge_into(loser_id, winner_id)

    def plan_update(self, ref: Ref, values: dict) -> Impact:
        """Plan updating an edition's columns: gate the payload + pin the identity fingerprint."""
        return _crud.plan_update(self._a, self.RESOURCE, self._gate, self._s, ref, values)

    # ── plan (read) ──────────────────────────────────────────────────────────
    def plan_delete(self, ref: Ref, policy: "OrphanPolicy | None" = None) -> Impact:
        """Plan deleting an edition: its holdings (as `cascades`) + their non-FK closure + this
        edition's cover art (`ref_purges`/`file_ops`), and any Work orphaned (per `policy`, FLAG by
        default)."""
        self._a.authorize(Action(self.RESOURCE, "delete", AccessMode.READ))
        policy = policy or FlagOrphans()
        edition = self._s.get(ref.id)
        if edition is None:   # absent or already tombstoned
            return Impact("delete", ref, blocks=(Block("not_found", f"edition {ref.id} not found"),))
        target = edition.ref()

        # Holdings (children) + their non-FK closure. Delegate to the Holding writer with the full
        # sibling set so a file/hash shared only among the doomed holdings is purged, not kept.
        hids = self._s.holding_ids(ref.id)
        sibs = frozenset(hids)
        cascades: tuple[Ref, ...] = ()
        ref_purges = ()
        file_ops: tuple[FileOp, ...] = ()
        hw = self._a.holdings.writes
        for hid in hids:
            sub = hw.plan_delete(Ref("holding", hid), also_deleting=sibs - {hid})
            cascades += (sub.target,)
            ref_purges += sub.ref_purges
            file_ops += sub.file_ops

        # This edition's own id-keyed cover/spine/pin art (orphan-audit #3) — trash what exists.
        file_ops += tuple(FileOp("trash", p) for p in self._s.art_files(ref.id))

        # Semantic orphans: a live Work that would have no other live edition once this is tombstoned.
        orphans: tuple[Orphan, ...] = ()
        blocks: tuple[Block, ...] = ()
        for wid in self._s.orphaned_work_ids(ref.id):
            wref = Ref("work", wid)
            reason = f"work {wid} would have no remaining live edition"
            decision = policy.decide(wref, reason)
            orphans += (Orphan(wref, reason, decision),)
            if decision == OrphanDecision.REFUSE:
                blocks += (Block("orphan_refuse", reason),)

        return Impact("delete", target, cascades=cascades, orphans=orphans,
                      ref_purges=ref_purges, file_ops=file_ops, blocks=blocks)

    # ── stage / apply (write) ─────────────────────────────────────────────────
    def _stage(self, impact: Impact):
        """Authorize + recheck + stage the edition delete (no commit). Returns the file_ops (cover
        art + holding files) for the caller to trash after the transaction commits."""
        self._a.authorize(Action(self.RESOURCE, impact.op, AccessMode.WRITE))
        if impact.op not in ("delete", "update"):
            raise IntegrityViolation(f"unsupported edition op: {impact.op!r}")
        if not impact.appliable:
            raise IntegrityViolation("; ".join(b.message for b in impact.blocks))
        self._a._audit(impact)
        if impact.op == "update":
            _crud.stage_update(self._s, self.RESOURCE, impact.target, impact.changes)
            return ()
        self._recheck(impact.target)
        # Pre-destructive checkpoint: snapshot the holdings BEFORE the hard-delete so `restore` can
        # bring them back (soft-delete only tombstones the edition row; its children are removed).
        self._a._checkpoint(impact, {"holding": self._s.snapshot_holdings(impact.target.id)})
        # Children HARD-delete: holdings (≈1-to-1 with a file) + their hash caches; files trash
        # post-commit. reading_position FK-cascades off the holding delete.
        self._purge_caches(impact)
        self._s.delete_holdings(impact.target.id)
        # Root SOFT-deletes: tombstone the edition (id frozen → cover/review refs stay safe).
        self._s.tombstone(impact.target.id)
        # GC'd orphan works run the FULL work-delete closure, staged on THIS same transaction:
        # tombstone the now-unanchored root AND purge its work-OWNED review/promotion refs — so a
        # GC'd orphan is consistent with a direct `acc.works.writes.delete` (no dangling pending
        # review items left behind). Falls back to a bare tombstone if the work is already gone.
        for o in impact.orphans:
            if o.decision != OrphanDecision.GC:
                continue
            work_plan = self._a.works.writes.plan_delete(o.ref)
            if work_plan.appliable:
                self._a.works.writes._stage(work_plan)
            else:
                self._s.tombstone_work(o.ref.id)
        return impact.file_ops

    def apply(self, impact: Impact) -> Impact:
        """Execute a planned edition delete in one transaction; filesystem effects after commit."""
        try:
            file_ops = self._stage(impact)
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise
        self._a.backing.run(file_ops, self._a.trash_dir)   # non-transactional; after the rows are gone
        return impact

    def restore(self, ref: Ref) -> None:
        """Un-delete a tombstoned edition (clear `deleted_at`) — restores the metadata shell, its
        edge-links, AND the holdings the delete hard-removed, re-inserted from the pre-destructive
        checkpoint (their files wait in `.trash/` — recover those by hand). WRITE-gated."""
        self._a.authorize(Action(self.RESOURCE, "restore", AccessMode.WRITE))
        try:
            snap = self._a._latest_checkpoint("edition", ref.id)
            self._s.restore(ref.id)
            if snap and snap.get("holding"):
                self._s.restore_holdings(snap["holding"])
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise

    # ── internals ────────────────────────────────────────────────────────────
    def _recheck(self, target: Ref) -> None:
        """TOCTOU + recycled-id guard (identity fingerprint) + optimistic-concurrency (`rev`): the
        edition must still be live and unchanged since the plan — else `StaleWrite`/`NotFound`."""
        _crud.recheck(self._s, self.RESOURCE, target)

    def _purge_caches(self, impact: Impact) -> None:
        for p in impact.ref_purges:
            if p.kind != "cache_row":
                raise IntegrityViolation(f"unknown ref_purge kind: {p.kind!r}")
            table, key = p.locator.split(":", 1)
            if table not in HOLDING_FILE_HASH_CACHES:     # whitelist — no injected table names
                raise IntegrityViolation(f"refusing to purge unregistered table {table!r}")
            self._s.purge_holding_cache(table, key)
