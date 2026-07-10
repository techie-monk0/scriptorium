"""Browse the catalogue by subject and by author / translator (web-only index).

One page, two parts:
  1. **Editions, segmented by top-level subject** — subject names are slash-paths
     ('Buddhism/Madhyamaka/…'); the top level is the first segment. Each edition (via
     its own tags + contained works) is bucketed under every top-level subject it
     carries; untagged editions fall under "(Uncategorized)". Within a bucket, A–Z by
     title. Clicking an edition opens its COVER PAGE ("Tap / click to read"), which in
     turn opens the reader.
  2. **By author / translator** — every contributor, A–Z by name; under each, the
     editions they appear on (A–Z → cover page) and the works they wrote (A–Z → work
     detail).
"""
from __future__ import annotations

from flask import abort, g, render_template

from catalogue.db_store import fold_key
from catalogue.services import library as library_mod
from catalogue.services import subject_tree as T
from catalogue.webui.routes._shared import _acc

_NO_SUBJECT = "(Uncategorized)"

# work_author roles that count as authorship for the multi-work fallback
# (mirrors works.py `_WORK_AUTHOR_ROLES`).
_AUTHOR_ROLES = ("author", "attributed", "compiler", "reviser")


def _edition_byline(acc, eid: int):
    """(authors, translators) name lists for an edition's Editions-row byline. Book-level
    authors (edition_author) + translators (edition_translator + per-work overrides); a
    multi-work container with no book-level author falls back to its contained-work authors."""
    people = library_mod.edition_persons(g.db, eid)
    authors = [a["name"] for a in people["authors"]]
    if not authors:                        # multi-work container → contained-work authors
        for pid in acc.editions.reads.contained_work_author_ids(eid, _AUTHOR_ROLES):
            person = acc.persons.reads.get(pid)
            if person:
                authors.append(person.primary_name)
    translators = [t["name"] for t in people["translators"]]
    return authors, translators


def _by_title(rows):
    """Sort display rows alphabetically by folded title (diacritic-/case-insensitive),
    id as the stable tiebreak."""
    return sorted(rows, key=lambda r: (fold_key(r["title"]), r["id"]))


def _edition_url(eid: int) -> str:
    """Click an edition → its cover page ('Tap / click to read'), which opens the reader."""
    return f"/edition/{eid}/coverpage"


def register(app, ctx):
    @app.get("/by-author")
    def browse_by_author():
        acc = _acc(g.db)

        # ── 1. Editions, segmented by TOP-LEVEL subject, A–Z by title ────────
        buckets: "dict[str, list]" = {}
        for e in acc.editions.reads.all():
            authors, translators = _edition_byline(acc, e.id)
            row = {"id": e.id, "title": e.title or f"Edition #{e.id}", "url": _edition_url(e.id),
                   "authors": authors, "translators": translators}
            tops = sorted({(T.top_level(n) or _NO_SUBJECT)
                           for n in acc.editions.reads.topic_subject_names(e.id)})
            for top in (tops or [_NO_SUBJECT]):
                buckets.setdefault(top, []).append(row)
        # Heading order: alphabetical, with the no-subject bucket last.
        subject_sections = [
            {"subject": name, "editions": _by_title(buckets[name])}
            for name in sorted(buckets, key=lambda n: (n == _NO_SUBJECT, fold_key(n)))
        ]

        # ── 2. Authors / translators (A–Z by name), each with editions + works ─
        people = []
        for p in acc.persons.reads.directory():
            p_editions = _by_title([
                {"id": eid, "title": title or f"Edition #{eid}", "roles": roles,
                 "url": _edition_url(eid)}
                for eid, title, _year, roles in acc.persons.reads.appearing_editions(p.id)
            ])
            p_works = _by_title([
                {"id": wid, "title": label or f"Work #{wid}", "role": role,
                 "url": f"/work/{wid}"}
                for wid, role, label in acc.persons.reads.contributed_works(p.id)
            ])
            if p_editions or p_works:
                people.append({"id": p.id, "name": p.primary_name,
                               "editions": p_editions, "works": p_works})

        return render_template("by_author.html",
                               subject_sections=subject_sections, people=people)

    @app.get("/edition/<int:eid>/coverpage")
    def edition_coverpage(eid):
        """Interstitial before the reader: just the edition's cover + a 'Tap / click to
        read' prompt. The cover links to /edition/<id>/read (the actual reader)."""
        e = _acc(g.db).editions.reads.get(eid)
        if not e:
            abort(404)
        return render_template("edition_coverpage.html", eid=eid,
                               title=e.title or "(untitled)")
