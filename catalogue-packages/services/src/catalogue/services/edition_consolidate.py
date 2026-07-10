"""Consolidate duplicate EDITIONS into one manifestation with multiple holdings.

When the same book was ingested in several formats (epub + pdf) the sweep minted a
separate `edition` per file, and the downstream passes duplicated the works too. This
module finds those clusters, classifies each, and — on operator confirmation — folds
the duplicates into one canonical edition so every file lands as a HOLDING of a single
edition, then dedups the works that were duplicated alongside them.

Read-only detection (`find_clusters`, `normalized_isbn_collisions`) is safe to run any
time. `consolidate` MUTATES and is fully reversible: it captures one combined snapshot
of every edition AND work it touches and logs a single `consolidate` undo entry, so a
mistaken merge is one ↩ Undo away.

Identity rule:
  - cluster by folded title; ISBN compared ONLY through normalize_isbn (a booster, never
    decisive — different formats carry different ISBNs; one ISBN can span volumes);
  - a VOLUME set (distinct vol numbers in the filenames, or a wide content-length spread)
    is grouped, NEVER merged;
  - a year mismatch / wide content spread → 'review' (likely a revised edition), not an
    auto-merge.
"""
from __future__ import annotations

import re
from collections import defaultdict

from catalogue.db_store import fold_key
from catalogue.services import contributor_undo as undo
from catalogue.services import entity_undo as EU
from catalogue.services import work_merge
from catalogue.services.isbn import normalize_isbn


