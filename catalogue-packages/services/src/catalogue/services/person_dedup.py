"""Authority-driven person dedup — collapse `person` rows that refer to the same
real person, identified by a CONNECTED set of authority keys.

Binding does not dedupe: spelling/title variants each get bound to the same (or a
cross-linked) authority id and accumulate as duplicates. This module reframes dedup
as a connected-components problem over authority keys:

    identity(person) = {person.external_id} ∪ {person_external_id.value}

Two records are the same person iff their key-sets are CONNECTED — directly (a
shared key) or transitively (one record's cross-links reach the other's). Union-find
over every bound record's key-set yields the components; each component collapses to
one canonical record (the richest), the rest folded in with `keep_name_alias=True`.

Safety (over-merge is far worse than a missed merge — see authority_dedup_model.md §6):
  * keys are FILTERED to person-typed schemes, so a wrong-type id (a BDRC Topic
    `bdr:T…`, an org/work id) can never bridge two unrelated people;
  * a component is routed to REVIEW (never auto-merged) when it is oversized
    (> max_component) or carries >1 distinct hub id (conflated authorities);
  * the batch runs sandbox-first and appends a JSONL merge log;
  * offline is the default — `expand(..., reharvest=True)` (opt-in) re-harvests live
    cross-links for the deep / asymmetric-link closure, fault-tolerant per id.

    python3 -m catalogue.services.person_dedup catalogue-db/catalogue.db_store.sandbox            # dry run
    python3 -m catalogue.services.person_dedup catalogue-db/catalogue.db_store.sandbox --apply
    python3 -m catalogue.services.person_dedup catalogue-db/catalogue.db_store.sandbox --reharvest --apply
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field

from . import contributor_edit as CE
from catalogue.db_store import integrity as I
from .verify import PERSON_ID_PREFIXES

# Authority keys we treat as person identity. PERSON_ID_PREFIXES is the hub-id
# allowlist (person.external_id); cross-links add DILA, which is only ever a
# cross-link, never a hub. `bdr:T…` (BDRC Topic) and other wrong-type ids fall
# outside this set and are dropped, so they cannot bridge unrelated people (§6.3).
PERSON_KEY_PREFIXES = PERSON_ID_PREFIXES + ("dila:",)

# The hub scheme — Wikidata is the identity hub. >1 distinct hub id in one
# component means the authorities were conflated → route to review (§6.1).
HUB_PREFIX = "wikidata:Q"

# Every table with a FK referencing person(id), and how a merge handles it.
# `repoint` = contributor_store.repoint_person moves the edge; `move`/`carry` =
# contributor_edit.apply_merge relocates the identity row. If the schema grows a
# new person FK absent here, test_person_dedup::test_person_fk_coverage fails,
# forcing a conscious decision rather than silent edge loss on dup delete (§6.10).
PERSON_FK_HANDLED = {
    "work_author": "repoint",          # person_id
    "edition_translator": "repoint",   # person_id
    "edition_author": "repoint",       # person_id (book-level authorship)
    "edition_work": "repoint",         # translator_person_id (per-work override)
    "person_alias": "move",            # _merge_person moves aliases (deduped)
    "person_external_id": "carry",     # apply_merge carries cross-links onto winner
}


def _reads(db):
    """The person READ surface bound over this caller's connection (engine-routed, live-only)."""
    from catalogue.access_api import system_conn
    return system_conn(db).persons.reads


def _is_person_key(value) -> bool:
    return bool(value) and str(value).startswith(PERSON_KEY_PREFIXES)


def _ensure_indexes(db) -> None:
    """Create the dedup lookup indexes if a forked/legacy DB lacks them (schema.sql
    creates them on init_db; a sandbox copy may predate that). Idempotent + cheap."""
    db.execute("CREATE INDEX IF NOT EXISTS person_external_id_value_idx "
               "ON person_external_id(value)")
    db.execute("CREATE INDEX IF NOT EXISTS person_external_id_hub_idx "
               "ON person(external_id)")


# ── identity key-sets ─────────────────────────────────────────────────────────
def key_set(db, pid: int) -> set:
    """Every person-typed authority key record `pid` owns: its hub `external_id`
    plus every harvested `person_external_id` value, filtered to person schemes.
    A tombstoned person owns no keys (the engine reads live only)."""
    return {k for k in _reads(db).authority_keys(pid) if _is_person_key(k)}


def _harvest_cross_links(key: str, verifiers=None) -> set:
    """Live cross-links for one id (reharvest only). Wikidata is the hub and returns
    all cross-links in one hop; non-hub ids aren't reverse-expanded here. Fault
    tolerant: a network failure yields fewer links (under-merge, never over) (§6.14)."""
    if not key.startswith(HUB_PREFIX):
        return set()
    try:
        from . import picker
        _name, _aliases, extra = picker._harvest_extra(key)
        return {v for v in (extra or {}).values() if _is_person_key(v)}
    except Exception:
        return set()


