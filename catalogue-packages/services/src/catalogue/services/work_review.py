"""Review queue for works with INCOMPLETE data.

Frictionless work creation (typing a name in the root/commentary box, a quick authority
pick) deliberately lets a work into the catalogue with only a title — so there must be a
stage that surfaces every such work for completion. A work is incomplete when it lacks
any of: a subject keyword, an author, or a canonical identity (canonical# OR a native
Sanskrit/Tibetan title). The operator fixes it on the work page, then marks it reviewed
(`work.review_status = 'ok'`), which clears it from the queue. An explicit 'needs_fix'
keeps it in the queue regardless.
"""
from __future__ import annotations


def _acc(db):
    """A system Access over this connection (engine-routed work-review reads + writes)."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def work_reasons(db, wid: int) -> list:
    """Why work `wid` is considered incomplete — an empty list means it's complete.

    A work's root/commentary `work_type` is deliberately NOT required: it is a
    relational role that only applies to works in a root-commentary pair. A songs
    collection, a standalone sutra, a biography or a modern book has no such role,
    so an absent type is not incompleteness."""
    reads = _acc(db).works.reads
    fields = reads.review_fields(wid)
    if fields is None:
        return []
    reasons = []
    if not reads.has_subject(wid):
        reasons.append("no subject")
    if not reads.has_author(wid):
        reasons.append("no author")
    if not (fields["canonical_number"] or (fields["sanskrit_title"] or "").strip()
            or (fields["tibetan_title"] or "").strip()):
        reasons.append("no canonical# / native title")
    return reasons


def _title(db, wid: int) -> str:
    return _acc(db).works.reads.representative_title(wid) or f"work #{wid}"


def incomplete_works(db) -> list:
    """Every work needing review — unreviewed/needs_fix AND missing data (or explicitly
    needs_fix). Returns `[{id, title, reasons, status}]`, most-incomplete first."""
    out = []
    for wid, status in _acc(db).works.reads.incomplete_rows():
        if status == "ok":
            continue
        reasons = work_reasons(db, wid)
        if reasons or status == "needs_fix":
            out.append({"id": wid, "title": _title(db, wid),
                        "reasons": reasons, "status": status})
    out.sort(key=lambda w: (-len(w["reasons"]), (w["title"] or "").lower()))
    return out


def count_incomplete(db) -> int:
    """Fast count of works needing review (same rule as `incomplete_works`) — for the
    dashboard badge."""
    return _acc(db).works.reads.count_incomplete()


def review_detail(db, wid: int) -> dict:
    """Full review record for one work: its fields + the incompleteness reasons."""
    from catalogue.db_store import contributor_store as cs
    from catalogue.services import subjects as S
    acc = _acc(db)
    fields = acc.works.reads.review_fields(wid)
    if fields is None:
        return {}
    authors = []
    for pid in cs.work_author_ids(db, wid):
        p = acc.persons.reads.get(pid)
        authors.append({"id": pid, "name": (p.primary_name if p else f"person #{pid}")})
    aliases = acc.works.reads.aliases(wid)
    canon_sys, canon_num = fields["canonical_system"], fields["canonical_number"]
    return {
        "id": wid, "title": _title(db, wid), "reasons": work_reasons(db, wid),
        "canonical": f"{canon_sys}:{canon_num}" if canon_sys and canon_num else None,
        "sanskrit": fields["sanskrit_title"], "tibetan": fields["tibetan_title"],
        "work_type": fields["work_type"],
        "original_language": fields["original_language"], "status": fields["review_status"],
        "authors": authors, "subjects": [n for _i, n in S.subjects_for(db, "work", wid)],
        "aliases": aliases,
    }


def set_review(db, wid: int, status, *, commit: bool = True) -> None:
    """Set a work's review verdict ('ok' clears it from the queue; 'needs_fix' keeps it;
    None = unreviewed). A work still tagged `Uncategorized` cannot be marked 'ok' — a
    real subject must replace the placeholder first (raises `UncategorizedError`)."""
    from catalogue.services import subjects as S
    if status == "ok" and S.has_uncategorized(db, "work", wid):
        raise S.UncategorizedError(
            "This work is still tagged “Uncategorized”. Assign a real subject (and "
            "remove the Uncategorized tag) before marking it reviewed.")
    _acc(db).works.writes.set_review_status(wid, status)
    if commit:
        db.commit()
