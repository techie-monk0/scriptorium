"""Step 4: promote BDRC-verified CIP Wylie titles into canonical rows.

For an edition whose CIP uniform title converted to EWTS and was CONFIRMED against BDRC
(catalogue/wylie_resolve), materialise the canonical identity the catalogue lacked:
  • a `work` row carrying the Wylie `tibetan_title` + `canonical_system='bdrc'` /
    `canonical_number=<bdrc id>` (the id doubles as the dedup key — editions resolving to
    the same BDRC id collapse onto ONE work, e.g. the several dgongs-pa-rab-gsal copies);
  • `work_alias` rows: wylie (our EWTS + BDRC's own label) + english (the edition title);
  • a `person` (the author) with a wylie alias + dates, linked author `work_contributor`;
  • the `edition_work` link.

SAFE BY DESIGN:
  • dry-run unless `apply=True` (the dry run executes against real DB state inside a
    SAVEPOINT and rolls back, so "created vs already" is accurate);
  • idempotent — re-running finds the work by BDRC id, checks each alias/link before
    inserting, so nothing duplicates;
  • by default promotes ONLY author-confirmed verdicts (`require_author=True`) — a
    title-only match is homonym-risky (this whole module exists because Wylie titles
    collide across authors) and is reported for review, not written.

Reuses the existing promotion rails (get_or_create_person, add_alias).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional

from catalogue.services import cip as cip_mod
from catalogue.db_store import add_alias, connect, fold_key, nfc
from catalogue.services.edition_resolve import _full_text
from catalogue.services.promote import get_or_create_person
from catalogue.services.translit import to_ewts
from catalogue.services.wylie_resolve import WorkSearchFn, verify_from_cip


@dataclass
class PromoteResult:
    edition_id: int
    status: str = "skipped"            # promoted | already | skipped | review
    work_id: Optional[int] = None
    person_id: Optional[int] = None
    actions: List[str] = field(default_factory=list)
    note: str = ""


# A CIP-derived EWTS is stored as a secondary alias only when CLEAN — OCR that
# destroyed a diacritic (⁄es, koº, digits-for-letters) must not become canonical text;
# the BDRC authority label is used for the canonical form instead.
_CLEAN_EWTS = re.compile(r"^[a-z '/.\-]+$")


def _is_clean_ewts(s: Optional[str]) -> bool:
    return bool(s) and bool(_CLEAN_EWTS.match(s))


def _shad(s: Optional[str]) -> str:
    """Strip the trailing Tibetan shad ('…gsal/') + whitespace from a BDRC label."""
    return (s or "").strip().rstrip("/").strip()


def _find_work_by_bdrc(db, bdrc_id: str) -> Optional[int]:
    from catalogue.access_api import system_conn
    return system_conn(db).works.reads.find_by_canonical("bdrc", bdrc_id)


def _existing_edition_work(db, edition_id: int) -> Optional[int]:
    from catalogue.access_api import system_conn
    rows = system_conn(db).works.reads.edition_work_rows(edition_id)
    return rows[0][0] if rows else None


def _ensure_alias(db, kind: str, pid: int, text: str, scheme: str,
                  actions: List[str]) -> None:
    from catalogue.access_api import system_conn
    text = (text or "").strip()
    if not text:
        return
    reads = (system_conn(db).persons.reads if kind == "person"
             else system_conn(db).works.reads)
    if not reads.has_alias_scheme_key(pid, scheme, fold_key(text)):
        add_alias(db, kind, pid, text, scheme)
        actions.append(f"+{kind}_alias[{scheme}] {text!r}")


def promote_verified(db, edition_id: int, *, bdrc_id: str, ewts_title: str,
                     bdrc_title_label: Optional[str], english_title: Optional[str],
                     ewts_author: Optional[str], author_display: Optional[str],
                     bdrc_author_label: Optional[str] = None,
                     dates: Optional[str] = None, apply: bool = False) -> PromoteResult:
    """Idempotently write canonical rows for ONE verified edition. Executes inside a
    SAVEPOINT; commits only when apply=True, else rolls back (accurate dry-run).

    SAFE work resolution: enriches the edition's OWN existing work in place, or attaches
    an unlinked edition to the BDRC-canonical work, or creates one — but NEVER re-points
    an edition off a pre-existing work (that destructive merge is left to a reviewed
    step). When the edition's work and a BDRC-canonical work differ, it records a
    merge-candidate note. Canonical title/author text comes from the clean BDRC authority
    labels; the OCR-derived EWTS is added only as a secondary alias when clean."""
    res = PromoteResult(edition_id)
    authority_title = _shad(bdrc_title_label) or ewts_title
    authority_author = _shad(bdrc_author_label) or None
    db.execute("SAVEPOINT cip_promote")
    try:
        existing = _existing_edition_work(db, edition_id)
        by_bdrc = _find_work_by_bdrc(db, bdrc_id)
        if existing is not None:
            wid = existing                       # enrich in place — never re-point (safe)
            if by_bdrc is not None and by_bdrc != existing:
                res.note = f"merge-candidate: w{by_bdrc} shares {bdrc_id}"
        elif by_bdrc is not None:
            wid = by_bdrc                         # unlinked edition → attach to canonical
        else:
            from catalogue.access_api import system_conn
            wid = system_conn(db).works.writes.insert_work({
                "work_type": None, "original_language": "bo", "canonical_system": "bdrc",
                "canonical_number": bdrc_id, "tibetan_title": authority_title,
                "notes": f"CIP→BDRC verified ({bdrc_id})"})
            from catalogue.services import subjects as S
            S.ensure_categorized(db, "work", wid)   # never subject-less; flagged Uncategorized
            res.actions.append(f"+work #{wid} (Uncategorized — review later) "
                               f"tibetan_title={authority_title!r}")
        if wid != res.work_id:
            from catalogue.access_api import system_conn
            acc = system_conn(db)
            acc.works.writes.set_scalars(wid, {"canonical_system": "bdrc",
                                               "canonical_number": bdrc_id})
            acc.works.writes.fill_scalars(wid, {"tibetan_title": authority_title})
        res.work_id = wid

        _ensure_alias(db, "work", wid, authority_title, "wylie", res.actions)
        if _is_clean_ewts(ewts_title) and fold_key(ewts_title) != fold_key(authority_title):
            _ensure_alias(db, "work", wid, ewts_title, "wylie", res.actions)
        if english_title:
            _ensure_alias(db, "work", wid, english_title, "english", res.actions)

        # Author identity from the CLEAN BDRC label (consistent across editions → dedups
        # the author); fall back to the OCR'd ALA-LC heading only if BDRC gave none.
        display = authority_author or author_display
        if display:
            pid, created = get_or_create_person(db, display, "author")
            res.person_id = pid
            if created:
                res.actions.append(f"+person #{pid} {display!r}")
            from catalogue.access_api import system_conn
            acc = system_conn(db)
            person_dto = acc.persons.reads.get(pid)
            if dates and not (person_dto and person_dto.dates):
                acc.journal.update_row("person", {"dates": dates}, {"id": pid})
            if authority_author:
                _ensure_alias(db, "person", pid, authority_author, "wylie", res.actions)
            if _is_clean_ewts(ewts_author) and (
                    not authority_author
                    or fold_key(ewts_author) != fold_key(authority_author)):
                _ensure_alias(db, "person", pid, ewts_author, "wylie", res.actions)
            from catalogue.db_store import contributor_store as cs
            authored = any(p == pid and r == "author"
                           for p, r, _n in acc.works.reads.author_rows_named(wid))
            if not authored:
                cs.add_work_author(db, wid, pid)
                res.actions.append(f"+work_author #{pid}")

        from catalogue.access_api import system_conn
        acc = system_conn(db)
        if wid not in acc.works.reads.ids_in_edition(edition_id):
            seq = acc.editions.reads.next_work_sequence(edition_id)
            acc.works.writes.link_to_edition(edition_id, wid, seq, None)
            res.actions.append(f"+edition_work e{edition_id}→w{wid}")

        res.status = "promoted" if res.actions and any(
            a[0] == "+" for a in res.actions) else "already"
    finally:
        if apply:
            db.execute("RELEASE SAVEPOINT cip_promote")
        else:
            db.execute("ROLLBACK TO SAVEPOINT cip_promote")
            db.execute("RELEASE SAVEPOINT cip_promote")
    return res


def run(db_path: str, *, apply: bool = False, require_author: bool = True,
        limit: Optional[int] = None, search: Optional[WorkSearchFn] = None) -> dict:
    db = connect(db_path)
    from catalogue.access_api import system_conn
    rows = [(e.id, e.title) for e in system_conn(db).editions.reads.all()]
    if limit:
        rows = rows[:limit]
    t = {"editions": len(rows), "promoted": 0, "already": 0, "review": 0, "skipped": 0}
    print(f"CIP→canonical step 4 {'APPLY' if apply else 'DRY-RUN'} "
          f"(require_author={require_author})…", flush=True)

    for eid, etitle in rows:
        try:
            text = _full_text(db, eid) or ""
        except Exception:
            continue
        rec = cip_mod.parse_cip(text) if text.strip() else None
        if not rec or rec.uniform_script != "tibetan" or not rec.uniform_title:
            continue
        v = verify_from_cip(rec.uniform_title, script="tibetan",
                            author_alalc=rec.author_heading, search=search)
        author_confirmed = v.matched and "author confirmed" in v.reason
        if not v.matched or (require_author and not author_confirmed):
            if v.matched:
                t["review"] += 1
                print(f"  e{eid} REVIEW (title-only, no author anchor): "
                      f"{v.bdrc_id} {v.ewts_query!r}", flush=True)
            continue
        res = promote_verified(
            db, eid, bdrc_id=v.bdrc_id, ewts_title=v.ewts_query,
            bdrc_title_label=v.title_label, bdrc_author_label=v.author_label,
            english_title=etitle,
            ewts_author=to_ewts(rec.author_heading, ocr=True, names=True)
            if rec.author_heading else None,
            author_display=nfc(rec.author_heading) if rec.author_heading else None,
            dates=(rec.author_dates[0] if rec.author_dates else None), apply=apply)
        t[res.status] = t.get(res.status, 0) + 1
        if res.note:
            t["merge_candidates"] = t.get("merge_candidates", 0) + 1
        print(f"  e{eid} {res.status.upper()} w{res.work_id} ({v.bdrc_id}, "
              f"conf={v.confidence}): {'; '.join(res.actions) or '—'}"
              f"{'  [' + res.note + ']' if res.note else ''}", flush=True)
    if apply:
        db.commit()
    print(f"\nsummary: {t}", flush=True)
    return t


def main() -> None:
    ap = argparse.ArgumentParser(description="Step 4: promote BDRC-verified CIP Wylie "
                                             "titles to canonical work/person rows.")
    ap.add_argument("db")
    ap.add_argument("--apply", action="store_true", help="commit (default: dry-run)")
    ap.add_argument("--allow-title-only", dest="require_author", action="store_false",
                    help="also promote title-only matches (homonym-risky)")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    run(args.db, apply=args.apply, require_author=args.require_author, limit=args.limit)


if __name__ == "__main__":
    main()