def expand(keys, db, verifiers=None, *, reharvest=False) -> set:
    """Transitive closure of a key-set over cross-links (fixed-point BFS). Offline:
    just the person-typed keys as given (union-find over stored keys already gives
    the offline closure for the batch). reharvest=True: also fetch each id's live
    cross-links until no new key appears — handles asymmetric / late links (§5)."""
    result = {k for k in keys if _is_person_key(k)}
    if not reharvest:
        return result
    frontier = set(result)
    seen = set()
    while frontier:
        k = frontier.pop()
        if k in seen:
            continue
        seen.add(k)
        for nk in _harvest_cross_links(k, verifiers):
            if nk not in result:
                result.add(nk)
                frontier.add(nk)
    return result


# ── canonical selection ───────────────────────────────────────────────────────
def _edge_count(db, pid: int) -> int:
    return _reads(db).edge_count(pid)


def _is_verified(db, pid: int) -> int:
    p = _reads(db).get(pid)
    return 1 if p and p.verification_status in ("verified", "confirmed_local") else 0


def choose_canonical(db, member_pids) -> int:
    """Survivor = most edges, tie-break verified-status, then lowest id (§1.1)."""
    return max(member_pids,
               key=lambda p: (_edge_count(db, p), _is_verified(db, p), -p))


# ── components (union-find) ───────────────────────────────────────────────────
@dataclass
class Component:
    members: list           # all person ids in the component (sorted)
    keys: set               # union of every authority key
    canonical: int          # chosen survivor
    dups: list              # members - canonical (sorted)
    routed: str             # "merge" | "review"
    reason: str = ""        # why routed to review (empty when "merge")
    joins: dict = field(default_factory=dict)  # pid -> shared keys (the "why")


def _persons_with_keys(db, *, verifiers=None, reharvest=False) -> dict:
    """{pid: key-set} for every person owning ≥1 person-typed authority key."""
    out = {}
    for pid in _reads(db).keyed_person_ids():
        ks = key_set(db, pid)
        if reharvest:
            ks = expand(ks, db, verifiers, reharvest=True)
        if ks:
            out[pid] = ks
    return out


def components(db, verifiers=None, *, reharvest=False, max_component: int = 8) -> list:
    """Union-find over all keyed persons → multi-member components, each tagged
    merge|review by the safety guards. Pure provisional (no-key) rows never appear."""
    keysets = _persons_with_keys(db, verifiers=verifiers, reharvest=reharvest)
    parent = {p: p for p in keysets}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    by_key: dict = {}
    for pid, ks in keysets.items():
        for k in ks:
            if k in by_key:
                union(pid, by_key[k])
            else:
                by_key[k] = pid

    clusters: dict = {}
    for pid in keysets:
        clusters.setdefault(find(pid), []).append(pid)

    comps = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        keys = set().union(*(keysets[m] for m in members))
        canonical = choose_canonical(db, members)
        dups = [m for m in members if m != canonical]
        # the "why": each member's keys that another member also owns
        shared = {k for k in keys if sum(1 for m in members if k in keysets[m]) > 1}
        joins = {m: sorted(keysets[m] & shared) for m in members}
        routed, reason = _route(members, keys, max_component)
        comps.append(Component(members, keys, canonical, dups, routed, reason, joins))
    return comps


def _route(members, keys, max_component: int):
    """Decide merge vs review for a component (the over-merge guards, §6.1/§6.2)."""
    hubs = {k for k in keys if k.startswith(HUB_PREFIX)}
    if len(hubs) > 1:
        return "review", f"conflated: {len(hubs)} distinct hub ids {sorted(hubs)}"
    if len(members) > max_component:
        return "review", f"oversized: {len(members)} members > max_component={max_component}"
    return "merge", ""


# ── batch driver ──────────────────────────────────────────────────────────────
def plan_batch(db, verifiers=None, *, reharvest=False, max_component: int = 8) -> dict:
    """Reviewable, NON-mutating report: {'merge': [...], 'review': [...]}. Each merge
    item carries the per-pair plan_merge previews (allow_cross_authority=True)."""
    _ensure_indexes(db)
    merge, review = [], []
    for c in components(db, verifiers, reharvest=reharvest, max_component=max_component):
        item = {"canonical": c.canonical, "members": c.members,
                "dups": c.dups, "keys": sorted(c.keys), "joins": c.joins}
        if c.routed == "merge":
            item["merges"] = [CE.plan_merge(db, dup, c.canonical, allow_cross_authority=True)
                              for dup in c.dups]
            merge.append(item)
        else:
            item["reason"] = c.reason
            review.append(item)
    return {"merge": merge, "review": review}


