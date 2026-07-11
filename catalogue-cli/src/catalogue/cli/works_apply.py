"""Apply verified single-work detections (work_detection) into the canonical tables
— the rebuild. Dry-run by default. BACK UP first (it drops degenerate works):
`cp private/catalogue-db/catalogue.db private/catalogue-db/catalogue.db_store.pre-apply-$(date +%s).bak`.

  python3 -m catalogue.cli.works_apply DB                       # preview counts
  python3 -m catalogue.cli.works_apply DB --apply               # apply all single detections
  python3 -m catalogue.cli.works_apply DB --only modern --apply # just the 'modern' ones
  python3 -m catalogue.cli.works_apply DB --eid 312 --apply
"""
import argparse
import json
from collections import Counter

from catalogue.db_store import init_db
from catalogue.services import works_apply as WA
from catalogue.db_store import default_db_path


def _targets(db, *, only=None, eid=None):
    from catalogue.access_api import system_conn
    out = []
    for e, _kind, pj in system_conn(db).editions.reads.detections("single"):
        p = json.loads(pj)
        if p.get("applied"):
            continue
        if eid is not None and e != eid:
            continue
        if only and p.get("determination") != only:
            continue
        out.append((e, p.get("determination")))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run preview)")
    ap.add_argument("--only", choices=("classical", "modern"), help="restrict to one determination")
    ap.add_argument("--eid", type=int, default=None)
    args = ap.parse_args(argv)
    db = init_db(args.db)

    targets = _targets(db, only=args.only, eid=args.eid)
    print(f"{len(targets)} unapplied single detection(s): {dict(Counter(d for _, d in targets))} "
          f"[{'APPLY' if args.apply else 'dry-run'}]")
    if args.apply:
        applied = 0
        for e, _ in targets:
            if WA.apply_single(db, e, commit=False).get("status") == "applied":
                applied += 1
        db.commit()
        print(f"applied {applied} edition(s)")
    else:
        print("dry-run — re-run with --apply (back up the DB first)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
