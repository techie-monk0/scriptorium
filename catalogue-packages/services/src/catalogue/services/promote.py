"""Promote `book_toc_pattern` review proposals into canonical rows (§8 step 5).

A proposal (built by `process._emit_book_proposal`) is a container description:
the book's title-page author(s)/translator(s) plus a list of works. Promoting it
materialises canonical `work` / `person` / `work_contributor` / `edition_work`
rows and records exactly what it created in `promotion`, so a revert deletes
precisely those rows and nothing shared.

Names are NFC-normalised and deduped on the §4.2 fold-key (`person_alias.
normalized_key == fold_key(name)`) — this is also where born-digital mojibake
author strings collapse onto an existing person (punch-list M4). BDRC/84000
verification is a SEPARATE, idempotent pass over the created rows (see
`whats_next.md` B) — deliberately not done here so accepting stays instant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from catalogue.db_store import contributor_store as cs
from catalogue.db_store import add_alias, fold_key, nfc
from . import work_identity
from .names import split_contributors, split_name_dates


def _acc(db):
    """A system Access over this connection — engine-routed person/work/edition/holding reads +
    writes, the review queue + promotion, and the row-snapshot journal. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)

# Buckets the review UI segments `book_toc_pattern` proposals into. `no_author`
# is checked first so an author-less proposal always lands in the review bucket
# regardless of its structure.
SEGMENTS = ("single_work", "unsegmented", "multi_work", "no_author")


def bucket(payload: dict) -> str:
    """Which review segment a proposal belongs to."""
    works = payload.get("works") or []
    has_author = bool(payload.get("book_authors")) or any(
        w.get("authors") for w in works
    )
    if not has_author:
        return "no_author"
    structure = payload.get("structure")
    if structure == "multi_work":
        return "multi_work"
    if structure == "collection_unsegmented":
        return "unsegmented"
    return "single_work"


def proposal_summary(payload: dict) -> dict:
    """At-a-glance fields for the master list + side panel (no DB access)."""
    works = payload.get("works") or []
    authors = list(payload.get("book_authors") or [])
    if not authors:  # fall back to per-work authors for the label
        for w in works:
            for a in w.get("authors") or []:
                if a not in authors:
                    authors.append(a)
    return {
        "holding_id": payload.get("holding_id"),
        "structure": payload.get("structure"),
        "source": payload.get("source"),
        "authors": authors,
        "translators": list(payload.get("book_translators") or []),
        "verified": bool(payload.get("contributors_verified")),
        "confidence": payload.get("contributors_confidence"),
        "bucket": bucket(payload),
        "works": [
            {
                "title": w.get("title") or "(untitled)",
                "kind": w.get("kind") or "work",
                "authors": list(w.get("authors") or []),
                "translators": list(w.get("translators") or []),
                "author_inherited": bool(w.get("author_inherited")),
            }
            for w in works
        ],
    }


# ── Person dedup ────────────────────────────────────────────────────────────
def get_or_create_person(db, name: str, role_hint: str | None = None):
    """Return (person_id, created). Dedupes on the §4.2 fold-key: a name whose
    fold-key already exists as a `person_alias` reuses that person (so spelling/
    mojibake variants collapse). Trailing biographical dates are split off the
    name into `person.dates` (so the key matches a dated/undated spelling alike).
    New persons get an NFC `primary_name` + a seed alias. `created` is True only
    when a fresh `person` row was inserted."""
    clean, dates = split_name_dates(nfc(name).strip())
    # Fold-key dedup, LIVE-only (engine-routed): a tombstoned person owning this fold is
    # NOT reused — promotion would otherwise resurrect a deleted person by re-linking it.
    existing = _acc(db).persons.reads.find_by_alias_fold(clean)
    if existing is not None:
        return existing, False
    pid = _acc(db).persons.writes.insert_person(clean, role_hint, dates)
    add_alias(db, "person", pid, clean, "english")
    return pid, True


# ── Promote / revert one proposal ─────────────────────────────────────────────
@dataclass
class PromotionResult:
    review_item_id: int
    holding_id: int | None = None
    work_ids: list = field(default_factory=list)         # all works attached (created + reused)
    created_work_ids: list = field(default_factory=list)  # only the works THIS promotion created
    merge_candidate_work_ids: list = field(default_factory=list)  # title-collision, author unconfirmed
    created_person_ids: list = field(default_factory=list)
    status: str = "promoted"          # promoted | already | no_edition | not_proposal
    message: str = ""


def _load_item(db, review_item_id: int):
    r = _acc(db).review.reads.get(review_item_id)
    return (r["item_type"], r["payload_json"], r["status"]) if r else None