def _acc(db):
    """A system Access over this connection — engine-routed edition/work/holding reads, the
    edition-merge op, and the row-snapshot journal. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)

_VOL_RE = re.compile(r"vol\.?\s*(\d+)", re.I)
# Two holdings whose stored text differs by more than this fraction are probably
# different volumes / a revised edition, not the same book in two formats.
_CONTENT_SPREAD = 0.30


# ── small per-edition facts ───────────────────────────────────────────────────
def _holdings(db, eid):
    """(holding_type, form, file_path, archival_pdf_path) per holding of an edition."""
    return _acc(db).holdings.reads.format_rows(eid)


def _formats(db, eid):
    from catalogue.db_store import derive_holding_type
    out = []
    for ht, form, fp, arch in _holdings(db, eid):
        f = ht or derive_holding_type(form, fp, arch)
        if f and f not in out:
            out.append(f)
    return out


def _volume_numbers(db, eid):
    """Volume numbers signalled by this edition — its `volume` column digits plus any
    'Vol N' in its holding filenames. Distinct numbers across a cluster ⇒ a volume set."""
    nums = set()
    vol = _acc(db).editions.reads.volumes([eid]).get(eid)
    if vol:
        nums.update(int(n) for n in re.findall(r"\d+", vol))
    for _ht, _form, fp, _arch in _holdings(db, eid):
        nums.update(int(m) for m in _VOL_RE.findall(fp or ""))
    return nums


def _content_len(db, eid):
    """Cheap proxy for how much text an edition holds (edition_text rows). Used to tell a
    same-book format-dup (similar length) from a volume / revision (very different)."""
    return _acc(db).editions.reads.text_row_count(eid)


def primary_holding(db, eid):
    """The holding to treat as an edition's representative file once it has several — a
    text-bearing one (native / ocr_good) preferred, else the lowest id. Replaces the
    arbitrary `… ORDER BY id LIMIT 1` picks that would otherwise grab an image-only PDF
    over a good EPUB. Returns the holding id, or None."""
    rows = _acc(db).holdings.reads.by_edition(eid)
    if not rows:
        return None
    for h in rows:
        if h.text_status in ("native", "ocr_good"):
            return h.id
    return rows[0].id


# ── detection (read-only) ─────────────────────────────────────────────────────
def _edition_brief(db, eid, title, isbn, year):
    h = _holdings(db, eid)
    return {
        "edition_id": eid, "title": title, "isbn": isbn,
        "isbn_norm": normalize_isbn(isbn or "") or None, "year": year,
        "formats": _formats(db, eid), "n_holdings": len(h),
        "has_archival": any(r[3] for r in h),  # r = (holding_type, form, file_path, archival_pdf_path)
        "content_len": _content_len(db, eid),
    }


def _canonical_of(members):
    """Pick the survivor: most holdings, then has an archival PDF, then most text, then
    lowest id (stable). The others fold into it."""
    return sorted(members, key=lambda m: (
        -m["n_holdings"], not m["has_archival"], -m["content_len"], m["edition_id"]))[0]


def _classify(members):
    """format_dup (merge) | volume_set (link, don't merge) | review (human decides)."""
    vols = [m["_vols"] for m in members]
    distinct_vols = {n for s in vols for n in s}
    if any(vols) and len(distinct_vols) > 1:
        return "volume_set"
    lens = [m["content_len"] for m in members if m["content_len"]]
    if len(lens) >= 2:
        lo, hi = min(lens), max(lens)
        if hi and (hi - lo) / hi > _CONTENT_SPREAD:
            return "review"                     # wide text spread → volume / revision
    years = {m["year"] for m in members if m["year"]}
    if len(years) > 1:
        return "review"                         # different years → likely a revised edition
    return "format_dup"


def find_clusters(db):
    """Editions that look like the same book held more than once. Returns a list of
    {key, action, canonical, members[], isbns, note}. READ-ONLY."""
    groups = defaultdict(list)
    for e in _acc(db).editions.reads.all():
        groups[fold_key(e.title or "")].append((e.id, e.title, e.isbn, e.year))

    clusters = []
    for key, raw in groups.items():
        if len(raw) < 2 or not key:
            continue
        members = [_edition_brief(db, eid, t, isbn, year) for eid, t, isbn, year in raw]
        for m in members:
            m["_vols"] = _volume_numbers(db, m["edition_id"])
        action = _classify(members)
        canon = _canonical_of(members)
        isbns = sorted({m["isbn_norm"] for m in members if m["isbn_norm"]})
        clusters.append({
            "key": key, "action": action, "title": canon["title"],
            "canonical_id": canon["edition_id"],
            "dup_ids": [m["edition_id"] for m in members if m["edition_id"] != canon["edition_id"]],
            "members": [{k: v for k, v in m.items() if not k.startswith("_")} for m in members],
            "isbns": isbns,
            "note": ("ISBNs differ across formats (expected)" if len(isbns) > 1
                     else "shared ISBN" if isbns else "no ISBN"),
        })
    clusters.sort(key=lambda c: (c["action"], c["title"] or ""))
    return clusters


def normalized_isbn_collisions(db):
    """Editions whose NORMALIZED ISBNs collide — catches hyphenation-hidden duplicates and
    data-entry typos (e.g. two different volumes accidentally sharing one ISBN). Flags
    `same_title` (likely a real dup) vs different titles (likely a typo). READ-ONLY."""
    by_norm = defaultdict(list)
    for e in _acc(db).editions.reads.all():
        if not (e.isbn or "").strip():
            continue
        n = normalize_isbn(e.isbn)
        if n:
            by_norm[n].append((e.id, e.title, e.isbn))
    out = []
    for n, members in by_norm.items():
        if len(members) < 2:
            continue
        titles = {fold_key(t or "") for _e, t, _i in members}
        out.append({"isbn_norm": n, "members": members, "same_title": len(titles) == 1})
    return out


# ── apply (mutating, reversible) ──────────────────────────────────────────────
def _dup_work_groups(db, eid):
    """Works linked to `eid` that duplicate each other — same folded primary title AND
    same author set. Returns groups of >1 work id (lowest-id first = the survivor)."""
    acc = _acc(db)
    wids = acc.works.reads.ids_in_edition(eid)
    buckets = defaultdict(list)
    for wid in wids:
        t = acc.works.reads.representative_title(wid)
        key = (fold_key(t) if t else f"w{wid}", work_merge.author_set(db, wid))
        buckets[key].append(wid)
    return [sorted(g) for g in buckets.values() if len(g) > 1]


def _move_edition(db, dup, into):
    """Re-point a duplicate edition's whole subtree onto `into`, then drop it — the
    edition-merge engine op (`acc.editions.writes.merge`). Logs NO undo of its own; the
    caller's combined snapshot covers it."""
    _acc(db).editions.writes.merge(dup, into)


def _snapshot(db, edition_ids, work_ids):
    return {"kind": "consolidate", "ids": sorted({int(e) for e in edition_ids}),
            "editions": EU.snapshot_editions(db, edition_ids),
            "works": EU.snapshot_works(db, work_ids)}


def _restore_consolidation(db, snap):
    """Reverse a consolidate in ONE pass. edition_work FKs BOTH a work and an edition, and
    here both parents were deleted — so every `work` and `edition` parent row must be
    re-inserted BEFORE any edition_work join row, regardless of which sub-snapshot it came
    from. Clear everything for the involved ids (cascades), then reinsert parents, then the
    joins, then relationships."""
    ed, wk = snap["editions"], snap["works"]
    wids = wk["ids"]
    j = _acc(db).journal
    if j.table_exists("relationship"):
        j.clear_two_col("relationship", "from_work_id", "to_work_id", wids)
    EU._clear(db, EU._WORK_TABLES, wk["ids"])
    EU._clear(db, EU._EDITION_TABLES, ed["ids"])

    work_parents = [t for t in EU._WORK_TABLES if t[0] != "edition_work"]
    edition_parents = [t for t in EU._EDITION_TABLES if t[0] != "edition_work"]
    EU._reinsert(db, work_parents, wk)               # work + its non-join children
    EU._reinsert(db, edition_parents, ed)            # edition + its non-join children
    EU._reinsert(db, [("edition_work", "edition_id")], ed)   # join: both parents now exist
    j.insert_rows("relationship", wk["tables"].get("relationship") or [])


def _fingerprint(db, snap):
    return (EU._fingerprint(db, EU._EDITION_TABLES, snap["editions"]["ids"])
            + EU._fingerprint(db, EU._WORK_TABLES, snap["works"]["ids"], with_relationship=True))


undo.register_kind("consolidate", restore=_restore_consolidation, ids_key="ids",
                   missing=lambda db, snap: [], fingerprint=_fingerprint)


def consolidate(db, canonical_id, dup_ids, *, commit=True):
    """Fold `dup_ids` editions into `canonical_id` (their holdings + contributors + works
    move), then dedup the works that were duplicated across them. ONE reversible unit.

    Returns {status, canonical_id, merged_editions, merged_works, undo_token}."""
    acc = _acc(db)
    dup_ids = [d for d in dup_ids if d != canonical_id]
    for eid in [canonical_id, *dup_ids]:
        if not acc.health.owner_exists("edition", eid):
            return {"error": f"no such edition #{eid}"}
    if not dup_ids:
        return {"error": "nothing to merge into the canonical edition"}

    all_eids = [canonical_id, *dup_ids]
    work_ids = sorted({w for e in all_eids for w in acc.works.reads.ids_in_edition(e)})
    snap = _snapshot(db, all_eids, work_ids)

    for dup in dup_ids:
        _move_edition(db, dup, canonical_id)

    merged_works = []
    for group in _dup_work_groups(db, canonical_id):
        winner, *losers = group
        for loser in losers:
            res = work_merge.apply_work_merge(db, loser, winner, commit=False)
            if not res.get("error"):
                merged_works.append({"merged": loser, "into": winner})

    token = undo.log_undo(
        db, "consolidate",
        f"consolidated {len(dup_ids)} edition(s) into #{canonical_id}"
        + (f" + merged {len(merged_works)} duplicate work(s)" if merged_works else ""),
        snap)
    if commit:
        db.commit()
    return {"status": "consolidated", "canonical_id": canonical_id,
            "merged_editions": dup_ids, "merged_works": merged_works, "undo_token": token}


def link_volume_set(db, edition_ids, *, commit=True):
    """Mark editions as volumes of ONE multi-volume publication (shared volume_set_id),
    ordered by volume_seq derived from filename 'Vol N' where available. Does NOT merge."""
    ids = sorted(set(edition_ids))
    if len(ids) < 2:
        return {"error": "need at least two editions to form a volume set"}
    set_id = min(ids)
    seq = 0
    for eid in sorted(ids, key=lambda e: (sorted(_volume_numbers(db, e)) or [9999], e)):
        seq += 1
        nums = sorted(_volume_numbers(db, eid))
        _acc(db).journal.update_row(
            "edition", {"volume_set_id": set_id, "volume_seq": nums[0] if nums else seq},
            {"id": eid})
    if commit:
        db.commit()
    return {"status": "linked", "volume_set_id": set_id, "edition_ids": ids}
