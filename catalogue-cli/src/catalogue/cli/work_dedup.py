"""Work dedup + volume grouping pass (FRBR migration, Phase A).

Today every edition mints its own `work`, so the same composition exists as many
duplicate work rows; the FRBR model wants ONE shared work per composition. This
module finds the duplicates and — critically — separates genuine duplicates from
multi-VOLUME sets (Lamrim Chenmo vols 1–3, …), which look identical by title but
must be GROUPED, never merged.

Tiers (per frbr_migration_plan.md §3, sized to the live DB):
  • Tier 1 — shared canonical_number: ZERO in this corpus (canonical_number empty),
    so it's a no-op here; computed anyway for completeness.
  • Tier 2 — identical fold_key(title) AND identical author-set. Each such group is
    CLASSIFIED `volume_set` vs `duplicate` from volume tokens in the title / linked
    editions; volume sets → group_volume_set, duplicates → human-confirmed merge.
  • Tier 3 — fuzzy (token-overlap of titles + shared author) → review_queue, never
    auto-applied.

Nothing here merges automatically. `run_dedup` produces a worklist; the operator
acts via the CLI (`group` / `merge`) or, later, the work-merge picker. Volume
grouping is non-destructive (sets two columns) and may be auto-applied with
`apply-volumes`.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict

from catalogue.access_api import system_conn
from catalogue.db_store import fold_key, init_db
from catalogue.services.work_merge import apply_work_merge, author_set
from catalogue.db_store import default_db_path

# ── volume-token detection ────────────────────────────────────────────────────
# A "Vol. 2" / "Volume II" / "v. 3" / "Part 1" designator → its integer number.
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
          "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}
_VOL_RE = re.compile(
    r"\b(?:vol(?:ume)?|v|part|pt)\b\.?\s*"
    r"(\d{1,2}|[ivx]{1,5})\b",
    re.IGNORECASE,
)


def volume_number(text: str | None) -> int | None:
    """The volume/part number named in `text` (arabic or roman), or None. Returns
    the FIRST match — a title like '… Vol. 1 … Volume II' resolves to 1."""
    if not text:
        return None
    m = _VOL_RE.search(text)
    if not m:
        return None
    tok = m.group(1).lower()
    if tok.isdigit():
        return int(tok)
    return _ROMAN.get(tok)


def _edition_volume_number(db, eid: int) -> int | None:
    """Volume number for an edition, preferring its explicit `volume` column, then
    falling back to a token in its title."""
    from catalogue.access_api import system_conn
    row = system_conn(db).editions.reads.volume_title(eid)
    if not row:
        return None
    return volume_number(row[0]) or volume_number(row[1])


def _work_volume_number(db, wid: int) -> int | None:
    """Volume number for a work: a token in its title, else any of its editions'."""
    from catalogue.access_api import system_conn
    title = system_conn(db).works.reads.representative_title(wid)
    n = volume_number(title)
    if n is not None:
        return n
    for ed in system_conn(db).editions.reads.by_work(wid):
        n = _edition_volume_number(db, ed.id)
        if n is not None:
            return n
    return None


# ── volume grouping (non-destructive) ─────────────────────────────────────────
def group_volume_set(db, edition_ids, *, set_id: int | None = None,
                     commit: bool = True) -> dict:
    """Mark `edition_ids` as one multi-volume set: a shared `volume_set_id` (defaults
    to the lowest edition id — stable) and an ascending `volume_seq` ordered by the
    detected volume number (editions with no detectable number sort last, by id).
    Idempotent."""
    ids = sorted(set(int(e) for e in edition_ids))
    if len(ids) < 2:
        return {"error": "need ≥2 editions to form a volume set"}
    set_id = set_id if set_id is not None else ids[0]
    ordered = sorted(ids, key=lambda e: (_edition_volume_number(db, e) or 99, e))
    from catalogue.access_api import system_conn
    acc = system_conn(db)
    for seq, eid in enumerate(ordered, 1):
        acc.editions.writes.set_volume_set(eid, set_id, seq)
    if commit:
        db.commit()
    return {"volume_set_id": set_id,
            "members": [{"edition_id": e, "volume_seq": i}
                        for i, e in enumerate(ordered, 1)]}


