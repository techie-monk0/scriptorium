"""Work write surface — plan → apply for both `delete` and `merge`, storage-agnostic.

Work is the hub of the FRBR graph, so it is the first aggregate with two write ops:

  * **delete** — tombstone the work (soft-delete; id frozen). Its edges (edition_work, work_author,
    subjects/traditions/collections, relationships) ride the tombstone — reads already hide a dead
    work, and `restore` brings the shell back. The only non-FK closure is the **work-owned** review
    queue / promotion refs (the over-purge-safe registry, §6): a work delete purges ONLY items it
    OWNS (`work_authorship`/`work_canonical`), never the secondary `work_id` an edition-owned
    `title_proposal`/`edition_metadata` carries — the bug that dropped ~254 proposals for live
    editions.
  * **merge** — fold a duplicate work into a canonical winner: re-point every edge (editions,
    authors, relationships, commentary, collections/subjects/traditions, aliases) onto the winner,
    backfill the winner's empty scalar fields, re-point the work-owned non-FK refs (the decision
    survives onto the winner), then tombstone the loser. The Impact previews the moves as
    `LinkRepoint`s. Guards self-merge and a canonical-id conflict.

`apply` re-checks BOTH endpoints' identity fingerprints (TOCTOU + recycled-id guard) before staging.
No filesystem effects. All persistence goes through `WorkStore`; this layer is policy + the merge
contract, and stages cleanly into a `Session`. See entity_api_model.md §5/§6.
"""
from __future__ import annotations

from catalogue.contracts import (
    AccessMode,
    Action,
    BasicGate,
    Block,
    FieldRule,
    Impact,
    IntegrityViolation,
    LinkRepoint,
    NotFound,
    Ref,
    RefPurge,
    StaleWrite,
    allowed_values,
    get_field,
)

from .. import _crud


def _choices(name: str) -> "tuple[str, ...] | None":
    """The controlled-vocab `choices` for a strict work field (genre/tenet_system) from the
    CategoricalField registry, or None for a non-strict / non-registered field — so the gate and
    the store read the SAME vocabulary."""
    f = get_field("work", name)
    return allowed_values(f) if (f and f.strict and not f.open_vocab and f.values) else None


# SCALAR work columns a create/update may set. A work's title (a representative alias) and its authors
# are EDGES, not columns — managed via separate alias/author ops, not gated here. The controlled-vocab
# fields carry `choices` sourced from the registry (genre/tenet_system reject out-of-vocab values).
_GATE = BasicGate({"work": {
    "canonical_system": FieldRule(max_len=50),
    "canonical_number": FieldRule(max_len=50),
    "work_type": FieldRule(max_len=50),
    "original_language": FieldRule(max_len=50),
    "era": FieldRule(max_len=50),
    "sanskrit_title": FieldRule(max_len=300),
    "tibetan_title": FieldRule(max_len=300),
    "notes": FieldRule(),
    "tradition": FieldRule(max_len=200),
    "genre": FieldRule(max_len=50, choices=_choices("genre")),
    "tenet_system": FieldRule(max_len=100, choices=_choices("tenet_system")),
}})


