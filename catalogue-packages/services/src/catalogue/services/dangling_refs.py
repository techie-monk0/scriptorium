"""Purge the non-FK references a cascade can't reach when an entity is deleted.

The schema's foreign keys (run with `PRAGMA foreign_keys=ON`, so cascades fire) clean
every properly-linked row. But two stores keep an entity's id WITHOUT an FK, where the
database can't see it as a reference, so nothing auto-cleans them:

  • `review_queue.payload_json` — the edition/holding/work/person id is buried inside a
    JSON blob (`{"person_id": 42, …}`). A *pending* item left pointing at a deleted id is
    not just clutter: SQLite recycles integer primary keys, so a future row inheriting that
    id makes the stale item bind authority data onto the WRONG new entity (the accept path
    reads the id straight from the payload). Same shape as the cover-cache id-reuse bug.
  • `promotion` — `holding_id` column + `work_ids`/`person_ids` JSON arrays, no FK. A stale
    person id here can make `revert_proposal` delete the wrong (reused-id) person.

These helpers run on every entity-delete path to drop the matching PENDING review items and
scrub the promotion records. Best-effort hygiene; the validate-at-consume guards in
verify.py / person_work.py / promote.py are the actual correctness backstop. Pure DB work
(no commit here — the caller's delete txn owns it); never raises on a bad payload.

Ownership is the over-purge guard. A work delete must scrub only the review items it OWNS
(`work_authorship` / `work_canonical`), never the SECONDARY `work_id` an edition-owned
`title_proposal` / `edition_metadata` happens to carry — matching on a bare `work_id == wid`
once dropped ~254 proposals for LIVE editions (orphan-audit tail #1). Ownership is declared once,
in `access_api.registry.REVIEW_ITEM_OWNERS`; this module reuses it so the legacy delete paths and
the access-API Work path agree by construction. `sweep_dangling_refs` is the one-shot backfill that
reconciles the accumulated legacy debris (pending items / promotion ids whose owning row is gone)."""
from __future__ import annotations

import json

from catalogue.access_api.registry import (
    PROMOTION_ID_ARRAYS,
    REVIEW_ITEM_OWNERS,
    review_items_owned_by,
)


def _acc(db):
    """A system Access over this connection — the review queue (`acc.review`) + the cross-entity
    existence check (`acc.health.owner_exists`). The caller's delete txn owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def _pending_items(db):
    """(id, item_type, payload_dict) for every PENDING review_queue row — the only ones an accept
    path can still act on. Unparseable payloads are skipped (treated as not-a-match)."""
    out = []
    for rid, item_type, raw in _acc(db).review.reads.all_pending():
        try:
            out.append((rid, item_type, json.loads(raw) if raw else {}))
        except (ValueError, TypeError):
            continue
    return out


def _drop_pending(db, match) -> int:
    """Delete every pending review item whose payload `match(payload)` is truthy."""
    review = _acc(db).review
    ids = [rid for rid, _t, p in _pending_items(db) if match(p)]
    for rid in ids:
        review.writes.delete(rid)
    return len(ids)


def _drop_owned(db, owned_pairs, target_id) -> int:
    """Delete pending items of an OWNED `(item_type, payload_key)` whose key == `target_id`.
    Ownership-scoped: an item whose type isn't in `owned_pairs` is left untouched even if its
    payload happens to mention `target_id` (the over-purge guard)."""
    review = _acc(db).review
    key_by_type = dict(owned_pairs)
    ids = [rid for rid, item_type, p in _pending_items(db)
           if item_type in key_by_type and p.get(key_by_type[item_type]) == target_id]
    for rid in ids:
        review.writes.delete(rid)
    return len(ids)


def _scrub_id_array(db, column: str, gone: int) -> int:
    """Remove integer `gone` from every promotion.<column> JSON array that contains it,
    rewriting the row. `column` is a trusted literal ('work_ids' | 'person_ids')."""
    review = _acc(db).review
    n = 0
    for rid, raw in review.reads.promotion_rows(column):
        try:
            ids = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            continue
        if gone not in ids:
            continue
        review.writes.set_promotion_column(
            rid, column, json.dumps([i for i in ids if i != gone]))
        n += 1
    return n


def purge_edition_refs(db, eid: int, holding_ids) -> dict:
    """Pending review items naming this edition or any of its (already-captured) holding
    ids, plus promotion rows for those holdings. Pass `holding_ids` gathered BEFORE the
    cascade drops the holdings — a review item keys off holding_id, not edition_id."""
    hids = set(holding_ids or [])
    dropped = _drop_pending(db, lambda p: p.get("edition_id") == eid
                            or p.get("holding_id") in hids)
    review = _acc(db).review
    promos = sum(review.writes.delete_promotion(holding_id=hid) for hid in hids)
    return {"review_items": dropped, "promotions": promos}


def purge_work_refs(db, wid: int) -> dict:
    """Pending WORK-OWNED review items naming this work, and the work scrubbed from promotion
    arrays. Ownership-scoped (registry.REVIEW_ITEM_OWNERS): only `work_authorship` / `work_canonical`
    are touched — a secondary `work_id` an edition-owned `title_proposal` / `edition_metadata` carries
    is LEFT ALONE (the ~254-item over-purge fix). The access-API Work path is already correct; this
    brings the legacy delete path in line with it."""
    dropped = _drop_owned(db, review_items_owned_by("work"), wid)
    scrubbed = _scrub_id_array(db, "work_ids", wid)
    return {"review_items": dropped, "promotions": scrubbed}


def purge_person_refs(db, pid: int) -> dict:
    """Pending review items naming this person, and the person scrubbed from promotion
    arrays — so a later revert can't delete the wrong (reused-id) person."""
    dropped = _drop_pending(db, lambda p: p.get("person_id") == pid)
    scrubbed = _scrub_id_array(db, "person_ids", pid)
    return {"review_items": dropped, "promotions": scrubbed}