# ── group discovery + classification ──────────────────────────────────────────
def _work_title(db, wid: int) -> str:
    from catalogue.access_api import system_conn
    return system_conn(db).works.reads.representative_title(wid) or f"work#{wid}"


def _member(db, wid: int) -> dict:
    from catalogue.access_api import system_conn
    eds = [{"edition_id": e, "title": t, "volume": v, "isbn": isbn,
            "volume_number": volume_number(v) or volume_number(t)}
           for e, t, v, isbn in system_conn(db).editions.reads.realizations_titled(wid)]
    return {"work_id": wid, "title": _work_title(db, wid),
            "author_person_ids": sorted(author_set(db, wid)),
            "volume_number": _work_volume_number(db, wid),
            "editions": eds}


def _work_isbns(member: dict) -> set:
    return {e["isbn"] for e in member["editions"]
            if e["isbn"] and e["isbn"].strip()}


def shared_isbn(members: list[dict]) -> str | None:
    """The single ISBN every member carries, or None. A strong 'same work' key: if
    all members' editions resolve to exactly ONE common ISBN (and none lack it), they
    are the same book entered twice — safe to auto-merge. Returns None when any member
    has no ISBN or the ISBNs disagree (→ needs human confirm)."""
    per_member = [_work_isbns(m) for m in members]
    if any(not s for s in per_member):
        return None
    union = set().union(*per_member)
    return next(iter(union)) if len(union) == 1 else None


def _classify(members: list[dict]) -> str:
    """Classification for a same-title, same-author group:
      • `isbn_safe`  — all members share ONE ISBN ⇒ same book; safe auto-merge.
      • `volume_set` — ≥2 DISTINCT volume numbers ⇒ a multi-volume set; GROUP, never merge.
      • `duplicate`  — otherwise; a human-confirmed merge candidate.
    ISBN agreement wins over a volume hint (a shared ISBN is one physical book)."""
    if shared_isbn(members) is not None:
        return "isbn_safe"
    nums = {m["volume_number"] for m in members if m["volume_number"] is not None}
    return "volume_set" if len(nums) >= 2 else "duplicate"


def tier2_groups(db) -> list[dict]:
    """Tier 2: works grouped by identical fold_key(title) AND identical author-set,
    each classified volume_set vs duplicate with its evidence. Excludes singletons."""
    by_key: dict[str, list[int]] = defaultdict(list)
    for wid in system_conn(db).works.reads.all_ids():
        by_key[fold_key(_work_title(db, wid))].append(wid)

    groups = []
    for key, wids in by_key.items():
        if len(wids) < 2:
            continue
        by_authors: dict[frozenset, list[int]] = defaultdict(list)
        for wid in wids:
            by_authors[author_set(db, wid)].append(wid)
        for aset, members_ids in by_authors.items():
            if len(members_ids) < 2:
                continue
            members = [_member(db, w) for w in sorted(members_ids)]
            groups.append({
                "fold_key": key,
                "classification": _classify(members),
                "shared_isbn": shared_isbn(members),
                "author_person_ids": sorted(aset),
                "members": members,
                # the merge convention: lowest work id wins
                "suggested_winner": members[0]["work_id"],
            })
    return groups


def tier1_groups(db) -> list[dict]:
    """Tier 1: works sharing (canonical_system, canonical_number). Empty in this
    corpus; here for completeness / future imports."""
    out = []
    for sys, num, wids in system_conn(db).works.reads.canonical_duplicate_groups():
        out.append({"canonical": f"{sys}:{num}", "members": [_member(db, w) for w in wids],
                    "suggested_winner": wids[0]})
    return out


