"""Backfill `edition.ol_work_key` from each edition's ISBN (OpenLibrary).

The cross-format "already in catalogue" scan verdict (catalogue/domain/intake_match)
clusters editions of one work via their OpenLibrary work key — print/epub/pdf carry
DIFFERENT ISBNs, so the work key is what lets a phone scan of one format match a copy
already held in another. New editions are keyed automatically (capture resolve + the
sweep post-pass); this is the one-time catch-up for the existing back-catalogue.

Read-mostly + resumable: only editions with an ISBN and a still-NULL key are touched,
each committed as it resolves. Uses the polite ThrottledOpener (spaces requests, retries
429/5xx) so a bulk run is not mistaken for a string of 'no record' misses. Never raises
on a miss — that edition is left NULL for a future pass.

    python -m catalogue.cli.backfill_ol_work_key [--db PATH] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse

from catalogue.db_store import connect
from catalogue.services.intake_match import backfill_work_keys
from catalogue.services.isbn import make_fetch  # re-exported for `python -m` callers/back-compat
from catalogue.db_store import default_db_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve + print, but write nothing.")
    args = ap.parse_args(argv)
    conn = connect(args.db)
    stats = backfill_work_keys(
        conn, fetch=make_fetch(), limit=args.limit, dry_run=args.dry_run,
        on_resolved=lambda eid, isbn, key: print(f"edition {eid}  {isbn} → {key}"))
    print(f"\ncandidates={stats['candidates']} resolved={stats['resolved']} "
          f"missed={stats['missed']}" + ("  (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
