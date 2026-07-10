"""The access-API gateway — the bound entry point every client uses.

`bind(principal, policy, db_path)` returns an `Access` carrying the caller's identity, the
authorization policy, and lazily-opened **read-only** / **read-write** connections. Reads run
over the RO connection (so a read path cannot write even with a bug); writes over RW. Every
entity repository calls `Access.authorize(action)` before touching the DB, so authorization is
enforced at one place and the access modules carry no auth logic.

See docs/access/entity_api_model.md §5/§9.
"""
from __future__ import annotations

from pathlib import Path

from catalogue.contracts import SYSTEM, Action, AllowAll, Policy, Principal
from catalogue.db_store import connect_ro, connect_rw

from .backing import Backing, LocalBacking
from .caches import ClassificationCacheRepo, ParsedTocCacheRepo, ResolverCacheRepo
from .capture import CaptureRepo
from .collections import CollectionRepo
from .editions import EditionRepo
from .gloss_cache import GlossCacheRepo
from .health import OrphanSweep
from .holdings import HoldingRepo
from .ingest_ignore import IngestIgnoreRepo
from .journal import JournalRepos
from .persons import PersonRepo
from .reading_position import ReadingPositionRepo
from .review_queue import ReviewQueueRepo
from .section_cache import SectionCacheRepo
from .session import Session
from .starred import StarredRepo
from .sweep_state import SweepStateRepo
from .subjects import SubjectRepo
from .traditions import TraditionRepo
from .tradition_classify import TraditionClassifyRepo
from .vocab import VocabRepo
from .wishlist import WishlistRepo
from .works import WorkRepo