def title_collision_groups(db) -> list[dict]:
    """Works sharing a fold_key(title) but spanning ≥2 DISTINCT author-sets — the
    homonym-check worklist (e.g. promote's merge_candidate: same English title,
    different or absent authors). Weaker than a tier-2 duplicate (which is same
    title AND same author); these need a human eye and are never auto-merged."""
    by_key: dict[str, list[int]] = defaultdict(list)
    for wid in system_conn(db).works.reads.all_ids():
        by_key[fold_key(_work_title(db, wid))].append(wid)
    out = []
    for key, wids in by_key.items():
        if len(wids) < 2:
            continue
        if len({author_set(db, w) for w in wids}) < 2:
            continue        # single author-set → a tier-2 duplicate, not a collision
        out.append({"fold_key": key,
                    "members": [_member(db, w) for w in sorted(wids)],
                    "suggested_winner": sorted(wids)[0]})
    return out


# ── Tier 3 fuzzy → review_queue ────────────────────────────────────────────────
def _title_tokens(text: str) -> set:
    return set(t for t in re.findall(r"[^\W_]+", fold_key(text)) if len(t) > 2)


def enqueue_tier3(db, *, threshold: float = 0.6, commit: bool = True) -> int:
    """Fuzzy candidates: pairs of works with title token-Jaccard ≥ threshold AND a
    shared author, that are NOT already exact-key Tier-2 mates. Enqueued as
    'work_merge' review items (never auto-merged). Returns the count newly queued."""
    works = []
    for wid in system_conn(db).works.reads.all_ids():
        title = _work_title(db, wid)
        works.append((wid, fold_key(title), _title_tokens(title), author_set(db, wid)))

    queued = 0
    for i in range(len(works)):
        wa, ka, ta, aa = works[i]
        for j in range(i + 1, len(works)):
            wb, kb, tb, ab = works[j]
            if ka == kb:                      # exact key → Tier 2's job, skip
                continue
            if not (aa & ab):                 # require a shared author
                continue
            if not ta or not tb:
                continue
            jac = len(ta & tb) / len(ta | tb)
            if jac < threshold:
                continue
            a, b = sorted((wa, wb))
            acc = system_conn(db)
            if acc.review.reads.exists_pending(
                    "work_merge", f'%"winner": {a}%', f'%"loser": {b}%'):
                continue
            acc.review.writes.enqueue("work_merge",
                json.dumps({"winner": a, "loser": b, "jaccard": round(jac, 3),
                            "reason": "fuzzy_title_shared_author",
                            "titles": [_work_title(db, a), _work_title(db, b)]}))
            queued += 1
    if commit:
        db.commit()
    return queued


# ── orchestrator ───────────────────────────────────────────────────────────────
def run_dedup(db, *, fuzzy: bool = True) -> dict:
    """Produce the dedup worklist (no merges applied). Groups are split by
    classification: `isbn_safe` (auto-mergeable — apply via apply_safe_merges),
    `volume_set` (group, never merge), and `duplicate` (human-confirmed merge).
    Tier-3 fuzzy candidates are enqueued to review_queue."""
    t1 = tier1_groups(db)
    t2 = tier2_groups(db)
    queued = enqueue_tier3(db) if fuzzy else 0
    isbn_safe = [g for g in t2 if g["classification"] == "isbn_safe"]
    vol = [g for g in t2 if g["classification"] == "volume_set"]
    dup = [g for g in t2 if g["classification"] == "duplicate"]
    return {"tier1": t1, "isbn_safe": isbn_safe,
            "tier2_volume_sets": vol, "tier2_duplicates": dup,
            "tier3_queued": queued,
            "summary": {"tier1_groups": len(t1), "isbn_safe_merges": len(isbn_safe),
                        "volume_sets": len(vol), "duplicate_candidates": len(dup),
                        "tier3_queued": queued}}


def apply_safe_merges(db, *, commit: bool = True) -> list[dict]:
    """Auto-merge every `isbn_safe` group into its lowest-id member (all members share
    one ISBN ⇒ the same work). Returns the per-group merge reports. Human-confirm
    (`duplicate`) and volume sets are left untouched."""
    reports = []
    for g in tier2_groups(db):
        if g["classification"] != "isbn_safe":
            continue
        winner = g["suggested_winner"]
        for m in g["members"]:
            if m["work_id"] != winner:
                reports.append(apply_work_merge(db, m["work_id"], winner, commit=False))
    if commit:
        db.commit()
    return reports


