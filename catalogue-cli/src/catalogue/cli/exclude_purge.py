"""Remove already-ingested files that match the exclusion rules (vocab.json
`_exclusions` — ANNOTATED subtrees by default) from the catalogue.

Deletes the matching holdings; an edition all of whose holdings were excluded is
deleted too (cascading its edition_work / edition_subject / edition_author /
work_detection), and any work left with no edition is garbage-collected. Other
data is untouched. Dry-run by default.

Phase-4 conversion: this now goes through the **access-API** instead of opening
the DB and issuing raw DELETEs itself. A fully-excluded edition is removed via
`acc.editions.writes` (which tombstones the edition, hard-deletes its holdings,
and — with `GCOrphans` — tombstones any work left with no live edition); a
partially-excluded edition keeps its clean holdings and only its excluded ones
are removed via `acc.holdings.writes`. Routing it is also a correctness upgrade:
the access-API delete trashes the holding files and purges the non-FK closure
(file-hash caches, `e<id>` cover art) that the old raw DELETE left dangling —
exactly the edition-id-reuse hazard class. Idempotent; dry-run by default.

  python3 -m catalogue.cli.exclude_purge catalogue-db/catalogue.db          # preview
  python3 -m catalogue.cli.exclude_purge catalogue-db/catalogue.db --apply  # delete
"""
import argparse

from catalogue.access_api import system_access
from catalogue.contracts import GCOrphans, OrphanDecision, Ref
from catalogue.services.skip import is_excluded
from catalogue.db_store import default_db_path


def plan(acc):
    """Returns (excluded_holdings[(hid, eid, path)], editions_to_delete[eid]).

    Reads every holding through the access-API and matches each file_path against
    the exclusion rules. An edition is removed only if EVERY one of its holdings
    is excluded."""
    holdings = acc.holdings.reads.all()
    excl = [(h.id, h.edition_id, h.file_path) for h in holdings
            if is_excluded(file_path=h.file_path)]
    excl_hids = {h[0] for h in excl}
    by_ed: dict[int, list[int]] = {}
    for h in holdings:
        by_ed.setdefault(h.edition_id, []).append(h.id)
    del_eds = [eid for eid, hids in by_ed.items()
               if hids and all(h in excl_hids for h in hids)]
    return excl, del_eds


def apply(acc, excl, del_eds):
    """Remove the excluded holdings/editions via the access-API. Returns the
    number of works garbage-collected (orphaned once their editions were gone)."""
    del_set = set(del_eds)
    ew = acc.editions.writes
    hw = acc.holdings.writes

    # Fully-excluded editions: one plan→apply each, in id order, so the semantic-
    # orphan check sees siblings deleted earlier in the run already gone (a work
    # shared by two doomed editions is GC'd when its *last* edition goes).
    removed_works = 0
    for eid in sorted(del_set):
        impact = ew.apply(ew.plan_delete(Ref("edition", eid), GCOrphans()))
        removed_works += sum(1 for o in impact.orphans
                             if o.decision == OrphanDecision.GC)

    # Excluded holdings of editions that survive (some clean holding remains):
    # delete just those holdings. Pass the whole doomed set as siblings so a file
    # shared only among them is trashed, not falsely kept.
    loose = [hid for hid, eid, _fp in excl if eid not in del_set]
    loose_set = frozenset(loose)
    for hid in loose:
        hw.apply(hw.plan_delete(Ref("holding", hid), also_deleting=loose_set - {hid}))

    return removed_works


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--apply", action="store_true", help="delete (default: dry-run)")
    ap.add_argument("--limit-show", type=int, default=15, help="how many sample paths to print")
    args = ap.parse_args(argv)

    with system_access(args.db) as acc:
        excl, del_eds = plan(acc)
        print(f"{len(excl)} excluded holding(s) · {len(del_eds)} edition(s) fully excluded "
              f"[{'APPLY' if args.apply else 'dry-run'}]")
        for hid, eid, fp in excl[: args.limit_show]:
            print(f"  h{hid} (e{eid})  {fp}")
        if len(excl) > args.limit_show:
            print(f"  … and {len(excl) - args.limit_show} more")

        if args.apply:
            removed_works = apply(acc, excl, del_eds)
            print(f"deleted {len(excl)} holding(s), {len(del_eds)} edition(s), "
                  f"{removed_works} orphaned work(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
