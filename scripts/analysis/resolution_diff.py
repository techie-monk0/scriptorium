"""Dry-run resolution diff over ALL authors.

For every provisional, still-unbound person it computes what the DEFAULT person
verification would do vs what `--person-resolution-extensions` would do (extended
honorific/office stripping + alias-based matching), side by side with the
CURRENTLY-bound id. It mirrors `verify.verify_person`'s decision ladder but WRITES
NOTHING to the person rows — so you can see the diff before committing to a real pass.

The only writes are resolver-cache lookups (so re-runs are fast). Point it at a COPY
so even those don't touch the live DB:

    cp catalogue-db/catalogue.db /private/tmp/diff.db
    caffeinate -i python3 resolution_diff.py /private/tmp/diff.db [--bdrc-over-blmp] \\
        [--limit N] [--offline] [--all] [--out diff.csv]

Then run the REAL pass when the diff looks right:
    python3 -m catalogue.verify catalogue-db/catalogue.db --kind person \\
        --person-resolution-extensions [--bdrc-over-blmp] [--person-work-joint-pass]
"""
from __future__ import annotations

import argparse
import csv
import sys
import time

# Run from anywhere: this script lives two levels under the repo root (scripts/<bucket>/).
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from catalogue.services import verify
from catalogue.db_store import default_db_path, init_db


def _outcome(db, verifiers, pid: int, name: str, *, extensions: bool):
    """(status, external_id, via_query, verifier) for one person — the same logic as
    verify.verify_person (HARD hit across any query form wins; else first fuzzy is the
    fallback; work-attached fuzzy → deferred) but with NO write."""
    m, via = None, None
    for q in verify._person_query_forms(db, pid, name, extensions=extensions):
        cand = verify._first_match(db, verifiers, "person", q)
        if cand and not cand.provisional:
            m, via = cand, q
            break
        if cand and m is None:
            m, via = cand, q
    has_work = verify._person_has_work(db, pid)
    if m and not m.provisional:
        return ("matched", m.number, via, m.verifier)
    if has_work:
        return ("deferred", (m.number if m else None), via, (m.verifier if m else None))
    if not m:
        return ("unmatched", None, None, None)
    return ("candidate", m.number, via, m.verifier)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--bdrc-over-blmp", dest="bdrc_over_blmp", action="store_true",
                    help="route persons through the BDRC ElasticSearch verifier")
    ap.add_argument("--offline", action="store_true", help="cache-only, no network")
    ap.add_argument("--limit", type=int, default=None, help="only the first N persons")
    ap.add_argument("--all", action="store_true",
                    help="print unchanged rows too (default: only rows that differ)")
    ap.add_argument("--out", default=None, help="write the full per-person diff to CSV")
    args = ap.parse_args(argv)

    db = init_db(args.db)
    db.execute("PRAGMA busy_timeout = 30000")
    verifiers = verify.default_verifiers(offline=args.offline,
                                         bdrc_over_blmp=args.bdrc_over_blmp)
    ids = db.execute(
        "SELECT id, primary_name, external_id FROM person "
        "WHERE verification_status = 'provisional' AND external_id IS NULL "
        "ORDER BY id" + (f" LIMIT {int(args.limit)}" if args.limit else "")).fetchall()
    print(f"{len(ids)} provisional unbound person(s); chain="
          f"[{', '.join(v.name for v in verifiers)}]"
          f"{' (offline)' if args.offline else ''}", file=sys.stderr, flush=True)

    rows = []
    tally = {"new_match": 0, "new_candidate": 0, "other_change": 0, "unchanged": 0}
    t0 = time.time()
    for i, (pid, name, cur) in enumerate(ids, 1):
        try:
            d = _outcome(db, verifiers, pid, name, extensions=False)
            e = _outcome(db, verifiers, pid, name, extensions=True)
        except Exception as ex:               # a throttle/network blip on one name
            print(f"  ! [{pid}] {name!r}: {type(ex).__name__}: {ex}",
                  file=sys.stderr, flush=True)
            d = e = ("error", None, None, None)
        changed = (d[0], d[1]) != (e[0], e[1])
        if changed:
            tally["new_match" if e[0] == "matched" else
                  "new_candidate" if e[0] == "candidate" else "other_change"] += 1
        else:
            tally["unchanged"] += 1
        rows.append((pid, name, cur, d, e, changed))
        if changed or args.all:
            flag = "★" if e[0] == "matched" else ("?" if e[0] == "candidate" else " ")
            print(f"{flag} [{pid:4}] {(name or '')[:32]:32} "
                  f"default={d[0]:9} {(d[1] or ''):>16}   "
                  f"ext={e[0]:9} {(e[1] or ''):>16}   via={e[2] or ''}", flush=True)
        if i % 25 == 0:
            print(f"  …{i}/{len(ids)} ({time.time() - t0:.0f}s)",
                  file=sys.stderr, flush=True)

    print(f"\nDIFF vs default — new auto-match={tally['new_match']}, "
          f"new candidate={tally['new_candidate']}, other change={tally['other_change']}, "
          f"unchanged={tally['unchanged']}  ({time.time() - t0:.0f}s)")
    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["pid", "name", "current_id", "default_status", "default_id",
                        "ext_status", "ext_id", "ext_via", "ext_verifier", "changed"])
            for pid, name, cur, d, e, changed in rows:
                w.writerow([pid, name, cur, d[0], d[1], e[0], e[1], e[2], e[3],
                            int(changed)])
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
