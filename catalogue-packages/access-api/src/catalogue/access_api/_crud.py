"""Shared create / update / query mechanics for the aggregate readers & writers.

The leaf engine (`_leaf.py`) bakes these in for the structurally-identical leaf roots. The richer
aggregates (Edition / Person / Work) keep their own reads/writes — cascades, merge, semantic orphans
— but reuse the SAME create/update/query machinery here so the gate + plan→apply + fingerprint-recheck
+ Query shape is ONE implementation, not four. Each entity supplies its `RESOURCE`, an `IntegrityGate`
and a `Store` exposing `get`/`current`/`create`/`update`/`list_page`/`count`.

Convention (matches the leaf engine):
  * `create` is standalone (no prior id to recheck) — it commits and returns the Impact carrying the
    assigned id; it is NOT staged into a `Session`.
  * `update` is plan→stage→apply like delete: `plan_update` gates the payload + pins the target's
    identity fingerprint; `stage_update` rechecks (TOCTOU/recycled-id → StaleWrite/NotFound) and
    writes, with no commit, so a writer's `_stage` can call it and a `Session` can batch it.
"""
from __future__ import annotations

from catalogue.contracts import (
    AccessMode,
    Action,
    Block,
    Impact,
    IntegrityViolation,
    NotFound,
    Query,
    Ref,
    StaleWrite,
)


# ── create ───────────────────────────────────────────────────────────────────────
def plan_create(access, resource: str, gate, values: dict) -> Impact:
    """Gate (normalize + validate) a create payload → an Impact whose `changes` is the normalized
    row. Target id is 0 (assigned at `create`). READ-gated."""
    access.authorize(Action(resource, "create", AccessMode.READ))
    norm, blocks = gate.check(resource, values)
    return Impact("create", Ref(resource, 0), changes=norm, blocks=blocks)


def create(access, resource: str, store, gate, values: dict, idempotency_key=None) -> Impact:
    """Create a row from a validated payload in one transaction. Returns the Impact with the assigned
    id in its target. WRITE-gated; a malformed payload raises `IntegrityViolation`.

    `idempotency_key` makes a retried create safe: if the key was already used, the recorded entity is
    returned (no second row, op tagged 'create_idempotent'); otherwise the key is recorded in the same
    transaction as the insert, so a duplicate retry can never create a twin."""
    if idempotency_key is not None:
        hit = access._idempotent_lookup(idempotency_key)
        if hit is not None:
            return Impact("create_idempotent", Ref(hit[0], hit[1]), changes=dict(values))
    impact = plan_create(access, resource, gate, values)
    access.authorize(Action(resource, "create", AccessMode.WRITE))
    if not impact.appliable:
        raise IntegrityViolation("; ".join(b.message for b in impact.blocks))
    try:
        new_id = store.create(impact.changes)
        if idempotency_key is not None:
            access._idempotent_record(idempotency_key, resource, new_id)
        created = Impact("create", Ref(resource, new_id), changes=impact.changes)
        access._audit(created)
        access.commit()
    except Exception:
        access.rollback()
        raise
    return created


# ── update ───────────────────────────────────────────────────────────────────────
def plan_update(access, resource: str, gate, store, ref: Ref, values: dict) -> Impact:
    """Gate the payload + pin the target's identity fingerprint. Un-appliable if the entity is gone
    or the payload is malformed. READ-gated (it's a plan)."""
    access.authorize(Action(resource, "update", AccessMode.READ))
    dto = store.get(ref.id)
    if dto is None:
        return Impact("update", ref, blocks=(Block("not_found", f"{resource} {ref.id} not found"),))
    norm, blocks = gate.check(resource, values, partial=True)
    return Impact("update", dto.ref(), changes=norm, blocks=blocks)


def stage_update(store, resource: str, target: Ref, changes: dict) -> None:
    """Recheck the target then write the gated payload — NO commit, so a writer's `_stage`/`Session`
    owns the transaction. The recheck is the TOCTOU/recycled-id guard (identity fingerprint) PLUS the
    optimistic-concurrency `rev` guard — a concurrent update advances `rev`, so a plan built against
    the old `rev` loses (StaleWrite) instead of clobbering the intervening write."""
    recheck(store, resource, target)
    store.update(target.id, changes)


def recheck(store, resource: str, target: Ref) -> None:
    """Identity-fingerprint + optimistic-`rev` guard for a planned write. Each is opt-in by presence
    on the `target` Ref (so a plan that didn't capture one skips that check)."""
    dto = store.current(target.id)
    if dto is None:
        raise NotFound(f"{resource} {target.id} no longer exists")
    cur = dto.ref()
    if target.fingerprint is not None and cur.fingerprint != target.fingerprint:
        raise StaleWrite(f"{resource} {target.id} changed since plan (fingerprint mismatch)")
    if target.rev is not None and cur.rev != target.rev:
        raise StaleWrite(
            f"{resource} {target.id} changed since plan (rev {target.rev} → {cur.rev})")


# ── query (read) ───────────────────────────────────────────────────────────────────
def list_page(access, resource: str, store, query: "Query | None") -> list:
    """One page of LIVE entities, filtered by `query.contains` + paginated. READ-gated."""
    access.authorize(Action(resource, "list", AccessMode.READ))
    q = query or Query()
    return store.list_page(q.contains, q.limit, q.offset)


def count(access, resource: str, store, query: "Query | None") -> int:
    """Total LIVE entities matching `query.contains` (the pagination total). READ-gated."""
    access.authorize(Action(resource, "count", AccessMode.READ))
    q = query or Query()
    return store.count(q.contains)