def promote_proposal(db, review_item_id: int, *, commit: bool = True) -> PromotionResult:
    """Materialise one `book_toc_pattern` proposal into canonical rows.

    Idempotent: a proposal that already has a `promotion` row is a no-op
    (status="already"). Returns a PromotionResult describing what was created."""
    row = _load_item(db, review_item_id)
    if not row or row[0] != "book_toc_pattern":
        return PromotionResult(review_item_id, status="not_proposal",
                               message="not a book_toc_pattern item")
    if _acc(db).review.reads.promotion_exists(review_item_id):
        return PromotionResult(review_item_id, status="already",
                               message="already promoted")

    payload = json.loads(row[1])
    holding_id = payload.get("holding_id")
    er = _acc(db).holdings.reads.get(holding_id)
    if not er:
        return PromotionResult(review_item_id, holding_id=holding_id,
                               status="no_edition",
                               message=f"holding {holding_id} has no edition")
    edition_id = er.edition_id

    res = PromotionResult(review_item_id, holding_id=holding_id)
    # Cache persons within this proposal so the same name across works is one row.
    seen: dict[str, int] = {}

    def resolve_person(name, role_hint):
        clean = nfc(name).strip()
        if not clean:
            return None
        key = fold_key(clean)
        if key in seen:
            return seen[key]
        pid, created = get_or_create_person(db, clean, role_hint)
        seen[key] = pid
        if created:
            res.created_person_ids.append(pid)
        return pid

    # FRBR: author lives on the WORK (work_author); translator on the EDITION
    # (edition_translator). Translators are accumulated across all the edition's
    # works and set once at book level after the loop.
    edition_author_pids: set = set()
    edition_translator_pids: list = []
    for seq, w in enumerate(payload.get("works") or [], start=1):
        kind = (w.get("kind") or "work").strip()
        notes = kind if kind in ("root", "commentary") else None
        title = (w.get("title") or "").strip() or "(untitled)"

        # Resolve this work's authors FIRST — work identity needs them: an
        # English-title match only reuses an existing work when an author agrees
        # (homonym safety). A contributor field may be several people mashed
        # together; split it (confidently) before dedup/linking.
        work_author_pids: list = []
        for a in w.get("authors") or []:
            for nm in split_contributors(a):
                pid = resolve_person(nm, "author")
                if pid is not None and pid not in work_author_pids:
                    work_author_pids.append(pid)

        # Work identity at creation (the get_or_create_person twin): attach to an
        # existing Work — same canonical#, original-language title, or English
        # title+author — instead of forking. Non-destructive: an unconfirmed
        # title collision creates a flagged merge-candidate, never an auto-merge.
        wid, created, merge_cand = work_identity.get_or_create_work(
            db, english_title=title, author_pids=work_author_pids, notes=notes)
        if created:
            for pid in work_author_pids:
                cs.add_work_author(db, wid, pid)
            from catalogue.services import subjects as S
            S.ensure_categorized(db, "work", wid)   # never subject-less; review will flag it
            res.created_work_ids.append(wid)
        if merge_cand:
            res.merge_candidate_work_ids.append(wid)
        # The work's authors (created or reused) feed the translator self-skip below.
        edition_author_pids.update(cs.work_author_ids(db, wid))

        for t in w.get("translators") or []:
            for nm in split_contributors(t):
                pid = resolve_person(nm, "translator")
                # Skip a translator who is also an author of this book — the contributor
                # resolver sometimes mirrors the author into the translator slot; recording
                # someone as their own translator adds no catalogue value and misleads.
                if pid is not None and pid not in edition_author_pids \
                        and pid not in edition_translator_pids:
                    edition_translator_pids.append(pid)

        _acc(db).works.writes.link_to_edition(
            edition_id, wid, seq, (w.get("locator") or "").strip() or None)
        res.work_ids.append(wid)

    cs.set_edition_translators(db, edition_id, edition_translator_pids)

    # Record only the works THIS promotion created — revert deletes these, and a
    # reused (shared) work must survive. The edition's links to reused works are
    # detached separately in revert_proposal.
    _acc(db).review.writes.insert_promotion(
        review_item_id, holding_id, res.created_work_ids, res.created_person_ids)
    _acc(db).review.writes.set_status(review_item_id, "promoted")
    if commit:
        db.commit()
    return res


