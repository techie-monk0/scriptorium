"""Catalogue health check — report (and optionally fix) the non-FK orphans the cascade can't reach.

A thin operator front-end over the access-API `OrphanSweep`: it binds a SYSTEM `Access`, runs the
registry-driven scan across every non-FK reference class (hash caches, pending review items,
promotion id-arrays, cover art), and prints the findings. With `--apply` it performs the cleanup
(DB scrubs in one transaction; art files trashed to `.trash/`, recoverable). Read-only by default.

    python -m catalogue.cli.verify                 # health report — touch nothing
    python -m catalogue.cli.verify --apply         # ...then fix what it found
    python -m catalogue.cli.verify --apply -v      # ...and list each orphan

This is the catch-all backstop; the per-delete purges and validate-at-consume guards are the primary
correctness mechanisms. Exit status is non-zero when (in report mode) orphans were found, so it can
gate CI / a cron healthcheck.
"""
from __future__ import annotations

import argparse
import os
import sys

from catalogue.access_api import system_access
from catalogue.db_store import default_db_path


def _total(report: dict) -> int:
    return (len(report["hash_cache_orphans"]) + len(report["review_item_orphans"])
            + len(report["promotion_orphans"]) + len(report["cover_art_orphans"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Report/fix non-FK reference orphans (id-reuse hazards).")
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--apply", action="store_true", help="perform the cleanup (default: report only)")
    ap.add_argument("-v", "--verbose", action="store_true", help="list each orphan found")
    args = ap.parse_args(argv)

    with system_access(args.db) as acc:
        report = acc.health.scan()
        if args.verbose:
            for table, fh in report["hash_cache_orphans"]:
                print(f"  hash-cache  {table}: {fh} (no holding)")
            for it in report["review_item_orphans"]:
                print(f"  review item #{it['id']} ({it['item_type']}) → "
                      f"{it['owner']} #{it['owner_id']} (gone)")
            for pr in report["promotion_orphans"]:
                print(f"  promotion #{pr['review_item_id']}.{pr['column']} → drop {pr['removed']}")
            for path in report["cover_art_orphans"]:
                print(f"  cover art  {path} (no edition)")

        found = _total(report)
        print(f"hash-cache: {len(report['hash_cache_orphans'])}, "
              f"review items: {len(report['review_item_orphans'])}, "
              f"promotion arrays: {len(report['promotion_orphans'])}, "
              f"cover art: {len(report['cover_art_orphans'])}  (total {found}).")

        if args.apply:
            res = acc.health.apply(report)
            print(f"Fixed: {res}.")
            return 0
        if found:
            print("\nReport only — re-run with --apply to clean up.")
            return 1
        print("Clean — no non-FK orphans.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
