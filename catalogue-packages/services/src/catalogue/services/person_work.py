"""Person + work joint resolution (the homonym disambiguator).

Name-only person verification (catalogue/verify.py --kind person) cannot tell two
same-named people apart: every "Nāgārjuna" folds to one key and grabs the same top
Wikidata hit. This pass uses the WORK a person authored as the disambiguator — a work
title (especially with a Toh number) has a near-unique referent where a name does not.

Mechanism, per provisional person:
  1. find the works they're attached to (work_contributor author/translator, edition_work
     translator) — these links already exist in the catalogue;
  2. resolve each WORK via the existing WorkAuthorityResolver → the work's authoritative
     author identity (Q-id + name + cross-links), carried in `consensus.author_ids`;
  3. keep only author-identities whose name EXACTLY matches the local person (the Sajjana
     guard — a wrong work-resolution must not silently bind the wrong person);
  4. if the surviving identities agree (or one work) → BIND the person to that authority id
     (+ harvest its BDRC/DILA/VIAF cross-links), and fill the work's canonical_number too;
     if they DISAGREE → queue a `person_work_joint` conflict (the person row may be two
     people conflated, or a work mis-attributed); if the name only fuzzily matches → queue
     a `needs_confirm` candidate. Auto-bind ONLY on strong-work + exact-name.

Strictly additive: a person with no resolvable work falls back to the name-only path.

MODULARITY: this is a LEAF composer. It imports verify + work_authority only (never the
reverse), and knows NOTHING about Wikidata/VIAF/BDRC or property ids — it speaks to the
abstract `WorkAuthorityResolver` (injectable) and the source-neutral author-identity dict
`{"name","external_id","extra_ids"}`. Swapping the author source changes nothing here.
"""
from __future__ import annotations

import json

from catalogue.db_store import init_db
from .verify import bind_person, name_matches_exactly, name_overlaps
from .work_authority import WorkAuthorityResolver, apply_to_work
from catalogue.db_store import default_db_path

ITEM_TYPE = "person_work_joint"


def _acc(db):
    """A system Access over this connection — engine-routed person/work reads + the review queue."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def _reads(db):
    """The person READ surface bound over this caller's connection (engine-routed, live-only)."""
    return _acc(db).persons.reads


# ── reading a person's works ────────────────────────────────────────────────────
def _person_name(db, pid: int) -> str:
    p = _reads(db).get(pid)
    return p.primary_name if p else ""


def _person_works(db, pid: int) -> list:
    """[(work_id, title, aliases_tuple)] for every work this person is attached to —
    as author/translator (work_contributor) or edition translator (edition_work).
    title = first work_alias; aliases = the rest (other scripts improve the match)."""
    from catalogue.db_store import contributor_store as cs
    wids = cs.person_work_ids(db, pid)
    acc = _acc(db)
    out = []
    for wid in wids:
        names = [t for t, _scheme in acc.works.reads.aliases(wid) if t]
        if names:
            out.append((wid, names[0], tuple(names[1:])))
    return out


# ── review-queue plumbing (mirrors verify._queue_candidate contract) ─────────────
def _candidate_queued(db, pid: int) -> bool:
    return _acc(db).review.reads.exists_pending(ITEM_TYPE, f'%"person_id": {pid}%')


def _queue(db, pid: int, name: str, reason: str, candidates: list, via_works: list,
           *, commit: bool = True) -> None:
    """Queue a person_work_joint review item. `candidates` is the list of distinct
    author identities found (1 for needs_confirm, ≥2 for a conflict); the first is
    surfaced as the headline candidate, the rest carried for the human to pick."""
    head = candidates[0] if candidates else {}
    _acc(db).review.writes.enqueue(ITEM_TYPE, {
        "person_id": pid,
        "person_name": name,
        "candidate_id": head.get("external_id"),
        "candidate_name": head.get("name"),
        "extra_ids": head.get("extra_ids") or {},
        "candidates": candidates,          # all distinct identities (conflict)
        "via_works": via_works,            # [{work_id,title,author_id}]
        "reason": reason,                  # 'work_conflict' | 'needs_confirm'
        "verifier": "person_work",
    })
    if commit:
        db.commit()