def revert_proposal(db, review_item_id: int, *, commit: bool = True) -> PromotionResult:
    """Undo one promotion: delete exactly the works it created (cascades clear
    work_contributor / edition_work / work_alias) and garbage-collect any person
    those works referenced that no surviving row still uses. Flips the queue item
    back to pending. No-op if the item was never promoted."""
    acc = _acc(db)
    prow = acc.review.reads.promotion(review_item_id)
    if not prow:
        return PromotionResult(review_item_id, status="already",
                               message="not promoted")
    work_ids = json.loads(prow[0])      # CREATED works only — safe to delete
    res = PromotionResult(review_item_id, holding_id=prow[2],
                          work_ids=work_ids, status="reverted")

    # The edition this promotion attached (via its holding) — needed even when
    # every work was REUSED (created work_ids empty) so we still detach the edition.
    erow = acc.holdings.reads.get(prow[2])
    promo_edition = erow.edition_id if erow else None

    # Every work this edition is attached to (created + reused). After detaching
    # the edition, any of these left with NO edition is dead and gets deleted —
    # this removes created works AND reference-count-collects a shared work once
    # its last edition goes (so a creator and a reuser both reverting leaks nothing).
    if promo_edition is not None:
        linked = set(acc.works.reads.ids_in_edition(promo_edition))
    else:
        linked = set()
    linked |= set(work_ids)

    # Persons to consider for GC: recorded created persons (a multi-author proposal
    # may mint a person it never links when the work is reused) + authors of every
    # work this edition touched + translators of the affected editions.
    editions = {ed.id for wid in linked for ed in acc.editions.reads.by_work(wid)}
    if promo_edition is not None:
        editions.add(promo_edition)
    affected = set(json.loads(prow[1]))
    for wid in linked:
        affected.update(cs.work_author_ids(db, wid))
    for eid in editions:
        affected.update(cs.edition_translator_ids(db, eid))

    if promo_edition is not None:
        acc.journal.clear_eq("edition_work", "edition_id", promo_edition)
    for wid in linked:
        if not acc.works.reads.has_edition_link(wid):
            acc.works.writes.hard_delete(wid)            # cascades work_author/alias

    # An edition the revert leaves with no contained works loses its (now dangling)
    # book-level translator set — the analog of the old per-work translator cascade.
    for eid in editions:
        if not acc.works.reads.ids_in_edition(eid):
            cs.set_edition_translators(db, eid, [])

    # Delete an affected person only if nothing references it any more — protects
    # persons shared with other (still-promoted) works. Never GC a VERIFIED/bound person:
    # a promotion only ever mints UNVERIFIED throwaways, so a verified row carrying one of
    # these (stored, possibly recycled) ids is a deliberately-curated entity that happened
    # to inherit the freed id — deleting it would be collateral damage (id-reuse guard).
    for pid in affected:
        if cs.person_referenced(db, pid):
            continue
        prows = acc.journal.capture("person", "id", [pid])   # raw row (tombstone counts)
        prow_p = prows[0] if prows else None
        if prow_p and (prow_p["external_id"] or prow_p["verification_status"] == "verified"):
            continue                       # curated/bound — not this promotion's throwaway
        acc.journal.clear("person", "id", [pid])
        res.created_person_ids.append(pid)

    acc.review.writes.delete_promotion(review_item_id=review_item_id)
    acc.review.writes.reopen(review_item_id)
    if commit:
        db.commit()
    return res


# ── Bulk over a segment ───────────────────────────────────────────────────────
def _proposals(db, status: str):
    for rid, pj in _acc(db).review.reads.items_by_type_status("book_toc_pattern", status):
        yield rid, json.loads(pj)


def segment_counts(db) -> dict:
    """{segment: {pending, promoted}} for the review tabs."""
    out = {s: {"pending": 0, "promoted": 0} for s in SEGMENTS}
    for status in ("pending", "promoted"):
        for _rid, payload in _proposals(db, status):
            out[bucket(payload)][status] += 1
    return out


def promote_segment(db, segment: str, *, verify: bool = False,
                    offline: bool = False) -> dict:
    """Promote every PENDING proposal in `segment`. Returns a summary; commits
    once at the end so the batch is atomic-ish per call. When `verify=True`, runs
    ingest-time authority matching over the persons/works the batch created in ONE
    pass after the commit (a shared verifier chain, warm caches) — see
    verify.verify_promotion; `offline` keeps that pass cache-only."""
    if segment not in SEGMENTS:
        raise ValueError(f"unknown segment {segment!r}")
    summary = {"segment": segment, "promoted": 0, "skipped": 0,
               "no_edition": 0, "ids": []}
    created_pids: list = []
    created_wids: list = []
    for rid, payload in list(_proposals(db, "pending")):
        if bucket(payload) != segment:
            continue
        r = promote_proposal(db, rid, commit=False)
        if r.status == "promoted":
            summary["promoted"] += 1
            summary["ids"].append(rid)
            created_pids.extend(r.created_person_ids)
            created_wids.extend(r.work_ids)
        elif r.status == "no_edition":
            summary["no_edition"] += 1
        else:
            summary["skipped"] += 1
    db.commit()
    if verify and (created_pids or created_wids):
        from . import verify as V                      # deferred: keep promote/verify decoupled
        batch = PromotionResult(0, work_ids=created_wids, created_person_ids=created_pids)
        summary["verified"] = V.verify_promotion(db, batch, offline=offline)
    return summary


def revert_segment(db, segment: str) -> dict:
    """Revert every PROMOTED proposal in `segment`."""
    if segment not in SEGMENTS:
        raise ValueError(f"unknown segment {segment!r}")
    summary = {"segment": segment, "reverted": 0, "ids": []}
    for rid, payload in list(_proposals(db, "promoted")):
        if bucket(payload) != segment:
            continue
        r = revert_proposal(db, rid, commit=False)
        if r.status == "reverted":
            summary["reverted"] += 1
            summary["ids"].append(rid)
    db.commit()
    return summary
