"""Person write surface — plan → apply, storage-agnostic.

Person is a soft-delete root whose owned parts (aliases, external-ids) ride along on the tombstone —
the row persists, so its `ON DELETE CASCADE` children stay attached and return on `restore`; there is
no non-FK file/cache closure to purge (unlike Holding/Edition). The one non-trivial closure is
**semantic, second-order**: tombstoning a person can leave a Work with no remaining live author. The
store finds those; the client's `OrphanPolicy` decides GC / FLAG / REFUSE per orphan (FLAG by
default — authorless works are normal here, e.g. anonymous canonical texts, so GC is opt-in). A
REFUSE becomes a `block`, making the plan un-appliable; a GC tombstones the work row.

`apply` re-checks the person's identity fingerprint (TOCTOU + recycled-id guard, the
`person_identity_ok` guard as a `Ref`), then in one transaction tombstones the person and any GC'd
orphan works. No filesystem effects. All persistence goes through `PersonStore`; this layer is policy
+ orchestration, and stages cleanly into a `Session` (the GC'd-work tombstone is the second-order
effect a future `Work` aggregate will deepen into a full work-closure delete). See entity_api_model.md §5/§6.
"""
from __future__ import annotations

from catalogue.contracts import (
    AccessMode,
    Action,
    BasicGate,
    Block,
    FieldRule,
    FlagOrphans,
    Impact,
    IntegrityViolation,
    LinkRepoint,
    NotFound,
    Orphan,
    OrphanDecision,
    OrphanPolicy,
    Ref,
    StaleWrite,
    allowed_values,
    get_field,
)

from .. import _crud

# person.tenet_system draws its controlled vocabulary (with choices enforcement) from the shared
# CategoricalField registry — the same source the work-side gate and the store guard read.
_TENET_CHOICES = allowed_values(get_field("person", "tenet_system"))

# Columns a create/update may set + their rules. primary_name is the identity anchor (with dates).
_GATE = BasicGate({"person": {
    "primary_name": FieldRule(required=True, max_len=300),
    "role_hint": FieldRule(max_len=100),
    "dates": FieldRule(max_len=100),
    "external_id": FieldRule(max_len=100),
    "verification_status": FieldRule(max_len=50),
    "notes": FieldRule(max_len=4000),
    "tradition": FieldRule(max_len=200),
    "tenet_system": FieldRule(max_len=200, choices=_TENET_CHOICES),
}})


