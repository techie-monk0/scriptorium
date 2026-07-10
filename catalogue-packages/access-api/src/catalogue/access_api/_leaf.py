"""Generic leaf-entity engine — for roots with no owned parts and only a work edge.

Subject, Collection and Tradition are structurally identical: a named row linked to works through
one join table, soft-deletable, with a trivial delete (the FK drops the link rows; no non-FK
closure, no file, no semantic orphan). So one generic Reader/Writer + one generic storage **port**
(`LeafStore`) serve all three; each entity module declares a `LeafSpec` + DTO and the SQLite adapter
fills in the SQL. This is the generic-engine + per-entity-declaration split (entity_api_model.md §2),
AND the access-layer/implementation split: the Reader/Writer hold policy + plan→apply and never touch
SQL; `SqliteLeafStore` is the swappable implementation. The surface matches Holding/Edition
(`get`/`plan_delete`/`apply`/`_stage`/`restore`), so a `Session` can stage any of them uniformly.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Callable

from catalogue.contracts import (
    AccessMode,
    Action,
    BasicGate,
    Block,
    FieldRule,
    Impact,
    IntegrityGate,
    IntegrityViolation,
    NotFound,
    Query,
    Ref,
    StaleWrite,
)

from . import _crud


@dataclass(frozen=True)
class LeafSpec:
    """Per-entity declaration the SQLite adapter executes against."""
    resource: str                          # "subject"
    table: str                             # "subject"
    columns: tuple[str, ...]               # ("id", "name", "kind") — positional for make_dto
    make_dto: Callable                     # row -> DTO (with a .ref() carrying the fingerprint)
    work_link: "tuple[str, str] | None"    # (join_table, fk_col), e.g. ("work_subject", "subject_id")
    soft_delete: bool = True               # roots tombstone (deleted_at); reads hide tombstones
    writable: tuple[str, ...] = ()         # columns a create/update may set (id excluded)
    display_col: str = "name"              # the column a Query `contains` filter searches
    gate: "IntegrityGate | None" = None    # validate/normalize a write payload; default below


# ── storage port ────────────────────────────────────────────────────────────────
class LeafStore(abc.ABC):
    """Port: the data operations a leaf entity's access layer needs. `resource`/`soft_delete`/
    `writable`/`gate` are the declaration the Reader/Writer read; the rest are pure data ops with no
    policy/transaction."""
    resource: str
    soft_delete: bool
    writable: tuple[str, ...]
    gate: IntegrityGate

    @abc.abstractmethod
    def get(self, entity_id: int): ...
    @abc.abstractmethod
    def list_by_work(self, work_id: int) -> list: ...
    @abc.abstractmethod
    def list_page(self, contains, limit: int, offset: int) -> list:
        """Live entities matching the optional `contains` filter, id-ordered, one page."""
    @abc.abstractmethod
    def count(self, contains) -> int:
        """Total live entities matching `contains` (for pagination)."""
    @abc.abstractmethod
    def current(self, entity_id: int):
        """The entity as the write transaction sees it (tombstoned or not), or None — the recheck."""
    @abc.abstractmethod
    def create(self, values: dict) -> int:
        """Insert a row from a validated payload; return the new id."""
    @abc.abstractmethod
    def update(self, entity_id: int, values: dict) -> None:
        """Apply a validated field payload to an existing row."""
    @abc.abstractmethod
    def delete(self, entity_id: int) -> None:
        """Tombstone (soft_delete) or remove (hard) — the adapter encodes which, per its spec."""
    @abc.abstractmethod
    def restore(self, entity_id: int) -> None: ...


def _live(spec: LeafSpec, alias: str = "") -> str:
    """`AND <alias>deleted_at IS NULL` for a soft-deletable entity, else empty."""
    return f" AND {alias}deleted_at IS NULL" if spec.soft_delete else ""


class SqliteLeafStore(LeafStore):
    """SQLite adapter over an `Access`'s RO/RW connections, driven by a `LeafSpec`."""

    def __init__(self, access, spec: LeafSpec):
        self._a = access
        self._s = spec
        self.resource = spec.resource
        self.soft_delete = spec.soft_delete
        self.writable = spec.writable
        # Default gate: enforce the writable whitelist (unknown field → block), nothing required.
        # An entity declares its own gate for required/length rules.
        self.gate = spec.gate or BasicGate(
            {spec.resource: {f: FieldRule() for f in spec.writable}})

    def get(self, entity_id):
        cols = ", ".join(self._s.columns)
        row = self._a.ro.execute(
            f"SELECT {cols} FROM {self._s.table} WHERE id = ?{_live(self._s)}",
            (entity_id,)).fetchone()
        return self._s.make_dto(row) if row else None

    def list_by_work(self, work_id):
        if self._s.work_link is None:
            raise NotImplementedError(f"{self._s.resource} has no work edge")
        join, fk = self._s.work_link
        cols = ", ".join(f"t.{c}" for c in self._s.columns)
        return [self._s.make_dto(r) for r in self._a.ro.execute(
            f"SELECT {cols} FROM {self._s.table} t JOIN {join} j ON j.{fk} = t.id "
            f"WHERE j.work_id = ?{_live(self._s, 't.')} ORDER BY t.id", (work_id,)).fetchall()]

    def _filter(self, contains):
        """(where-clause, args) restricting to live rows + the optional display-col substring."""
        clauses, args = [], []
        if self._s.soft_delete:
            clauses.append("deleted_at IS NULL")
        if contains:
            clauses.append(f"{self._s.display_col} LIKE ?")
            args.append(f"%{contains}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, args

    def list_page(self, contains, limit, offset):
        cols = ", ".join(self._s.columns)
        where, args = self._filter(contains)
        return [self._s.make_dto(r) for r in self._a.ro.execute(
            f"SELECT {cols} FROM {self._s.table}{where} ORDER BY id LIMIT ? OFFSET ?",
            (*args, limit, offset)).fetchall()]

    def count(self, contains):
        where, args = self._filter(contains)
        return self._a.ro.execute(
            f"SELECT count(*) FROM {self._s.table}{where}", args).fetchone()[0]

    def current(self, entity_id):
        cols = ", ".join(self._s.columns)
        row = self._a.rw.execute(
            f"SELECT {cols} FROM {self._s.table} WHERE id = ?", (entity_id,)).fetchone()
        return self._s.make_dto(row) if row else None

    def create(self, values):
        # Keys come from a gate-validated payload (∈ the declared writable set), never raw input.
        cols = list(values)
        cur = self._a.rw.execute(
            f"INSERT INTO {self._s.table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(values[c] for c in cols))
        return cur.lastrowid

    def update(self, entity_id, values):
        if not values:
            return
        set_clause = ", ".join(f"{c} = ?" for c in values)
        self._a.rw.execute(
            f"UPDATE {self._s.table} SET {set_clause}, rev = rev + 1 WHERE id = ?",
            (*values.values(), entity_id))

    def delete(self, entity_id):
        if self._s.soft_delete:
            self._a.rw.execute(
                f"UPDATE {self._s.table} SET deleted_at = datetime('now') WHERE id = ?", (entity_id,))
        else:
            self._a.rw.execute(f"DELETE FROM {self._s.table} WHERE id = ?", (entity_id,))

    def restore(self, entity_id):
        self._a.rw.execute(
            f"UPDATE {self._s.table} SET deleted_at = NULL WHERE id = ?", (entity_id,))


# ── access layer (storage-agnostic) ──────────────────────────────────────────────
class LeafReader:
    def __init__(self, access, store: LeafStore):
        self._a = access
        self._s = store

    def _read(self, verb: str) -> None:
        self._a.authorize(Action(self._s.resource, verb, AccessMode.READ))

    def get(self, entity_id: int):
        """One **live** entity by id, or None (a soft-deleted tombstone reads as absent)."""
        self._read("get")
        return self._s.get(entity_id)

    def by_work(self, work_id: int) -> list:
        """Every **live** entity linked to `work_id` through this entity's join table, in id order."""
        self._read("by_work")
        return self._s.list_by_work(work_id)

    def list(self, query: "Query | None" = None) -> list:
        """One page of **live** entities (id-ordered), filtered by `query.contains` (substring on the
        display column) and paginated by `query.limit`/`offset`. Defaults to the first 50."""
        self._read("list")
        q = query or Query()
        return self._s.list_page(q.contains, q.limit, q.offset)

    def count(self, query: "Query | None" = None) -> int:
        """Total **live** entities matching `query.contains` — the pagination total for `list`."""
        self._read("count")
        q = query or Query()
        return self._s.count(q.contains)


class LeafWriter:
    def __init__(self, access, store: LeafStore):
        self._a = access
        self._s = store

    def plan_delete(self, ref: Ref) -> Impact:
        """Plan deleting a leaf entity: a bare delete (no non-FK closure to enumerate). Un-appliable
        if the entity is gone (or already tombstoned, which reads as absent)."""
        self._a.authorize(Action(self._s.resource, "delete", AccessMode.READ))
        dto = self._s.get(ref.id)
        if dto is None:
            return Impact("delete", ref,
                          blocks=(Block("not_found", f"{self._s.resource} {ref.id} not found"),))
        return Impact("delete", dto.ref())

    def plan_create(self, values: dict) -> Impact:
        """Plan creating a leaf entity: run the IntegrityGate (normalize + validate) and carry the
        result as `changes`. Un-appliable (blocks) on a malformed payload. Target id is 0 — the real
        id is assigned at `create`."""
        self._a.authorize(Action(self._s.resource, "create", AccessMode.READ))
        norm, blocks = self._s.gate.check(self._s.resource, values)
        return Impact("create", Ref(self._s.resource, 0), changes=norm, blocks=blocks)

    def plan_update(self, ref: Ref, values: dict) -> Impact:
        """Plan updating a leaf entity: gate the payload + pin the target's identity fingerprint.
        Un-appliable if the entity is gone or the payload is malformed."""
        self._a.authorize(Action(self._s.resource, "update", AccessMode.READ))
        dto = self._s.get(ref.id)
        if dto is None:
            return Impact("update", ref,
                          blocks=(Block("not_found", f"{self._s.resource} {ref.id} not found"),))
        norm, blocks = self._s.gate.check(self._s.resource, values, partial=True)
        return Impact("update", dto.ref(), changes=norm, blocks=blocks)

    def create(self, values: dict, idempotency_key=None) -> Impact:
        """Create a leaf entity from a validated payload (one transaction). Returns the Impact with
        the assigned id in its target. WRITE-gated; a malformed payload raises `IntegrityViolation`.
        Create is standalone (no prior id to recheck), so it is not staged through a `Session`.
        `idempotency_key` dedups a retried create. Delegates to the shared `_crud` mechanics."""
        return _crud.create(self._a, self._s.resource, self._s, self._s.gate, values, idempotency_key)

    def _stage(self, impact: Impact):
        """Authorize + recheck + mutate (no commit). Returns () — no file ops. `delete` tombstones
        (soft_delete; recoverable via `restore`) or hard-deletes; `update` writes the gated payload.
        Both recheck the target's fingerprint (TOCTOU/recycled-id guard)."""
        self._a.authorize(Action(self._s.resource, impact.op, AccessMode.WRITE))
        if impact.op not in ("delete", "update"):
            raise IntegrityViolation(f"unsupported {self._s.resource} op: {impact.op!r}")
        if not impact.appliable:
            raise IntegrityViolation("; ".join(b.message for b in impact.blocks))
        self._recheck(impact.target)
        self._a._audit(impact)
        if impact.op == "delete":
            self._s.delete(impact.target.id)
        else:
            self._s.update(impact.target.id, impact.changes)
        return ()

    def apply(self, impact: Impact) -> Impact:
        try:
            self._stage(impact)
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise
        return impact

    def restore(self, ref: Ref) -> None:
        """Un-delete a tombstoned entity (clear `deleted_at`) — the flag-flip undo soft-delete
        buys. A no-op if the row is live or absent. WRITE-gated."""
        if not self._s.soft_delete:
            raise IntegrityViolation(f"{self._s.resource} is not soft-deletable")
        self._a.authorize(Action(self._s.resource, "restore", AccessMode.WRITE))
        try:
            self._s.restore(ref.id)
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise

    def _recheck(self, target: Ref) -> None:
        """TOCTOU + recycled-id guard (identity fingerprint) + optimistic-concurrency (`rev`): the row
        must still exist and be unchanged since the plan — else `StaleWrite`/`NotFound`. Each guard is
        opt-in by presence on the target Ref."""
        dto = self._s.current(target.id)
        if dto is None:
            raise NotFound(f"{self._s.resource} {target.id} no longer exists")
        cur = dto.ref()
        if target.fingerprint is not None and cur.fingerprint != target.fingerprint:
            raise StaleWrite(f"{self._s.resource} {target.id} changed since plan (fingerprint mismatch)")
        if target.rev is not None and cur.rev != target.rev:
            raise StaleWrite(
                f"{self._s.resource} {target.id} changed since plan (rev {target.rev} → {cur.rev})")


class LeafRepo:
    """A leaf entity's bound surface: `.reads` and `.writes`, built from one `LeafSpec` (SQLite
    store by default; inject another adapter for a fake/HTTP backing)."""

    def __init__(self, access, spec: LeafSpec, store: "LeafStore | None" = None):
        store = store or SqliteLeafStore(access, spec)
        self.reads = LeafReader(access, store)
        self.writes = LeafWriter(access, store)