def _append_log(log_path: str, rec: dict) -> None:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def apply_batch(db, plan: dict, *, keep_name_alias: bool = True,
                log_path=None, commit: bool = True) -> dict:
    """Execute plan['merge']: every dup folded into its ONE canonical, RE-PLANNED
    just-in-time per pair (apply_merge re-reads live state, §6.5), with an
    assert_integrity after each component (§6.10) and a JSONL log line per merge.
    Run INSIDE a sandbox fork. Idempotent on a clean DB (§6.7)."""
    applied, skipped = [], []
    for item in plan["merge"]:
        canon = item["canonical"]
        for dup in item["dups"]:
            via = sorted(item["joins"].get(dup, []))
            res = CE.apply_merge(db, dup, canon, keep_name_alias=keep_name_alias,
                                 allow_cross_authority=True, commit=False)
            if res.get("error"):
                skipped.append({"dup": dup, "into": canon, "error": res["error"]})
                continue
            applied.append(res)
            if log_path:
                _append_log(log_path, {
                    "ts": time.time(), "dup": dup, "dup_name": res["name"],
                    "into": canon, "into_name": res["into_name"], "via": via,
                    "works_repointed": len(res["works_repointed"]),
                    "editions_repointed": len(res["editions_repointed"])})
        try:
            I.assert_integrity(db)    # no new dangling person refs (§6.10)
        except I.IntegrityError:
            db.rollback()             # a component left a dangling ref → abort the WHOLE batch
            raise
    if commit:
        I.verified_commit(db)
    return {"merged": len(applied), "skipped": skipped,
            "components_merged": len(plan["merge"]),
            "components_for_review": len(plan["review"]),
            "details": applied, "review": plan["review"]}


# ── report (which record merged into which) ───────────────────────────────────
def dedup_report(plan: dict, result: dict | None = None) -> dict:
    """The human-facing 'which records merged into which' report, built from a
    `plan_batch` preview and — once it's been run — the `apply_batch` result. The
    SINGLE shape the CLI and the web dedupe page both render, so the preview and the
    applied report read identically. Names come from the plan's pre-merge previews
    (the folded-away rows are gone by the time we report, §6.5). Returns:
      {'applied': bool, 'merged_rows': int,
       'merges': [{'into': id, 'into_name': str,
                   'absorbed': [{'id','name','via':[keys],'works','editions','skipped'}]}],
       'review': [{'members':[ids], 'reason': str}],
       'skipped': [{'dup','into','error'}]}"""
    applied = result is not None
    detail = {d["merged"]: d for d in (result["details"] if applied else [])}
    skipped_ids = {s["dup"] for s in (result["skipped"] if applied else [])}
    merges = []
    for item in plan["merge"]:
        canon = item["canonical"]
        previews = item.get("merges", [])
        cname = previews[0]["canon"]["name"] if previews else f"#{canon}"
        absorbed = []
        for k, dup in enumerate(item["dups"]):
            prev = previews[k] if k < len(previews) else {}
            d = detail.get(dup)
            absorbed.append({
                "id": dup,
                "name": (prev.get("dup") or {}).get("name") or f"#{dup}",
                "via": sorted(item["joins"].get(dup, [])),
                "works": len(d["works_repointed"]) if d else None,
                "editions": len(d["editions_repointed"]) if d else None,
                "skipped": dup in skipped_ids,
            })
        merges.append({"into": canon, "into_name": cname, "absorbed": absorbed})
    return {"applied": applied,
            "merged_rows": result["merged"] if applied else 0,
            "merges": merges,
            "review": [{"members": i["members"], "reason": i["reason"]}
                       for i in plan["review"]],
            "skipped": result["skipped"] if applied else []}


def report_lines(report: dict) -> str:
    """Plain-text rendering of `dedup_report` (the CLI surface)."""
    n_into = len([m for m in report["merges"] if m["absorbed"]])
    head = (f"Merged {report['merged_rows']} duplicate row(s) into {n_into} "
            f"canonical record(s):" if report["applied"]
            else f"Would merge {sum(len(m['absorbed']) for m in report['merges'])} "
                 f"duplicate row(s) into {n_into} canonical record(s):")
    lines = [head]
    for m in report["merges"]:
        lines.append(f"  #{m['into']}  {m['into_name']}  ← absorbs:")
        for a in m["absorbed"]:
            tail = ""
            if a["works"] is not None:
                tail = f"   [{a['works']} work(s), {a['editions']} edition slot(s)]"
            if a["skipped"]:
                tail = "   [SKIPPED]"
            via = (" via " + ", ".join(a["via"])) if a["via"] else ""
            lines.append(f"      #{a['id']}  {a['name']}{via}{tail}")
    if report["review"]:
        lines.append(f"\n{len(report['review'])} component(s) left for review (NOT merged):")
        for r in report["review"]:
            lines.append(f"  members {r['members']} — {r['reason']}")
    if report["skipped"]:
        lines.append(f"\nskipped {len(report['skipped'])}: {report['skipped']}")
    return "\n".join(lines)


