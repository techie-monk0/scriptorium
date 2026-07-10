"""Reconcile the non-FK reference stores against the live rows — the dangling-ref backstop.

Two stores keep an entity's id WITHOUT a foreign key, so no cascade reaches them when a row is
deleted (`review_queue.payload_json`, the `promotion.work_ids` / `person_ids` JSON arrays). The
interactive delete paths now purge their own refs on delete (`services.dangling_refs.purge_*`), but
DBs predating that — and the pre-fix over-purge era — carry pending items / promotion ids whose
owning row was hard-deleted long ago. Because SQLite recycles primary keys, each stale id is a
latent id-reuse hazard: a future row inheriting the freed id gets bound by the stale item (the same
class as the orphaned-cover bug `cli.sweep_orphan_covers` sweeps).

This is the one-shot backfill twin: it drops pending review items (of a registered owned type) whose
owner id is gone, and scrubs promotion arrays of ids whose entity is gone. Ownership comes from
`access_api.registry.REVIEW_ITEM_OWNERS`, so it matches the per-delete purges exactly. A TOMBSTONED
(soft-deleted) root still has its row, so its id is frozen and is left alone — only fully-absent ids
are swept. Idempotent; dry-run by default.

Phase-4 conversion: this now goes through the **access-API** (`acc.health.dangling_ref_orphans()` +
`acc.health.apply`) instead of opening the DB itself — the same `OrphanSweep` backstop the `verify`
CLI uses, sliced to just the two reference-store classes. (The broader `verify` CLI also covers
hash-caches + cover art; this stays the focused reference-store tool.)

    python -m catalogue.cli.sweep_dangling_refs              # dry run — report, touch nothing
    python -m catalogue.cli.sweep_dangling_refs --apply       # drop/scrub the dangling refs
    python -m catalogue.cli.sweep_dangling_refs --apply -v    # ...and list each item/array
"""
from __future__ import annotations

import argparse
import os

from catalogue.access_api import system_access
from catalogue.db_store import default_db_path


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Drop pending review items / scrub promotion ids whose owning row is gone.")
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--apply", action="store_true", help="perform the cleanup (default: dry run)")
    ap.add_argument("-v", "--verbose", action="store_true", help="list each dangling ref")
    args = ap.parse_args(argv)

    with system_access(args.db) as acc:
        found = acc.health.dangling_ref_orphans()
        items = found["review_item_orphans"]
        arrays = found["promotion_orphans"]
        if args.apply:
            acc.health.apply({"hash_cache_orphans": [], "cover_art_orphans": [], **found})

    if args.verbose:
        for it in items:
            print(f"  dangling item #{it['id']} ({it['item_type']}) → "
                  f"{it['owner']} #{it['owner_id']} (gone)")
        for sc in arrays:
            print(f"  promotion #{sc['review_item_id']}.{sc['column']} → drop {sc['removed']}")

    drop, scrub = ("dropped", "scrubbed") if args.apply else ("would drop", "would scrub")
    print(f"{len(items)} dangling review item(s) {drop}; "
          f"{len(arrays)} promotion array(s) {scrub}.")
    if not args.apply:
        print("\nDry run — re-run with --apply to perform the cleanup.")


if __name__ == "__main__":
    main()
