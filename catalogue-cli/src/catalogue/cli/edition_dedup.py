"""Find and consolidate duplicate editions into one edition with many holdings.

READ-ONLY by default (a report). Mutating actions are explicit and reversible (each
logs a single `consolidate` undo entry). See catalogue/domain/edition_consolidate.py.

    python -m catalogue.cli.edition_dedup                      # dry-run report
    python -m catalogue.cli.edition_dedup --isbn-audit         # normalized-ISBN collisions
    python -m catalogue.cli.edition_dedup --apply 36 --dups 37 # fold #37 into #36
    python -m catalogue.cli.edition_dedup --link-volume-set 411 415 417 418
    python -m catalogue.cli.edition_dedup --apply-all-format-dups --yes
"""
from __future__ import annotations

import argparse
import os

from catalogue.db_store import connect
from catalogue.services import edition_consolidate as EC
from catalogue.db_store import default_db_path

DEFAULT_DB = default_db_path()


def _fmt_member(m):
    forms = "+".join(m["formats"]) or "?"
    isbn = m["isbn_norm"] or "—"
    return (f"    #{m['edition_id']:<4} [{forms:<12}] isbn={isbn:<14} "
            f"text={m['content_len']:<5} {(m['title'] or '')[:60]}")


def report(db) -> None:
    clusters = EC.find_clusters(db)
    by_action = {"format_dup": [], "volume_set": [], "review": []}
    for c in clusters:
        by_action.setdefault(c["action"], []).append(c)

    print(f"\n{'='*78}\nEDITION CONSOLIDATION REPORT — {len(clusters)} multi-edition clusters\n{'='*78}")
    titles = {"format_dup": "FORMAT DUPLICATES → merge into one edition (holdings combine)",
              "volume_set": "VOLUME SETS → link (volume_set_id), do NOT merge",
              "review":     "NEEDS REVIEW → year/text spread suggests a revision or volumes"}
    for action in ("format_dup", "volume_set", "review"):
        cs = by_action.get(action) or []
        print(f"\n── {titles[action]}  ({len(cs)}) " + "─" * 20)
        for c in cs:
            print(f"\n  «{c['title']}»  [{c['note']}]")
            if action == "format_dup":
                print(f"    → canonical #{c['canonical_id']}, fold in {c['dup_ids']}")
            for m in c["members"]:
                print(_fmt_member(m))

    coll = EC.normalized_isbn_collisions(db)
    typos = [x for x in coll if not x["same_title"]]
    if typos:
        print(f"\n── ⚠ NORMALIZED-ISBN COLLISIONS across DIFFERENT titles ({len(typos)}) "
              + "─" * 12)
        for x in typos:
            print(f"  isbn {x['isbn_norm']}:")
            for eid, t, raw in x["members"]:
                print(f"    #{eid:<4} raw={raw!r:18} {(t or '')[:55]}")
        print("  (distinct titles sharing one ISBN ⇒ a data-entry typo — fix the wrong one)")
    print()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Find/merge duplicate editions (one edition, many holdings).")
    ap.add_argument("db", nargs="?", default=DEFAULT_DB)
    ap.add_argument("--isbn-audit", action="store_true", help="only the normalized-ISBN collision report")
    ap.add_argument("--apply", type=int, metavar="CANONICAL", help="canonical edition id to fold dups INTO")
    ap.add_argument("--dups", type=int, nargs="+", help="duplicate edition ids to fold in (with --apply)")
    ap.add_argument("--link-volume-set", type=int, nargs="+", metavar="EID",
                    help="link these editions as one volume set (no merge)")
    ap.add_argument("--apply-all-format-dups", action="store_true",
                    help="consolidate EVERY detected format-duplicate cluster")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt for bulk apply")
    args = ap.parse_args(argv)
    db = connect(args.db)
    db.execute("PRAGMA busy_timeout = 30000")

    if args.isbn_audit:
        for x in EC.normalized_isbn_collisions(db):
            flag = "DUP" if x["same_title"] else "TYPO?"
            print(f"[{flag}] {x['isbn_norm']}: " + ", ".join(f"#{e}" for e, _t, _i in x["members"]))
        return

    if args.link_volume_set:
        print(EC.link_volume_set(db, args.link_volume_set))
        return

    if args.apply is not None:
        if not args.dups:
            ap.error("--apply requires --dups")
        print(EC.consolidate(db, args.apply, args.dups))
        return

    if args.apply_all_format_dups:
        clusters = [c for c in EC.find_clusters(db) if c["action"] == "format_dup"]
        print(f"{len(clusters)} format-duplicate cluster(s) will be consolidated:")
        for c in clusters:
            print(f"  #{c['canonical_id']} ← {c['dup_ids']}  «{c['title']}»")
        if not args.yes and input("proceed? [y/N] ").strip().lower() != "y":
            print("aborted."); return
        for c in clusters:
            r = EC.consolidate(db, c["canonical_id"], c["dup_ids"])
            print(f"  consolidated #{c['canonical_id']}: {r.get('status', r.get('error'))}")
        return

    report(db)


if __name__ == "__main__":
    main()