# ── the per-person decision ──────────────────────────────────────────────────────
def resolve_person_via_works(db, resolver, pid: int, *, commit: bool = True) -> str:
    """matched | candidate | unmatched | already.

    already   — person already has an external_id (bound elsewhere).
    matched   — works agree on ONE author identity that exactly matches the name → bound.
    candidate — queued: 'work_conflict' (works disagree) or 'needs_confirm' (fuzzy name).
    unmatched — no work, no resolvable work, or no name-corroborated identity."""
    person = _reads(db).get(pid)
    if not person:
        return "unmatched"                     # absent or tombstoned
    if person.external_id:
        return "already"
    name = person.primary_name or ""
    works = _person_works(db, pid)
    if not works:
        return "unmatched"                     # name-only path owns context-less persons

    # Resolve each work; collect (identity, work-info) for authors that carry an id.
    exact, fuzzy, via = [], [], []             # exact/fuzzy name-corroborated identities
    resolved_works = []                        # (wid, consensus) for canonical backfill
    for wid, title, aliases in works:
        c = resolver.resolve(title, aliases=aliases)
        if not getattr(c, "author_ids", None):
            continue
        resolved_works.append((wid, c))
        for ident in c.author_ids:
            iname, iid = ident.get("name"), ident.get("external_id")
            if not iid:
                continue
            rec = {"id": iid, "ident": ident,
                   "via": {"work_id": wid, "title": title, "author_id": iid}}
            if name_matches_exactly(name, iname):
                exact.append(rec)
            elif name_overlaps(name, iname):
                fuzzy.append(rec)

    if not exact and not fuzzy:
        return "unmatched"

    # Distinct identities under the EXACT (auto-bindable) gate, by external_id.
    by_id = {}
    for r in exact:
        by_id.setdefault(r["id"], r)
    via_exact = [r["via"] for r in exact]

    if len(by_id) == 1:
        # All resolvable works agree (or one work) + exact name → BIND.
        rec = next(iter(by_id.values()))
        ident = rec["ident"]
        bind_person(db, pid, ident["external_id"], ident.get("name"),
                    aliases=None, extra_ids=ident.get("extra_ids"), commit=False)
        # Enrich the works too: set canonical_number where confident & missing.
        for wid, c in resolved_works:
            try:
                apply_to_work(db, wid, c, only_if_verified=True, commit=False)
            except Exception:
                pass                           # best-effort; person binding already done
        if commit:
            db.commit()
        return "matched"

    if len(by_id) >= 2:
        # Works disagree on WHO the author is → conflict (possible conflation).
        if not _candidate_queued(db, pid):
            cands = [r["ident"] for r in by_id.values()]
            _queue(db, pid, name, "work_conflict", cands, via_exact, commit=commit)
        return "candidate"

    # No exact match, but a work resolved to a fuzzily-matching author → confirm.
    if not _candidate_queued(db, pid):
        seen, cands, via_fuzzy = set(), [], []
        for r in fuzzy:
            if r["id"] not in seen:
                seen.add(r["id"])
                cands.append(r["ident"])
                via_fuzzy.append(r["via"])
        _queue(db, pid, name, "needs_confirm", cands, via_fuzzy, commit=commit)
    return "candidate"


# ── the walk + CLI (mirrors verify.verify_all / work_authority.resolve_all_works) ─
def resolve_all_person_works(db, resolver=None, *, offline: bool = False,
                             limit: int | None = None, verbose: bool = False) -> dict:
    """Walk every provisional person that has ≥1 work edge through the joint
    resolver. Commits per row (resumable). Returns a status tally. `resolver` is
    injectable (tests/offline pass a fake); defaults to the live WorkAuthorityResolver."""
    resolver = resolver or WorkAuthorityResolver(db=db, offline=offline)
    ids = [r[0] for r in _reads(db).unresolved(limit=limit)]   # provisional+unbound LIVE persons
    tally = {"matched": 0, "candidate": 0, "unmatched": 0, "already": 0}
    if verbose:
        print(f"Person↔work joint resolution — {len(ids)} provisional person(s)…",
              flush=True)
    for i, pid in enumerate(ids, 1):
        status = resolve_person_via_works(db, resolver, pid, commit=True)
        tally[status] += 1
        if verbose:
            mark = {"matched": "✓", "candidate": "?", "unmatched": "·",
                    "already": "»"}[status]
            print(f"  [{i}/{len(ids)}] {mark} {status:9} {_person_name(db, pid)[:64]}",
                  flush=True)
    if verbose:
        print(f"done: {tally}", flush=True)
    return tally


# ── review accept / reject (the (db, item_id, *, commit) -> bool contract) ────────
def accept_person_work_joint(db, item_id: int, *, commit: bool = True) -> bool:
    """Bind a queued `person_work_joint` candidate's identity onto the person (via
    the shared `verify.bind_person`) and resolve the item. A 'work_conflict' item
    has no single answer, so accept is REFUSED for it (the human must disambiguate /
    merge first); only 'needs_confirm' is acceptable. False if missing/not pending/
    conflict/already bound."""
    row = _acc(db).review.reads.get_typed(item_id, ITEM_TYPE)
    if not row or row[1] != "pending":
        return False
    p = json.loads(row[0])
    if p.get("reason") == "work_conflict":
        return False                           # ambiguous — no single id to bind
    pid, cid = p.get("person_id"), p.get("candidate_id")
    if not pid or not cid:
        return False
    from catalogue.services.verify import person_identity_ok
    if not person_identity_ok(db, pid, p.get("person_name")):
        return False                           # person id was recycled — don't bind a stranger
    if not bind_person(db, pid, cid, p.get("candidate_name"), None,
                       p.get("extra_ids"), commit=False):
        return False
    _acc(db).review.writes.resolve(item_id)
    if commit:
        db.commit()
    return True


def reject_person_work_joint(db, item_id: int, *, commit: bool = True) -> bool:
    """Reject a queued `person_work_joint` candidate without binding anything."""
    if _acc(db).review.reads.status_of(item_id, ITEM_TYPE) != "pending":
        return False
    _acc(db).review.writes.reject(item_id)
    if commit:
        db.commit()
    return True


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description="Person↔work joint resolution: disambiguate a provisional person "
                    "by the works they authored/translated. Auto-binds strong+exact; "
                    "queues conflicts and fuzzy matches for review.")
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--offline", action="store_true",
                    help="cache-only; never hit the network (safe during OCR)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-row progress (just the final tally)")
    args = ap.parse_args(argv)
    db = init_db(args.db)            # additive migrations; idempotent
    db.execute("PRAGMA busy_timeout = 30000")
    tally = resolve_all_person_works(db, offline=args.offline, limit=args.limit,
                                     verbose=not args.quiet)
    print("summary:", tally)


if __name__ == "__main__":
    main()
