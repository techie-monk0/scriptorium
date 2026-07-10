"""Delete cached cover art whose edition no longer exists — the catch-all backstop for
the edition-id-reuse bug.

The cover cache is keyed by edition id on disk (`e<id>.jpg`, `spine-e<id>.*`, `e<id>.miss`,
pinned `e<id>.*`) — filenames the database can't see as references, so no cascade reaches them.
Because SQLite recycles primary keys, a future import inheriting a freed id would serve the deleted
book's cover (a new Uttaratantra PDF once showed Lisa Jewell's "None Of This Is True").

Phase-4 conversion: this now goes through the **access-API** (`acc.health` OrphanSweep) instead of
opening the DB and walking files itself — the `e<id>` scheme, the live-edition reconcile, and the
file effects all live behind the access layer (registry + Backing port). Orphans are TRASHED to
`.trash/` (recoverable) rather than hard-removed, matching the access-API delete philosophy.
Idempotent; dry-run by default.

    python -m catalogue.cli.sweep_orphan_covers              # dry run — list orphans, touch nothing
    python -m catalogue.cli.sweep_orphan_covers --apply       # trash the orphaned art
    python -m catalogue.cli.sweep_orphan_covers --apply -v    # ...and list each file
"""
from __future__ import annotations

import argparse
import os

from catalogue.access_api import system_access
from catalogue.contracts import FileOp
from catalogue.db_store import default_db_path


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Trash cached/pinned cover art whose edition no longer exists.")
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--cache", default=None, help="cover cache dir (default: <db dir>/.cover-cache)")
    ap.add_argument("--pinned", default=None, help="pinned overrides dir (default: <db dir>/covers-pinned)")
    ap.add_argument("--apply", action="store_true", help="trash the orphans (default: dry run)")
    ap.add_argument("-v", "--verbose", action="store_true", help="list each orphaned file")
    args = ap.parse_args(argv)

    with system_access(args.db) as acc:
        if args.cache is not None:
            acc.cover_cache = args.cache
        if args.pinned is not None:
            acc.cover_pinned = args.pinned
        orphans = acc.health.cover_art_orphans()
        if args.verbose:
            for path in orphans:
                print(f"  orphan: {path}")
        if args.apply:
            acc.backing.run([FileOp("trash", p) for p in orphans], acc.trash_dir)
            print(f"Trashed {len(orphans)} orphaned art file(s) to {acc.trash_dir}.")
        else:
            print(f"Dry run: {len(orphans)} orphaned art file(s) would be trashed. "
                  f"Re-run with --apply.")


if __name__ == "__main__":
    main()