def apply_volume_sets(db, *, commit: bool = True) -> list[dict]:
    """Auto-group every Tier-2 volume_set (non-destructive). Returns the groupings."""
    out = []
    for g in tier2_groups(db):
        if g["classification"] != "volume_set":
            continue
        eids = [e["edition_id"] for m in g["members"] for e in m["editions"]]
        if len(set(eids)) >= 2:
            out.append(group_volume_set(db, eids, commit=False))
    if commit:
        db.commit()
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────────
def _print_group(g: dict) -> None:
    print(f"  [{g.get('classification', g.get('canonical', '—'))}] "
          f"authors={g['author_person_ids']}  winner=work#{g['suggested_winner']}")
    for m in g["members"]:
        eds = ", ".join(f"e{e['edition_id']}(vol={e['volume_number']})" for e in m["editions"])
        print(f"      work#{m['work_id']} vol={m['volume_number']}  {m['title']!r}  [{eds}]")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("report", "apply-volumes", "apply-merges"):
        p = sub.add_parser(name)
        p.add_argument("db", nargs="?", default=default_db_path())
        if name == "report":
            p.add_argument("--no-fuzzy", action="store_true",
                           help="skip Tier-3 fuzzy enqueue")
    pg = sub.add_parser("group")
    pg.add_argument("db", nargs="?", default=default_db_path())
    pg.add_argument("--editions", required=True, help="comma-separated edition ids")
    pm = sub.add_parser("merge")
    pm.add_argument("db", nargs="?", default=default_db_path())
    pm.add_argument("--work", type=int, required=True, help="loser work id (folded in)")
    pm.add_argument("--into", type=int, required=True, help="winner work id")
    args = ap.parse_args(argv)

    db = init_db(args.db)
    if args.cmd == "report":
        res = run_dedup(db, fuzzy=not args.no_fuzzy)
        s = res["summary"]
        print(f"Tier 1 (canonical_number): {s['tier1_groups']} group(s)")
        print(f"SHARED-ISBN (safe auto-merge — `apply-merges`): {s['isbn_safe_merges']}")
        for g in res["isbn_safe"]:
            _print_group(g)
        print(f"Volume SETS (group, do NOT merge): {s['volume_sets']}")
        for g in res["tier2_volume_sets"]:
            _print_group(g)
        print(f"DUPLICATE candidates (human-confirm merge): {s['duplicate_candidates']}")
        for g in res["tier2_duplicates"]:
            _print_group(g)
        print(f"Tier 3 fuzzy enqueued to review_queue: {s['tier3_queued']}")
    elif args.cmd == "apply-merges":
        reports = apply_safe_merges(db)
        print(f"applied {len(reports)} safe ISBN-keyed work merge(s):")
        for r in reports:
            print(f"  work#{r['merged']} → work#{r['into']}  {r['into_title']!r}")
    elif args.cmd == "apply-volumes":
        groups = apply_volume_sets(db)
        print(f"grouped {len(groups)} volume set(s):")
        for g in groups:
            print(f"  set_id={g['volume_set_id']}: "
                  f"{[(m['edition_id'], m['volume_seq']) for m in g['members']]}")
    elif args.cmd == "group":
        eids = [int(x) for x in args.editions.split(",") if x.strip()]
        res = group_volume_set(db, eids)
        print(res.get("error") or
              f"grouped {len(res['members'])} editions as set {res['volume_set_id']}")
    elif args.cmd == "merge":
        res = apply_work_merge(db, args.work, args.into)
        if res.get("error"):
            print(f"error: {res['error']}")
            return 1
        print(f"merged work#{res['merged']} {res['title']!r} → work#{res['into']} "
              f"{res['into_title']!r}; {len(res['editions_repointed'])} edition(s) repointed, "
              f"+{len(res['aliases_gained'])} alias(es)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