# ── on-bind hook (Phase 2) ────────────────────────────────────────────────────
def persons_with_keys(db, keys, *, exclude: int | None = None) -> set:
    """Person ids owning any of `keys` — a hub `external_id` OR a `person_external_id`
    cross-link value (one indexed lookup per key). Pass the FULL harvested key-set
    (hub + cross-links) to catch a record bound under a DIFFERENT scheme — this is the
    cross-scheme match the add-person form runs before creating a duplicate. Keys are
    filtered to person schemes so a wrong-type id can't bridge unrelated people."""
    others = set()
    reads = _reads(db)
    for k in keys:
        if not _is_person_key(k):
            continue
        others.update(reads.persons_with_key(k))
    if exclude is not None:
        others.discard(exclude)
    return others


def _others_sharing_keys(db, pid: int, keys) -> set:
    """Other person ids that own any of `keys` (excludes `pid` itself)."""
    return persons_with_keys(db, keys, exclude=pid)


def dedup_on_bind(db, pid: int, *, auto_merge: bool = True, commit: bool = True) -> dict | None:
    """Called right after a person is bound. Self-sufficient: reads `pid`'s stored
    key-set + harvest_incomplete flag (no network, no threaded state). One indexed
    lookup; a local merge only on a hit. Returns:
      • None                        — no duplicate (complete harvest, nothing shares a key)
      • {'merged_into': sid, ...}   — auto-merged; sid MAY be the OTHER record, so the
                                      caller must treat `pid` as possibly gone (§6.11)
      • {'suggest': [ids], ...}     — review, no mutation: >1 match, conflated hubs, or an
                                      INCOMPLETE harvest (partial key-set → never a
                                      confident merge or a 'checked, no dup', §6.17)."""
    keys = key_set(db, pid)
    if not keys:
        return None
    others = _others_sharing_keys(db, pid, keys)
    incomplete = _reads(db).harvest_incomplete(pid)
    if not others:
        return None
    if incomplete:
        return {"suggest": sorted(others), "reason": "harvest incomplete — partial key-set"}
    if len(others) > 1:
        return {"suggest": sorted(others), "reason": f"{len(others)} candidate records"}
    other = others.pop()
    # conflation guard (§6.1): two DISTINCT hub ids bridged by a cross-link → suggest
    hubs = {k for k in (keys | key_set(db, other)) if k.startswith(HUB_PREFIX)}
    if len(hubs) > 1:
        return {"suggest": [other], "reason": f"conflated: {len(hubs)} hub ids {sorted(hubs)}"}
    if not auto_merge:
        return {"suggest": [other], "reason": "auto_merge disabled"}
    survivor = choose_canonical(db, [pid, other])
    loser = other if survivor == pid else pid
    # NOT journaled for undo: an on-bind auto-merge couples the bind + merge, and a
    # restored loser lands off-worklist (bound) where it's invisible — a confusing
    # "undo". Reverse it instead with Unbind on the survivor, which re-lists the record.
    res = CE.apply_merge(db, loser, survivor, allow_cross_authority=True, commit=commit)
    if res.get("error"):
        return {"suggest": [other], "reason": res["error"]}
    return {"merged_into": survivor, "merged_away": loser,
            "via": sorted(keys & key_set(db, survivor))}


# ── CLI ───────────────────────────────────────────────────────────────────────
def main(argv=None) -> None:
    from catalogue.db_store import connect
    from .verify import default_verifiers
    ap = argparse.ArgumentParser(
        description="Authority-driven person dedup. Run on a SANDBOX copy "
                    "(catalogue/sandbox.py fork) and promote after review.")
    ap.add_argument("db", help="a COPY of the catalogue (--apply writes to it)")
    ap.add_argument("--apply", action="store_true", help="perform the merges (default: dry run)")
    ap.add_argument("--reharvest", action="store_true",
                    help="re-fetch live cross-links for the deep/asymmetric closure (slow)")
    ap.add_argument("--max-component", type=int, default=8,
                    help="components larger than this go to review, not auto-merge")
    ap.add_argument("--log", default=None, help="JSONL merge-log path (with --apply)")
    args = ap.parse_args(argv)

    db = connect(args.db)
    verifiers = default_verifiers(offline=False) if args.reharvest else None
    plan = plan_batch(db, verifiers, reharvest=args.reharvest,
                      max_component=args.max_component)
    if args.apply:
        res = apply_batch(db, plan, log_path=args.log, commit=True)
        print(report_lines(dedup_report(plan, res)))
    else:
        print(report_lines(dedup_report(plan)))
        print("\n(dry run — pass --apply to perform the merges)")


if __name__ == "__main__":
    main()
