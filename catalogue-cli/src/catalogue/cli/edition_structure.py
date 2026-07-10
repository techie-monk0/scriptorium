"""Mark editions single-work vs multi-work (drives which detection runs).

A "work" is a Sanskrit/Tibetan root text or commentary. Tell the catalogue which
editions hold SEVERAL such texts (anthologies, root+commentary) — those get the
segmentation pass; the rest are single-work and get the single-text autodetect.

  # list every edition with its current structure + the autodetected guess
  python3 -m catalogue.cli.edition_structure DB

  # seed all unset editions from the autodetected proposal guess (then correct)
  python3 -m catalogue.cli.edition_structure DB --seed

  # set explicitly (comma/space-separated edition ids)
  python3 -m catalogue.cli.edition_structure DB --multi "12, 45, 67"
  python3 -m catalogue.cli.edition_structure DB --single 88

The checkbox web tool (/editions/structure) is the friendlier way to do this in
bulk; this CLI is the scriptable twin.
"""
import argparse

from catalogue.db_store import init_db
from catalogue.services import edition_structure as ES
from catalogue.db_store import default_db_path


def _ids(spec):
    return [int(x) for x in spec.replace(",", " ").split()] if spec else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--seed", action="store_true",
                    help="fill unset editions from the autodetected proposal guess")
    ap.add_argument("--multi", help="edition ids to mark multi_work")
    ap.add_argument("--single", help="edition ids to mark single_work")
    args = ap.parse_args(argv)
    db = init_db(args.db)

    touched = 0
    if args.seed:
        touched += ES.seed_from_proposals(db, only_unset=True)
        print(f"seeded {touched} unset edition(s) from proposal guesses")
    if args.multi or args.single:
        touched += ES.set_many(db, multi_ids=_ids(args.multi), single_ids=_ids(args.single))
    if touched:
        db.commit()

    eds = ES.list_editions(db)
    n_multi = sum(1 for e in eds if e["structure"] == "multi_work")
    n_single = sum(1 for e in eds if e["structure"] == "single_work")
    n_unset = sum(1 for e in eds if not e["structure"])
    print(f"\n{len(eds)} editions — {n_multi} multi · {n_single} single · {n_unset} unset")
    for e in eds:
        mark = {"multi_work": "[M]", "single_work": "[S]"}.get(e["structure"], "[ ]")
        g = f"guess={e['guess']}" if e["guess"] else ""
        print(f"  {mark} #{e['id']:<5} {e['title'][:60]:<60} {g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
