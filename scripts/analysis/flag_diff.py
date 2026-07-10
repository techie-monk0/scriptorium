"""Per-flag resolution diff over CURRENT resolution, for the person pass.

Read-only on the live catalogue — point it at a COPY (only resolver-cache rows and,
for the joint column, person rows on that throwaway copy are written). For every
provisional person it reports the flags the way they actually compose — each one only
attacks what the previous left DEFERRED — so each column is the *marginal* binding
that flag adds over the current resolution:

  current          person.external_id bound TODAY (mostly NULL — pass not yet run live)
  +extensions      --person-resolution-extensions name pass (extended strip + aliases)
  +bdrc-over-blmp  BDRC ElasticSearch, re-tried on those left DEFERRED by +extensions
  +joint           work-driven joint pass, run on those STILL deferred after ES

Usage:
    cp catalogue-db/catalogue.db /private/tmp/flagdiff.db
    caffeinate -i python3 -u flag_diff.py /private/tmp/flagdiff.db --out flag_diff.csv
    #   --limit N for a quick sample · --offline for cache-only
"""
from __future__ import annotations

import argparse
import csv
import sys
import time

# Run from anywhere: this script lives two levels under the repo root (scripts/<bucket>/).
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from catalogue.services import person_work
from catalogue.services import verify
from catalogue.db_store import init_db
from catalogue.services.work_authority import WorkAuthorityResolver


def _name_outcome(db, verifiers, pid, name, *, extensions):
    """(status, external_id, via) — verify.verify_person's decision ladder, no write."""
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
        return ("matched", m.number, via)
    if has_work:
        return ("deferred", (m.number if m else None), via)
    if not m:
        return ("unmatched", None, None)
    return ("candidate", m.number, via)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="?", default="catalogue-db/catalogue.db")
    ap.add_argument("--offline", action="store_true", help="cache-only, no network")
    ap.add_argument("--limit", type=int, default=None, help="only the first N persons")
    ap.add_argument("--out", default=None, help="write the full per-person CSV here")
    args = ap.parse_args(argv)

    db = init_db(args.db)
    db.execute("PRAGMA busy_timeout = 30000")
    base_chain = verify.default_verifiers(offline=args.offline)
    es_chain = verify.default_verifiers(offline=args.offline, bdrc_over_blmp=True)
    resolver = WorkAuthorityResolver(db=db, offline=args.offline)

    persons = db.execute(
        "SELECT id, primary_name, external_id FROM person "
        "WHERE verification_status = 'provisional' AND external_id IS NULL "
        "ORDER BY id" + (f" LIMIT {int(args.limit)}" if args.limit else "")).fetchall()
    n = len(persons)
    print(f"{n} provisional unbound person(s)", file=sys.stderr, flush=True)
    t0 = time.time()

    rows = []
    tally = {"ext_matched": 0, "es_rescued": 0, "joint_matched": 0,
             "joint_candidate": 0, "still_deferred": 0, "unmatched": 0,
             "already_current": 0}
    for i, (pid, name, cur) in enumerate(persons, 1):
        rec = {"pid": pid, "name": name, "current": cur,
               "ext": ("", "", ""), "es": ("", "", ""), "joint": ("", "")}
        try:
            ext = _name_outcome(db, base_chain, pid, name, extensions=True)
            rec["ext"] = ext
            if cur:
                tally["already_current"] += 1
            elif ext[0] == "matched":
                tally["ext_matched"] += 1

            if ext[0] == "deferred":
                # --bdrc-over-blmp re-tried on this deferred person.
                es = _name_outcome(db, es_chain, pid, name, extensions=True)
                rec["es"] = es
                if es[0] == "matched":
                    tally["es_rescued"] += 1
                elif es[0] == "deferred":
                    # still deferred → the joint pass (REAL, binds on the copy).
                    st = person_work.resolve_person_via_works(db, resolver, pid,
                                                              commit=True)
                    jid = db.execute("SELECT external_id FROM person WHERE id = ?",
                                     (pid,)).fetchone()[0]
                    rec["joint"] = (st, jid)
                    if st == "matched":
                        tally["joint_matched"] += 1
                    elif st == "candidate":
                        tally["joint_candidate"] += 1
                    else:
                        tally["still_deferred"] += 1
            elif ext[0] == "unmatched":
                tally["unmatched"] += 1
        except Exception as ex:                      # one name's network blip
            print(f"  ! [{pid}] {name!r}: {type(ex).__name__}: {ex}",
                  file=sys.stderr, flush=True)
        rows.append(rec)

        # Print only persons where SOME flag binds where current did not.
        bound = next((c for c in (rec["ext"][:2] if rec["ext"][0] == "matched" else (),
                                  rec["es"][:2] if rec["es"][0] == "matched" else (),
                                  rec["joint"] if rec["joint"][0] == "matched" else ())
                      if c), None)
        gained = (not cur) and any(s == "matched"
                                   for s in (rec["ext"][0], rec["es"][0], rec["joint"][0]))
        if gained:
            who = ("extensions" if rec["ext"][0] == "matched"
                   else "bdrc-es" if rec["es"][0] == "matched" else "joint")
            ident = (rec["ext"][1] if rec["ext"][0] == "matched"
                     else rec["es"][1] if rec["es"][0] == "matched" else rec["joint"][1])
            print(f"★ [{pid:4}] {(name or '')[:32]:32} {who:11} {ident}", flush=True)
        if i % 25 == 0:
            print(f"  …{i}/{n} ({time.time() - t0:.0f}s)", file=sys.stderr, flush=True)

    print(f"\nMARGINAL BINDS over current: extensions={tally['ext_matched']}, "
          f"bdrc-over-blmp(on deferred)={tally['es_rescued']}, "
          f"joint(on still-deferred)={tally['joint_matched']}  | "
          f"joint candidates={tally['joint_candidate']}, "
          f"still deferred={tally['still_deferred']}, "
          f"unmatched={tally['unmatched']}, already-bound={tally['already_current']}"
          f"  ({time.time() - t0:.0f}s)")
    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["pid", "name", "current_id",
                        "ext_status", "ext_id", "ext_via",
                        "es_status", "es_id", "es_via",
                        "joint_status", "joint_id"])
            for r in rows:
                w.writerow([r["pid"], r["name"], r["current"],
                            r["ext"][0], r["ext"][1], r["ext"][2],
                            r["es"][0], r["es"][1], r["es"][2],
                            r["joint"][0], r["joint"][1]])
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