class WorkWriter:
    RESOURCE = "work"

    def __init__(self, access, store):
        self._a = access
        self._s = store
        self._gate = _GATE

    # ── create / update (shared CRUD mechanics; SCALAR columns only) ─────────────
    def plan_create(self, values: dict) -> Impact:
        """Plan creating a work from its SCALAR columns (title/authors are edges, added separately)."""
        return _crud.plan_create(self._a, self.RESOURCE, self._gate, values)

    def create(self, values: dict, idempotency_key=None) -> Impact:
        """Create a work (scalar columns) in one transaction; returns the Impact with the assigned id.
        Standalone (not Session-staged), WRITE-gated. The new work has no alias/authors yet.
        `idempotency_key` dedups a retried create."""
        return _crud.create(self._a, self.RESOURCE, self._s, self._gate, values, idempotency_key)

    def plan_update(self, ref: Ref, values: dict) -> Impact:
        """Plan updating a work's SCALAR columns: gate the payload + pin the identity fingerprint."""
        return _crud.plan_update(self._a, self.RESOURCE, self._gate, self._s, ref, values)

    # ── work-authority direct commands (identity at creation; staged, caller commits) ─
    def insert_work(self, values: dict) -> int:
        """Insert a work from SCALAR columns (no alias/edges) and return its id — the identity-engine
        create (`get_or_create_work`'s fresh-work branch). WRITE-gated; stages, the caller commits."""
        self._a.authorize(Action(self.RESOURCE, "create", AccessMode.WRITE))
        return self._s.insert_scalars(values)

    def fill_scalars(self, work_id: int, values: dict) -> None:
        """Fill whitelisted scalar columns ONLY where empty (`COALESCE`) — native-title backfill and
        the manual-add era/type fill that must never clobber a curated value. Staged; caller commits."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.coalesce_scalars(work_id, values)

    def set_scalars(self, work_id: int, values: dict) -> None:
        """Overwrite whitelisted scalar columns outright — the manual-add edit that replaces a
        curated value (e.g. era). Staged; caller commits."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.set_scalars(work_id, values)

    def set_native_title(self, work_id: int, column: str, value) -> None:
        """Re-derive a denormalized native-title column from the aliases (value or None). Staged."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.set_native_title(work_id, column, value)

    def set_work_type(self, work_id: int, work_type) -> None:
        """Set a work's open-vocab type (registering an unseen code first). Staged; caller commits."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.set_work_type(work_id, work_type)

    def relate_commentary(self, commentary_wid: int, root_wid: int) -> None:
        """Record commentary_wid --commentary_on--> root_wid (idempotent) + mark both types. Staged."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.relate_commentary(commentary_wid, root_wid)

    def update_alias(self, alias_id: int, text: str) -> None:
        """Rewrite a work_alias's text in place (+ its fold-keyed normalized_key) — the title-swap
        primary-alias rewrite. Staged; caller commits."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.update_alias(alias_id, text)

    def rename_alias_checked(self, alias_id: int, work_id: int, text: str) -> bool:
        """Rename an alias only if it belongs to `work_id`; returns whether a row changed. Staged."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        return self._s.rename_alias_checked(alias_id, work_id, text)

    def set_alias_fields(self, alias_id: int, text: str, scheme: str) -> None:
        """Set an alias's text + scheme (+ refolded key) — the primary-title swap. Staged."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.set_alias_fields(alias_id, text, scheme)

    def remove_author(self, work_id: int, person_id: int, role: str) -> None:
        """Drop one work_author (work_id, person_id, role) link. Staged."""
        self._a.authorize(Action(self.RESOURCE, "unlink", AccessMode.WRITE))
        self._s.remove_author(work_id, person_id, role)

    def set_edition_work_note(self, edition_id: int, work_id: int, note) -> None:
        """Set the per-appearance note on an edition_work join row. Staged."""
        self._a.authorize(Action(self.RESOURCE, "link", AccessMode.WRITE))
        self._s.set_edition_work_note(edition_id, work_id, note)

    def unrelate_commentary(self, work_id: int, *, as_root: bool = False) -> None:
        """Drop the work's commentary_on edge(s) — as the commentary (default) or as the root. Staged."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.unrelate_commentary(work_id, as_root=as_root)

    def delete_alias(self, alias_id: int) -> None:
        """Delete one work_alias by id (revert an apply-created alias). Staged; caller commits."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.delete_alias(alias_id)

    def delete_aliases_by_scheme(self, work_id: int, scheme: str) -> None:
        """Delete every alias of a work in one scheme (placeholder-title resync). Staged."""
        self._a.authorize(Action(self.RESOURCE, "update", AccessMode.WRITE))
        self._s.delete_aliases_by_scheme(work_id, scheme)

    def link_to_edition(self, edition_id: int, work_id: int, sequence: int, locator=None) -> None:
        """Upsert the edition_work link (set sequence + section_locator, else insert). Staged."""
        self._a.authorize(Action(self.RESOURCE, "link", AccessMode.WRITE))
        self._s.link_to_edition(edition_id, work_id, sequence, locator)

    def unlink_from_edition(self, edition_id: int, work_id: int) -> None:
        """Remove the edition_work link(s) for (edition, work). Staged."""
        self._a.authorize(Action(self.RESOURCE, "unlink", AccessMode.WRITE))
        self._s.unlink_from_edition(edition_id, work_id)

    def set_review_status(self, work_id: int, status) -> None:
        """Set a work's review verdict ('ok' clears it from the queue, stamps reviewed_at;
        'needs_fix' keeps it; None = unreviewed, clears the stamp). Staged; caller commits."""
        self._a.authorize(Action(self.RESOURCE, "set_review", AccessMode.WRITE))
        self._s.set_review_status(work_id, status)

    def hard_delete(self, work_id: int) -> None:
        """HARD-delete an auto-minted PLACEHOLDER work (the throwaway-GC removal works_apply /
        promote.revert do — NOT a tombstone). Cascades its aliases/authors. The caller has already
        established the work is a degenerate placeholder. WRITE-gated; staged, caller commits."""
        self._a.authorize(Action(self.RESOURCE, "hard_delete", AccessMode.WRITE))
        self._s.hard_delete(work_id)

    # ── plan: delete ───────────────────────────────────────────────────────────
    def plan_delete(self, ref: Ref) -> Impact:
        """Plan deleting a work: a tombstone (edges ride along) plus the work-OWNED review/promotion
        refs to purge. Un-appliable if the work is gone or already tombstoned."""
        self._a.authorize(Action(self.RESOURCE, "delete", AccessMode.READ))
        work = self._s.get(ref.id)
        if work is None:
            return Impact("delete", ref, blocks=(Block("not_found", f"work {ref.id} not found"),))
        target = work.ref()
        ref_purges = tuple(
            RefPurge("queue_payload", f"review_queue:{rid}", target)
            for rid in self._s.owned_review_item_ids(ref.id))
        ref_purges += tuple(
            RefPurge("promotion_json", f"promotion.work_ids:{rid}", target)
            for rid in self._s.promotion_arrays_with(ref.id))
        return Impact("delete", target, ref_purges=ref_purges)

    # ── plan: merge ────────────────────────────────────────────────────────────
    def plan_merge(self, loser: Ref, winner: Ref) -> Impact:
        """Plan folding `loser` into `winner`: the edges that re-point (as `LinkRepoint`s), the
        aliases gained, the resolved canonical id. No mutation. Blocks self-merge, a missing/dead
        endpoint, or a conflicting canonical id."""
        self._a.authorize(Action(self.RESOURCE, "merge", AccessMode.READ))
        if loser.id == winner.id:
            return Impact("merge", loser, blocks=(Block("invalid", "cannot merge a work into itself"),))
        lose = self._s.get(loser.id)
        win = self._s.get(winner.id)
        blocks: tuple[Block, ...] = ()
        if lose is None:
            blocks += (Block("not_found", f"work {loser.id} not found"),)
        if win is None:
            blocks += (Block("not_found", f"work {winner.id} (merge target) not found"),)
        if blocks:
            return Impact("merge", loser, blocks=blocks)
        ln, wn = lose.canonical_number, win.canonical_number
        if ln and wn and ln != wn:
            return Impact("merge", lose.ref(), changes={"into": win.ref().to_dict()},
                          blocks=(Block("conflict",
                                        f"different canonical numbers ({lose.canonical_system}:{ln} "
                                        f"vs {win.canonical_system}:{wn}) — resolve before merging"),))
        l_ref, w_ref = lose.ref(), win.ref()
        repoints = tuple(LinkRepoint(edge, l_ref, w_ref)
                         for edge, n in self._s.edge_counts(loser.id).items() if n)
        if self._s.owned_review_item_ids(loser.id):
            repoints += (LinkRepoint("review_queue.work_id", l_ref, w_ref),)
        if self._s.promotion_arrays_with(loser.id):
            repoints += (LinkRepoint("promotion.work_ids", l_ref, w_ref),)
        changes = {"into": w_ref.to_dict(),
                   "aliases_gained": self._s.alias_gain(loser.id, winner.id),
                   "canonical_after": [win.canonical_system or lose.canonical_system, wn or ln]}
        return Impact("merge", l_ref, changes=changes, link_repoints=repoints)

    # ── stage / apply (write) ───────────────────────────────────────────────────
    def _stage(self, impact: Impact):
        """Authorize + recheck + stage a work delete or merge (no commit). Returns () — a work owns
        no files."""
        self._a.authorize(Action(self.RESOURCE, impact.op, AccessMode.WRITE))
        if not impact.appliable:
            raise IntegrityViolation("; ".join(b.message for b in impact.blocks))
        self._a._audit(impact)
        if impact.op == "update":
            _crud.stage_update(self._s, self.RESOURCE, impact.target, impact.changes)
        elif impact.op == "delete":
            self._recheck(impact.target)
            self._s.tombstone(impact.target.id)
            self._purge_refs(impact)
        elif impact.op == "merge":
            winner = self._winner_ref(impact)
            self._recheck(impact.target)            # loser still live + identity intact
            self._recheck(winner)                   # winner still live + identity intact
            self._s.merge(impact.target.id, winner.id)
        else:
            raise IntegrityViolation(f"unsupported work op: {impact.op!r}")
        return ()

    def apply(self, impact: Impact) -> Impact:
        """Execute a planned work delete/merge in one transaction (no filesystem effects)."""
        try:
            self._stage(impact)
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise
        return impact

    def restore(self, ref: Ref) -> None:
        """Un-delete a tombstoned work (clear `deleted_at`) — restores the row + its aliases/edges.
        NOTE: a work tombstoned by a MERGE comes back as an edgeless shell (its edges moved to the
        winner); restore is not a merge-undo. WRITE-gated."""
        self._a.authorize(Action(self.RESOURCE, "restore", AccessMode.WRITE))
        try:
            self._s.restore(ref.id)
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise

    # ── internals ────────────────────────────────────────────────────────────
    def _purge_refs(self, impact: Impact) -> None:
        for p in impact.ref_purges:
            table, key = p.locator.split(":", 1)
            if p.kind == "queue_payload":
                self._s.purge_review_item(int(key))
            elif p.kind == "promotion_json":
                _, rid = key.split(":", 1) if ":" in key else (None, key)
                self._s.scrub_promotion_work(int(rid), impact.target.id)
            else:
                raise IntegrityViolation(f"unknown ref_purge kind: {p.kind!r}")

    def _winner_ref(self, impact: Impact) -> Ref:
        into = impact.changes.get("into")
        if not into:
            raise IntegrityViolation("merge Impact missing its winner ('into')")
        return Ref.from_dict(into)

    def _recheck(self, target: Ref) -> None:
        """TOCTOU + recycled-id guard (identity fingerprint: title-fold + author set) + optimistic-
        concurrency (`rev`): the work must still be live and unchanged since the plan — else
        `StaleWrite`/`NotFound`. Used for both endpoints of a merge."""
        _crud.recheck(self._s, self.RESOURCE, target)