class Access:
    """A principal + policy bound to a DB, exposing the per-entity repositories. Holds at most
    one RO and one RW connection, opened on first use. Close it (or use it as a context manager)."""

    def __init__(self, principal: Principal, policy: Policy, db_path,
                 backing: "Backing | None" = None, *, conn=None):
        self.principal = principal
        self.policy = policy
        self._db_path = db_path
        # `conn` (bind_conn mode): an EXISTING caller-owned connection used for BOTH ro and rw, so the
        # engine STAGES onto the caller's transaction; commit/rollback become the caller's (no-ops
        # here). None = standalone mode (lazy-open our own RO/RW). See bind_conn().
        self._shared = conn
        self._ro = conn
        self._rw = conn
        self._scan_ocr = None
        # A holding's file is one pluggable backing — the local filesystem by default; inject another
        # adapter (object store, test fake) without changing the access layer. See backing.py.
        self.backing = backing or LocalBacking()
        # File-location config is derived from the DB path (siblings of the DB). In bind_conn mode the
        # caller has no db_path here; the few file-touching ops (edition/holding delete) aren't the
        # bind_conn use cases, and a caller that needs them sets these explicitly.
        parent = Path(db_path).parent if db_path is not None else None
        self.trash_dir = (parent / ".trash") if parent is not None else None
        self.cover_cache = str(parent / ".cover-cache") if parent is not None else None
        self.cover_pinned = str(parent / "covers-pinned") if parent is not None else None
        # Per-entity repositories, each with separate .reads / .writes surfaces.
        self.holdings = HoldingRepo(self)
        self.editions = EditionRepo(self)
        self.persons = PersonRepo(self)
        self.works = WorkRepo(self)
        self.subjects = SubjectRepo(self)
        self.collections = CollectionRepo(self)
        self.traditions = TraditionRepo(self)
        # The tradition-classifier's bespoke signal reads + the rule→llm work_tradition write
        # (Phase-2 work classifier + the person-lineage roster) — keeps those services SQL-free.
        self.tradition_classify = TraditionClassifyRepo(self)
        # Cross-entity maintenance: reconcile the non-FK registry against live rows (scan/apply).
        self.health = OrphanSweep(self)
        # The operator work list (title proposals, dedup/verify candidates, ingest promotions). A flat
        # policy-gated repo (not a soft-delete aggregate); writes stage, the caller owns the commit.
        self.review = ReviewQueueRepo(self)
        # The sweep-resume cache (path/size/mtime/hash). A flat repo over a non-entity cache table.
        self.sweep_state = SweepStateRepo(self)
        # The ingest-ignore list ("never surface this file again", keyed by path + hash).
        self.ingest_ignore = IngestIgnoreRepo(self)
        # The §14 phone/PWA capture inbox (capture_staging: scanned ISBNs awaiting resolution).
        self.capture = CaptureRepo(self)
        # The wishlist (wishlist_item: books wanted but not yet owned). A soft-deletable root with a
        # `rev` counter, but a flat repo (not the full plan/apply engine) — writes stage, route commits.
        self.wishlist = WishlistRepo(self)
        # Starred (favourited) editions (starred_edition: the Starred rail + highlighted covers). A
        # flat toggle repo — star is idempotent, unstar hard-deletes; writes stage, the route commits.
        self.starred = StarredRepo(self)
        # Controlled-vocabulary code lists (work_type / alias_scheme dropdowns).
        self.vocab = VocabRepo(self)
        # Per-copy in-app reader position (reading_position: locator + fraction per holding).
        self.reading_position = ReadingPositionRepo(self)
        # The title-gloss memoization cache (LLM gloss of a native-script title).
        self.gloss_cache = GlossCacheRepo(self)
        # The section-analysis cache (self-bootstrapping; keyed by file_hash + section_version).
        self.section_cache = SectionCacheRepo(self)
        # The classify-ladder + parsed-TOC memoization caches (derived data, not entities).
        self.classification_cache = ClassificationCacheRepo(self)
        self.parsed_toc_cache = ParsedTocCacheRepo(self)
        self.resolver_cache = ResolverCacheRepo(self)
        # The reversible-undo facility: generic verbatim row snapshot/restore (`acc.journal`) + the
        # undo_log entry table (`acc.undo_log`). A trusted low-level mechanism, below the entity model.
        _journal = JournalRepos(self)
        self.journal = _journal.rows
        self.undo_log = _journal.log

    @property
    def ro(self):
        """The read-only connection (reads/plans) — lazily opened, or the shared conn in bind_conn mode."""
        if self._ro is None:
            self._ro = connect_ro(self._db_path)
        return self._ro

    @property
    def rw(self):
        """The read-write connection (applies) — lazily opened, or the shared conn in bind_conn mode."""
        if self._rw is None:
            self._rw = connect_rw(self._db_path)
        return self._rw

    @property
    def scan_ocr(self):
        """The scan/OCR provenance repo (`.reads` / `.writes`). Lazily constructed so importing the
        gateway doesn't pull the digitization concern — it's a bounded slice clients opt into."""
        if self._scan_ocr is None:
            from .scan_ocr_access import ScanOcrRepo
            self._scan_ocr = ScanOcrRepo(self)
        return self._scan_ocr

    def authorize(self, action: Action) -> None:
        """Raise `Denied` if the policy refuses this action for this principal."""
        self.policy.check(self.principal, action)

    # ── idempotency (create dedup) ────────────────────────────────────────────
    def _idempotent_lookup(self, key: str):
        """The (entity_kind, entity_id) a prior create recorded under `key`, or None. Read on the RW
        connection (the create's transaction-consistent view)."""
        row = self.rw.execute(
            "SELECT entity_kind, entity_id FROM idempotency_key WHERE key = ?", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def _idempotent_record(self, key: str, entity_kind: str, entity_id: int) -> None:
        """Record that `key` produced this entity (same transaction as the create). OR IGNORE so a
        race keeps the first writer's row."""
        self.rw.execute(
            "INSERT OR IGNORE INTO idempotency_key (key, entity_kind, entity_id) VALUES (?, ?, ?)",
            (key, entity_kind, entity_id))

    # ── audit log (who-changed-what) ──────────────────────────────────────────
    def _audit(self, impact) -> None:
        """Record an applied write in `audit_log` on the RW connection — SAME transaction as the
        mutation, so it commits/rolls back with it. Logs principal + op + target root + a compact
        detail (changed columns / cascade count). A no-op for non-mutating/empty impacts."""
        if impact is None or impact.op in ("session", "read"):
            return
        import json
        detail: dict = {}
        if impact.changes:
            detail["changes"] = sorted(impact.changes)
        if impact.cascades:
            detail["cascades"] = len(impact.cascades)
        if impact.orphans:
            detail["orphans"] = len(impact.orphans)
        self.rw.execute(
            "INSERT INTO audit_log (principal, op, entity_kind, entity_id, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.principal.id, impact.op, impact.target.kind, impact.target.id,
             json.dumps(detail) if detail else None))

    # ── pre-destructive checkpoint (reversible hard-deletes) ──────────────────
    def _checkpoint(self, impact, snapshot: dict) -> None:
        """Snapshot the rows a destructive op is about to HARD-remove into the checkpoint table —
        SAME transaction, so it commits/rolls back with the delete. `snapshot` is {table: [rows]}."""
        import json
        self.rw.execute(
            "INSERT INTO checkpoint (principal, op, entity_kind, entity_id, snapshot) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.principal.id, impact.op, impact.target.kind, impact.target.id,
             json.dumps(snapshot)))

    def _latest_checkpoint(self, entity_kind: str, entity_id: int) -> "dict | None":
        """The most recent checkpoint snapshot for an entity (the one a `restore` undoes), or None.
        Read on the RW connection — the restore's transaction-consistent view."""
        import json
        row = self.rw.execute(
            "SELECT snapshot FROM checkpoint WHERE entity_kind = ? AND entity_id = ? "
            "ORDER BY id DESC LIMIT 1", (entity_kind, entity_id)).fetchone()
        return json.loads(row[0]) if row else None

    def audit_trail(self, entity_kind: str = None, entity_id: int = None, limit: int = 100):
        """The audit log, newest first, optionally scoped to one entity. Each row is a dict
        {id, ts, principal, op, entity_kind, entity_id, detail}. Read over the RO connection."""
        where, args = [], []
        if entity_kind is not None:
            where.append("entity_kind = ?"); args.append(entity_kind)
        if entity_id is not None:
            where.append("entity_id = ?"); args.append(entity_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.ro.execute(
            "SELECT id, ts, principal, op, entity_kind, entity_id, detail "
            f"FROM audit_log{clause} ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        cols = ("id", "ts", "principal", "op", "entity_kind", "entity_id", "detail")
        return [dict(zip(cols, r)) for r in rows]

    def session(self) -> Session:
        """A unit of work: stage several writes, commit them atomically (one transaction)."""
        return Session(self)

    def commit(self) -> None:
        """Commit the current write transaction. The access layer's unit-of-work primitive — a
        store adapter executes mutations on `rw` without committing; the writer/session calls this.
        In bind_conn mode this is a NO-OP: the caller owns the shared connection's transaction, so an
        engine `apply` stages onto it and the caller decides when to commit."""
        if self._shared is None and self._rw is not None:
            self._rw.commit()

    def rollback(self) -> None:
        """Roll the current write transaction back (mutations staged since the last commit). NO-OP in
        bind_conn mode — the caller owns rollback of its shared transaction."""
        if self._shared is None and self._rw is not None:
            self._rw.rollback()

    def close(self) -> None:
        if self._shared is not None:        # bind_conn: the caller owns the connection, don't close it
            return
        for conn in (self._ro, self._rw):
            if conn is not None:
                conn.close()
        self._ro = self._rw = None

    def __enter__(self) -> "Access":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def bind(principal: Principal, policy: Policy, db_path, backing: "Backing | None" = None) -> Access:
    """The single entry point: bind a principal + policy to a DB (and optionally a `Backing` for
    holding files). Caller closes it (or `with`)."""
    return Access(principal, policy, db_path, backing)


def system_access(db_path) -> Access:
    """Internal full-access binding for services / cli / populate (SYSTEM principal, AllowAll)."""
    return bind(SYSTEM, AllowAll(), db_path)


def bind_conn(principal: Principal, policy: Policy, conn, backing: "Backing | None" = None) -> Access:
    """In-process composition: an `Access` over an EXISTING caller-owned connection. The engine STAGES
    its mutations onto `conn` (used for both reads and writes); `acc.commit()`/`acc.rollback()` are
    no-ops, so the CALLER owns the transaction — making `caller-snapshot → acc.<write> → caller commit`
    atomic. Use this when a service/route already holds a `db` and a transaction; use `bind`/
    `system_access` for standalone clients that should own their own RO/RW connections. NOTE: ro and rw
    are the SAME (caller's) connection, so the read-only write-guard doesn't apply here — the caller
    already has write access. Don't close it (the caller owns the connection's lifecycle)."""
    return Access(principal, policy, None, backing, conn=conn)


def system_conn(conn) -> Access:
    """`bind_conn` with the SYSTEM principal + AllowAll — for internal services routing a mutation
    through the engine inside their own transaction."""
    return bind_conn(SYSTEM, AllowAll(), conn)
