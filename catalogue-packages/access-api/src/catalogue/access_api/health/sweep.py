"""`OrphanSweep` — reconcile every non-FK reference class the registry declares.

The non-FK registry is *declared, not discovered* (registry.py): every store that keeps an entity's
id/keys OUTSIDE a foreign key is listed there, so a delete-purge — and this sweep — can see it. The
per-delete purges keep things clean going forward; OrphanSweep is the catch-all backstop that
reconciles the WHOLE database against the live rows and reports (or, with `apply`, removes) what the
cascade can't reach:

  * **hash caches** — `raw_extract_cache` & friends whose `file_hash` names no holding (audit #5).
  * **review_queue** — pending items of a registered owned type whose owner row is gone.
  * **promotion** — id-array entries (work_ids / person_ids) whose entity is gone.
  * **cover art** — `e<id>` files whose edition is gone (the cover-cache id-reuse class, audit #3).

Existence, not soft-delete: a tombstoned root still has its row, so its id is frozen and NOT a reuse
hazard — only fully-absent ids are swept. `scan` is READ-gated and mutates nothing (the health
report); `apply` is WRITE-gated, runs the DB scrubs in one transaction and trashes the art files
after commit. Storage-agnostic via `SweepStore`; this layer is policy + orchestration. See
docs/access/entity_api_model.md §6.
"""
from __future__ import annotations

import os

from catalogue.contracts import AccessMode, Action, FileOp

from ..registry import REVIEW_ITEM_OWNERS, edition_id_from_art_name
from .store import SqliteSweepStore


class OrphanSweep:
    RESOURCE = "health"

    def __init__(self, access, store=None):
        self._a = access
        self._s = store or SqliteSweepStore(access)

    def purge_wrong_type_authority(self, person_id_prefixes) -> dict:
        """Data-repair: clear wrong-type person/work authority binds + scheme='other' alias pollution
        (the verify cleanup). Returns the 4 rowcounts. WRITE-gated; staged, the caller commits."""
        self._a.authorize(Action(self.RESOURCE, "purge_wrong_type_authority", AccessMode.WRITE))
        return self._s.purge_wrong_type_authority(person_id_prefixes)

    def owner_exists(self, kind: str, oid) -> bool:
        """Does a row with this id still EXIST for `kind` (a tombstone counts as present — only a
        fully-absent id is a reuse hazard)? The cross-entity existence check the legacy delete
        purges + the dangling-ref backfill share. READ-gated."""
        self._a.authorize(Action(self.RESOURCE, "owner_exists", AccessMode.READ))
        return self._s.owner_exists(kind, oid)

    # ── scan (read) ─────────────────────────────────────────────────────────────
    def scan(self) -> dict:
        """A read-only health report: the non-FK orphans across every registered class. Mutates
        nothing — feed it to `apply` to perform the cleanup."""
        self._a.authorize(Action(self.RESOURCE, "scan", AccessMode.READ))
        return {
            "hash_cache_orphans": self._s.orphan_hash_caches(),
            "review_item_orphans": self._scan_review_items(),
            "promotion_orphans": self._scan_promotion_arrays(),
            "cover_art_orphans": self._scan_cover_art(),
        }

    def dangling_ref_orphans(self) -> dict:
        """Just the two non-FK *reference-store* classes — pending review items + promotion id-arrays
        whose owner row is gone — without the hash-cache / cover-art classes `scan` also covers. The
        slice the `sweep_dangling_refs` backfill targets, so that CLI routes through the access-API
        instead of a raw connection. READ-gated; mutates nothing. Feed a report built from this to
        `apply` (with the other two classes empty) to scrub only these."""
        self._a.authorize(Action(self.RESOURCE, "scan", AccessMode.READ))
        return {
            "review_item_orphans": self._scan_review_items(),
            "promotion_orphans": self._scan_promotion_arrays(),
        }

    def _scan_review_items(self) -> list:
        review_items = []
        for rid, item_type, payload in self._s.pending_review_items():
            owner = REVIEW_ITEM_OWNERS.get(item_type)
            if owner is None:
                continue
            kind, key = owner
            oid = payload.get(key)
            if oid is None or self._s.owner_exists(kind, oid):
                continue
            review_items.append({"id": rid, "item_type": item_type, "owner": kind, "owner_id": oid})
        return review_items

    def _scan_promotion_arrays(self) -> list:
        promotion = []
        for rid, column, kind, ids in self._s.promotion_arrays():
            gone = [i for i in ids if not self._s.owner_exists(kind, i)]
            if gone:
                promotion.append({"review_item_id": rid, "column": column, "removed": gone})
        return promotion

    def cover_art_orphans(self) -> list:
        """Just the cover-art orphans (`e<id>` files whose edition is gone) — the covers-only slice of
        `scan`, READ-gated. Backs the cli cover sweep so it goes through the access-API, not raw SQL."""
        self._a.authorize(Action(self.RESOURCE, "scan", AccessMode.READ))
        return self._scan_cover_art()

    def _scan_cover_art(self) -> list:
        live = self._s.live_edition_ids()
        out = []
        for d in (self._a.cover_cache, self._a.cover_pinned):
            if not d or not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                eid = edition_id_from_art_name(name)
                if eid is not None and eid not in live:
                    out.append(os.path.join(d, name))
        return out

    # ── apply (write) ───────────────────────────────────────────────────────────
    def apply(self, report: dict) -> dict:
        """Perform the cleanup the report describes: DB scrubs in one transaction, art files trashed
        after commit (recoverable in `.trash/`, like the delete paths)."""
        self._a.authorize(Action(self.RESOURCE, "sweep", AccessMode.WRITE))
        try:
            for table, file_hash in report["hash_cache_orphans"]:
                self._s.purge_hash_cache(table, file_hash)
            for it in report["review_item_orphans"]:
                self._s.delete_review_item(it["id"])
            for pr in report["promotion_orphans"]:
                self._s.scrub_promotion_array(pr["review_item_id"], pr["column"], pr["removed"])
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise
        art = report["cover_art_orphans"]
        self._a.backing.run([FileOp("trash", p) for p in art], self._a.trash_dir)
        return {
            "hash_caches_purged": len(report["hash_cache_orphans"]),
            "review_items_dropped": len(report["review_item_orphans"]),
            "promotion_arrays_scrubbed": len(report["promotion_orphans"]),
            "cover_art_trashed": len(art),
        }