class PersonWriter:
    RESOURCE = "person"

    def __init__(self, access, store):
        self._a = access
        self._s = store
        self._gate = _GATE

    # ── create / update (shared CRUD mechanics) ────────────────────────────────
    def plan_create(self, values: dict) -> Impact:
        """Plan creating a person: gate the payload (primary_name required). Un-appliable if malformed."""
        return _crud.plan_create(self._a, self.RESOURCE, self._gate, values)

    def create(self, values: dict, idempotency_key=None) -> Impact:
        """Create a person in one transaction; returns the Impact with the assigned id. Standalone
        (not Session-staged), WRITE-gated. `idempotency_key` dedups a retried create."""
        return _crud.create(self._a, self.RESOURCE, self._s, self._gate, values, idempotency_key)

    def plan_update(self, ref: Ref, values: dict) -> Impact:
        """Plan updating a person's columns: gate the payload + pin the identity fingerprint."""
        return _crud.plan_update(self._a, self.RESOURCE, self._gate, self._s, ref, values)

    # ── alias sub-entity COMMANDS (direct: authorized + audited, outside plan→apply) ──────
    # Aliases are a sub-entity edit, not a root lifecycle op, so the plan→apply Impact ceremony
    # would be overkill. These are simple authorized + audited + committed commands; each writes one
    # `audit_log` row tagged with the alias op. WRITE-gated like any mutation.
    def insert_person(self, primary_name: str, role_hint=None, dates=None,
                      suggested_external_id=None) -> int:
        """Insert a bare person row (the get_or_create mint), optionally parking a
        `suggested_external_id`; returns its id. Staged; caller commits."""
        self._a.authorize(Action(self.RESOURCE, "create", AccessMode.WRITE))
        return self._s.insert_person(primary_name, role_hint, dates, suggested_external_id)

    def bind_external(self, pid: int, ext_id, status: str = "verified") -> None:
        """Set person.external_id + verification_status (the authority bind). Staged."""
        self._a.authorize(Action(self.RESOURCE, "bind", AccessMode.WRITE))
        self._s.bind_external(pid, ext_id, status)

    def store_external_id(self, pid: int, scheme: str, value: str) -> None:
        """Upsert one harvested cross-link id (person_external_id). Staged."""
        self._a.authorize(Action(self.RESOURCE, "bind", AccessMode.WRITE))
        self._s.store_external_id(pid, scheme, value)

    def clear_external_ids(self, pid: int) -> None:
        """Drop every person_external_id row for a person (rebind). Staged."""
        self._a.authorize(Action(self.RESOURCE, "bind", AccessMode.WRITE))
        self._s.clear_external_ids(pid)

    def set_kind_if_unbound(self, pid: int, status: str) -> bool:
        """Toggle a person's verification_status (provisional ↔ organization) ONLY when it is unbound
        (no external_id); returns whether a row changed. Staged; the caller owns the commit."""
        self._a.authorize(Action(self.RESOURCE, "set_kind", AccessMode.WRITE))
        return self._s.set_kind_if_unbound(pid, status)

    def confirm_local(self, pid: int) -> bool:
        """Flag an UNBOUND person 'confirmed_local'; returns whether a row changed. Staged."""
        self._a.authorize(Action(self.RESOURCE, "confirm_local", AccessMode.WRITE))
        return self._s.confirm_local(pid)

    def add_alias(self, pid: int, text: str, scheme: str = "english", *, if_absent: bool = False) -> None:
        """Append an alias to a LIVE person. `if_absent` skips it when the fold already exists
        (the searchable-spelling seed), else always inserts (the explicit 'add alias' action)."""
        self._a.authorize(Action(self.RESOURCE, "add_alias", AccessMode.WRITE))
        if self._s.get(pid) is None:
            raise NotFound(f"person {pid} not found")
        try:
            if not (if_absent and self._s.has_alias_fold(pid, text)):
                self._s.insert_alias(pid, text, scheme)
                self._a._audit(Impact("add_alias", Ref("person", pid), changes={"alias": text}))
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise

    def remove_alias(self, pid: int, alias_id: int) -> None:
        """Delete one of the person's aliases (no-op if it isn't theirs)."""
        self._a.authorize(Action(self.RESOURCE, "remove_alias", AccessMode.WRITE))
        try:
            self._s.remove_alias(pid, alias_id)
            self._a._audit(Impact("remove_alias", Ref("person", pid), changes={"alias_id": alias_id}))
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise

    def set_primary(self, pid: int, alias_id: int) -> None:
        """Promote an existing alias to be the person's PRIMARY (display) name, keeping the old
        primary name as a searchable alias. `NotFound` if the person or alias is gone."""
        self._a.authorize(Action(self.RESOURCE, "set_primary", AccessMode.WRITE))
        new_name = self._s.alias_text(pid, alias_id)
        cur = self._s.get(pid)
        if new_name is None or cur is None:
            raise NotFound(f"person {pid} / alias {alias_id} not found")
        try:
            if cur.primary_name and not self._s.has_alias_fold(pid, cur.primary_name):
                self._s.insert_alias(pid, cur.primary_name, "english")   # keep old name searchable
            self._s.set_primary_name(pid, new_name)
            self._a._audit(Impact("set_primary", Ref("person", pid), changes={"primary_name": new_name}))
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise

    # ── plan (read) ──────────────────────────────────────────────────────────
    def plan_delete(self, ref: Ref, policy: "OrphanPolicy | None" = None) -> Impact:
        """Plan deleting a person: a tombstone (aliases/external-ids ride along) plus any Work left
        with no live author (per `policy`, FLAG by default). Un-appliable if the person is gone or
        already tombstoned, or if the policy REFUSEs an orphan."""
        self._a.authorize(Action(self.RESOURCE, "delete", AccessMode.READ))
        policy = policy or FlagOrphans()
        person = self._s.get(ref.id)
        if person is None:   # absent or already tombstoned
            return Impact("delete", ref, blocks=(Block("not_found", f"person {ref.id} not found"),))
        target = person.ref()

        # Semantic orphans: a live Work that would have no live author once this person tombstones.
        orphans: tuple[Orphan, ...] = ()
        blocks: tuple[Block, ...] = ()
        for wid in self._s.orphaned_work_ids(ref.id):
            wref = Ref("work", wid)
            reason = f"work {wid} would have no remaining live author"
            decision = policy.decide(wref, reason)
            orphans += (Orphan(wref, reason, decision),)
            if decision == OrphanDecision.REFUSE:
                blocks += (Block("orphan_refuse", reason),)

        return Impact("delete", target, orphans=orphans, blocks=blocks)

    # ── merge (fold loser into winner) ────────────────────────────────────────
    def plan_merge(self, loser: Ref, winner: Ref, *, allow_cross_authority: bool = False,
                   keep_name_alias: bool = True) -> Impact:
        """Plan folding `loser` into `winner`: the contributor edges that re-point (as `LinkRepoint`s),
        the aliases gained. No mutation. Blocks self-merge, a missing/dead endpoint, or — unless
        `allow_cross_authority` — two DIFFERENT bound authorities (unbind one first). `keep_name_alias`
        rides on the Impact for apply."""
        self._a.authorize(Action(self.RESOURCE, "merge", AccessMode.READ))
        if loser.id == winner.id:
            return Impact("merge", loser, blocks=(Block("invalid", "cannot merge a person into itself"),))
        lose, win = self._s.get(loser.id), self._s.get(winner.id)
        blocks: tuple[Block, ...] = ()
        if lose is None:
            blocks += (Block("not_found", f"person {loser.id} not found"),)
        if win is None:
            blocks += (Block("not_found", f"person {winner.id} (merge target) not found"),)
        if blocks:
            return Impact("merge", loser, blocks=blocks)
        if (lose.external_id and win.external_id and lose.external_id != win.external_id
                and not allow_cross_authority):
            return Impact("merge", lose.ref(), changes={"into": win.ref().to_dict()},
                          blocks=(Block("conflict", f"different authorities ({lose.external_id} vs "
                                        f"{win.external_id}) — unbind one before merging"),))
        l_ref, w_ref = lose.ref(), win.ref()
        repoints = tuple(LinkRepoint(edge, l_ref, w_ref)
                         for edge, n in self._s.merge_edge_counts(loser.id).items() if n)
        changes = {"into": w_ref.to_dict(),
                   "aliases_gained": self._s.merge_aliases_gained(loser.id, winner.id),
                   "keep_name_alias": keep_name_alias}
        return Impact("merge", l_ref, changes=changes, link_repoints=repoints)

    def merge(self, loser: Ref, winner: Ref, *, allow_cross_authority: bool = False,
              keep_name_alias: bool = True) -> Impact:
        """Plan + apply a person merge in one transaction. Returns the applied Impact."""
        return self.apply(self.plan_merge(
            loser, winner, allow_cross_authority=allow_cross_authority, keep_name_alias=keep_name_alias))

    def _winner_ref(self, impact: Impact) -> Ref:
        return Ref.from_dict(impact.changes["into"])

    def split(self, blob: Ref, targets: "list[dict]") -> None:
        """Dissolve a conflated person into already-resolved `targets` ([{id, role}]) — attach each
        to the blob's works/editions, then HARD-delete the blob (a split has no single survivor, so
        it's a direct command, not plan→apply; recovery is the caller's snapshot-undo). WRITE-gated +
        audited; stages onto the connection (the caller commits)."""
        self._a.authorize(Action(self.RESOURCE, "split", AccessMode.WRITE))
        try:
            self._s.split(blob.id, targets)
            self._a._audit(Impact("split", blob, changes={"into": [t["id"] for t in targets]}))
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise

    # ── stage / apply (write) ─────────────────────────────────────────────────
    def _stage(self, impact: Impact):
        """Authorize + recheck + stage the person delete/update/merge (no commit). Returns () — a
        person owns no files to trash; its parts ride the tombstone."""
        self._a.authorize(Action(self.RESOURCE, impact.op, AccessMode.WRITE))
        if impact.op not in ("delete", "update", "merge"):
            raise IntegrityViolation(f"unsupported person op: {impact.op!r}")
        if not impact.appliable:
            raise IntegrityViolation("; ".join(b.message for b in impact.blocks))
        self._a._audit(impact)
        if impact.op == "update":
            _crud.stage_update(self._s, self.RESOURCE, impact.target, impact.changes)
            return ()
        if impact.op == "merge":
            winner = self._winner_ref(impact)
            self._recheck(impact.target)          # loser still live + identity intact
            self._recheck(winner)                 # winner still live + identity intact
            self._s.merge(impact.target.id, winner.id,
                          keep_name_alias=impact.changes.get("keep_name_alias", True))
            return ()
        self._recheck(impact.target)
        # Root SOFT-deletes: tombstone the person (id frozen → review/cache refs stay safe).
        self._s.tombstone(impact.target.id)
        # GC'd orphan works tombstone too (the second-order effect; Work-closure delete lands later).
        for o in impact.orphans:
            if o.decision == OrphanDecision.GC:
                self._s.tombstone_work(o.ref.id)
        return ()

    def apply(self, impact: Impact) -> Impact:
        """Execute a planned person delete in one transaction (no filesystem effects)."""
        try:
            self._stage(impact)
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise
        return impact

    def restore(self, ref: Ref) -> None:
        """Un-delete a tombstoned person (clear `deleted_at`) — restores the row plus its aliases,
        external-ids and edge-links (none were purged). WRITE-gated. NOTE: a work this delete GC'd is
        NOT auto-restored (a separate tombstone); restore it explicitly if intended."""
        self._a.authorize(Action(self.RESOURCE, "restore", AccessMode.WRITE))
        try:
            self._s.restore(ref.id)
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise

    # ── internals ────────────────────────────────────────────────────────────
    def _recheck(self, target: Ref) -> None:
        """TOCTOU + recycled-id guard (`person_identity_ok` as a `Ref`: name-fold + dates) +
        optimistic-concurrency (`rev`): the person must still be live and unchanged since the plan —
        else `StaleWrite`/`NotFound`."""
        _crud.recheck(self._s, self.RESOURCE, target)
