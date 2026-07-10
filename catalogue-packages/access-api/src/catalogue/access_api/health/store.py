"""OrphanSweep persistence — the storage PORT + its SQLite adapter.

The sweep orchestration (sweep.py) holds no SQL: it programs against `SweepStore`, an abstract port
of the read/scrub operations needed to reconcile the non-FK reference classes the registry declares.
`SqliteSweepStore` is the implementation over the gateway's RO/RW connections (reads on RO; scrubs
on RW, no commit — the sweep's `apply` owns the transaction via `Access.commit`).
"""
from __future__ import annotations

import abc
import json

from ..registry import (
    HOLDING_FILE_HASH_CACHES,
    PROMOTION_ID_ARRAYS,
)

# Tables a registry-declared id can own; existence (a tombstone still has its row) is what the sweep
# checks — a fully-absent id is the id-reuse hazard, a soft-deleted one is frozen and safe.
_OWNER_TABLES = ("edition", "work", "person", "subject", "collection", "tradition", "holding")


class SweepStore(abc.ABC):
    """Port: the data operations the OrphanSweep needs (no policy/transaction logic)."""

    # ── scan (reads) ────────────────────────────────────────────────────────────
    @abc.abstractmethod
    def orphan_hash_caches(self) -> "list[tuple[str, str]]":
        """(table, file_hash) for every hash-cache row whose file_hash names no holding (audit #5)."""
    @abc.abstractmethod
    def pending_review_items(self) -> "list[tuple[int, str, dict]]":
        """(id, item_type, payload) for every PENDING review_queue row (bad JSON skipped)."""
    @abc.abstractmethod
    def promotion_arrays(self) -> "list[tuple[int, str, str, list]]":
        """(review_item_id, column, owner_kind, ids) per registered promotion id-array."""
    @abc.abstractmethod
    def owner_exists(self, kind: str, oid) -> bool:
        """Does a row with this id still exist for `kind`? (Existence — a tombstone counts.)"""
    @abc.abstractmethod
    def live_edition_ids(self) -> "set[int]":
        """Ids with a row in `edition` — what cover-art `e<id>` filenames must reconcile against."""

    # ── apply (staged scrubs, no commit) ─────────────────────────────────────────
    @abc.abstractmethod
    def purge_hash_cache(self, table: str, file_hash: str) -> None: ...
    @abc.abstractmethod
    def delete_review_item(self, rid: int) -> None: ...
    @abc.abstractmethod
    def scrub_promotion_array(self, rid: int, column: str, removed) -> None:
        """Re-read the array and remove `removed` ids (re-read so a concurrent edit isn't clobbered)."""
    @abc.abstractmethod
    def purge_wrong_type_authority(self, person_id_prefixes) -> dict:
        """Data-repair (verify cleanup): clear person.external_id rows whose id matches NONE of
        `person_id_prefixes` (back to provisional), clear work canonical ids of the form 'bdr:P%'
        (person ids wrongly stored as work canonicals), and drop scheme='other' alias pollution on
        any person/work that still has a non-'other' alias. Returns the 4 rowcounts. Staged."""


class SqliteSweepStore(SweepStore):
    """SQLite adapter over an `Access`'s RO/RW connections."""

    def __init__(self, access):
        self._a = access

    def orphan_hash_caches(self):
        out: list[tuple[str, str]] = []
        for table in HOLDING_FILE_HASH_CACHES:
            rows = self._a.ro.execute(
                f"SELECT DISTINCT file_hash FROM {table} "
                "WHERE file_hash IS NOT NULL AND file_hash NOT IN "
                "(SELECT file_hash FROM holding WHERE file_hash IS NOT NULL)").fetchall()
            out += [(table, fh) for (fh,) in rows]
        return out

    def pending_review_items(self):
        out = []
        for rid, item_type, raw in self._a.ro.execute(
                "SELECT id, item_type, payload_json FROM review_queue WHERE status = 'pending'"
        ).fetchall():
            try:
                out.append((rid, item_type, json.loads(raw) if raw else {}))
            except (ValueError, TypeError):
                continue
        return out

    def promotion_arrays(self):
        out = []
        for kind, column in PROMOTION_ID_ARRAYS.items():
            for rid, raw in self._a.ro.execute(
                    f"SELECT review_item_id, {column} FROM promotion").fetchall():
                try:
                    ids = json.loads(raw) if raw else []
                except (ValueError, TypeError):
                    continue
                out.append((rid, column, kind, ids))
        return out

    def owner_exists(self, kind, oid):
        if kind not in _OWNER_TABLES:
            return True                                   # unknown kind → never flag it
        return self._a.ro.execute(
            f"SELECT 1 FROM {kind} WHERE id = ?", (oid,)).fetchone() is not None

    def live_edition_ids(self):
        return {r[0] for r in self._a.ro.execute("SELECT id FROM edition").fetchall()}

    def purge_hash_cache(self, table, file_hash):
        if table not in HOLDING_FILE_HASH_CACHES:         # whitelist — no injected table names
            raise ValueError(f"refusing to purge unregistered cache table {table!r}")
        self._a.rw.execute(f"DELETE FROM {table} WHERE file_hash = ?", (file_hash,))

    def purge_wrong_type_authority(self, person_id_prefixes):
        rw = self._a.rw
        prefixes = list(person_id_prefixes)
        # person ids matching none of the valid prefixes → clear, back to provisional.
        bad = " AND ".join("external_id NOT LIKE ?" for _ in prefixes) or "1=1"
        out = {"person_wrongtype": rw.execute(
            "UPDATE person SET external_id = NULL, verification_status = 'provisional' "
            f"WHERE external_id IS NOT NULL AND {bad}",
            tuple(f"{p}%" for p in prefixes)).rowcount}
        out["work_wrongtype"] = rw.execute(
            "UPDATE work SET canonical_system = NULL, canonical_number = NULL "
            "WHERE canonical_number LIKE 'bdr:P%'").rowcount
        out["person_aliases"] = rw.execute(
            "DELETE FROM person_alias WHERE scheme = 'other' AND person_id IN "
            "(SELECT person_id FROM person_alias WHERE scheme <> 'other')").rowcount
        out["work_aliases"] = rw.execute(
            "DELETE FROM work_alias WHERE scheme = 'other' AND work_id IN "
            "(SELECT work_id FROM work_alias WHERE scheme <> 'other')").rowcount
        return out

    def delete_review_item(self, rid):
        self._a.rw.execute("DELETE FROM review_queue WHERE id = ?", (rid,))

    def scrub_promotion_array(self, rid, column, removed):
        if column not in PROMOTION_ID_ARRAYS.values():    # whitelist — trusted literal only
            raise ValueError(f"refusing to scrub unregistered promotion column {column!r}")
        row = self._a.rw.execute(
            f"SELECT {column} FROM promotion WHERE review_item_id = ?", (rid,)).fetchone()
        if not row:
            return
        try:
            ids = json.loads(row[0]) if row[0] else []
        except (ValueError, TypeError):
            return
        gone = set(removed)
        self._a.rw.execute(
            f"UPDATE promotion SET {column} = ? WHERE review_item_id = ?",
            (json.dumps([i for i in ids if i not in gone]), rid))