# ── one-shot backfill: reconcile accumulated legacy debris ───────────────────────
# The per-delete purges above keep things clean GOING FORWARD, but DBs predating them (and the
# pre-fix over-purge era) carry pending items / promotion ids whose owning row was hard-deleted
# long ago. Because SQLite recycles primary keys, each such stale id is a latent id-reuse hazard
# (a future row inheriting the freed id gets bound by the stale item). This sweep is the
# dangling-ref twin of `cli.sweep_orphan_covers`: it reconciles `review_queue` + `promotion`
# against the live rows and drops/scrubs whatever points at an id that no longer EXISTS.
#
# Existence, not soft-delete: a TOMBSTONED root (deleted_at set) still has its row, so its id is
# frozen and NOT a reuse hazard — those are intentionally left for a possible `restore`. Only a
# fully-absent id (the legacy hard-delete) is swept.
_OWNER_TABLES = ("edition", "work", "person", "subject", "collection", "tradition", "holding")


def _owner_exists(db, kind: str, oid) -> bool:
    """Does a row with this id still exist for `kind`? (Existence — a tombstone counts as present.)"""
    if kind not in _OWNER_TABLES:
        return True                                   # unknown kind → never sweep it
    return _acc(db).health.owner_exists(kind, oid)


def sweep_dangling_refs(db, *, apply: bool = False) -> dict:
    """Find (and with `apply`, remove) the non-FK refs whose owning row no longer exists.

      * pending `review_queue` items of a registered (owned) type whose owner id is gone, and
      * `promotion` array ids (work_ids / person_ids) whose entity is gone.

    Dry-run by default (returns the report, touches nothing). Pure DB work — no commit; the caller
    owns the transaction. Mirrors `purge_*_refs` ownership semantics via `registry.REVIEW_ITEM_OWNERS`."""
    dangling_items, kept_items = [], 0
    for rid, item_type, payload in _pending_items(db):
        owner = REVIEW_ITEM_OWNERS.get(item_type)
        if owner is None:
            kept_items += 1                           # untyped/unowned → not our concern
            continue
        kind, key = owner
        oid = payload.get(key)
        if oid is None or _owner_exists(db, kind, oid):
            kept_items += 1
            continue
        dangling_items.append({"id": rid, "item_type": item_type, "owner": kind, "owner_id": oid})

    review = _acc(db).review
    scrubbed_arrays = []
    for kind, column in PROMOTION_ID_ARRAYS.items():
        for rid, raw in review.reads.promotion_rows(column):
            try:
                ids = json.loads(raw) if raw else []
            except (ValueError, TypeError):
                continue
            gone = [i for i in ids if not _owner_exists(db, kind, i)]
            if gone:
                scrubbed_arrays.append({"review_item_id": rid, "column": column, "removed": gone})

    if apply:
        for it in dangling_items:
            review.writes.delete(it["id"])
        for sc in scrubbed_arrays:
            raw = review.reads.promotion_column(sc["review_item_id"], sc["column"])
            ids = json.loads(raw) if raw else []
            gone = set(sc["removed"])
            review.writes.set_promotion_column(
                sc["review_item_id"], sc["column"],
                json.dumps([i for i in ids if i not in gone]))

    return {
        "applied": apply,
        "review_items_kept": kept_items,
        "review_items_dangling": dangling_items,
        "promotion_arrays_scrubbed": scrubbed_arrays,
    }
