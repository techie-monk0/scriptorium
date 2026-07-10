"""Thin read-only replica export — the device-local lookup+open dataset.

One row per EDITION (what the user calls "a book"), denormalized to exactly what an
OFFLINE client needs: identify it (title / authors / subjects / isbns + a folded
`search_text` blob) and open it (per-holding `StorageRef`). Drops the entire
FRBR / authority / review graph.

Two invariants:
  • READ-ONLY — never writes the catalogue (safe to run anytime, no snapshot needed).
  • PROVIDER-NEUTRAL — file references come from a `StoragePort`, so the client never
    learns it's kDrive; it just opens an opaque `open_url` (or uses `relpath` natively).

Served to every client via `GET /api/v1/replica`: the PWA loads it into IndexedDB; a
native app maps it to its own store. `schema_version` is bumped on any breaking change.
"""
from __future__ import annotations

import os
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from . import library as _lib
from . import storage as _storage
from catalogue.db_store import default_db_path


def _acc(db):
    """A system Access over this connection — engine-routed edition/holding reads for the replica
    export (the `out.*` replica DB writes stay raw — a separate output artifact)."""
    from catalogue.access_api import system_conn
    return system_conn(db)

SCHEMA_VERSION = 5   # v5: per-edition tradition (the editable Buddhist-lineage field, shown on detail)

# Sentinel so callers can pass provider=None to mean "no provider" (vs. "use the default").
_DEFAULT = object()


def _fold(s: str) -> str:
    """Diacritic/case-folded, whitespace-collapsed text for client-side matching. The
    client mirrors this when it folds a query, so lookup is accent-insensitive."""
    return " ".join(unicodedata.normalize("NFKD", s or "").casefold().split())


def _edition_ids(db) -> list:
    return sorted(_acc(db).editions.reads.all_ids())


def _isbns(db, eid: int) -> list:
    """Every ISBN reachable from the edition: its own column, the `edition_isbn` aliases,
    and any on its holdings. Deduped, order-stable."""
    return _acc(db).editions.reads.all_isbns(eid)


def _subjects(db, eid: int) -> list:
    """TOPICAL subjects of the edition itself plus those of the works it contains.
    Series (kind='series') are a separate namespace and deliberately excluded, so
    the replica's `subjects` stays a clean set of topics for client-side facets."""
    return _acc(db).editions.reads.topic_subject_names(eid)


def _series(db, eid: int) -> list:
    """SERIES subject names of the edition (kind='series') — the namespace `_subjects`
    deliberately drops. Lets a client GROUP the home "Series" rail and (with `volume`)
    order each set, without a server-composed payload. Names only; deduped, order-stable."""
    seen, out = set(), []
    for name, kind in _acc(db).editions.reads.subject_names_kinds(eid):
        if kind == "series" and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _works(db, eid: int) -> list:
    """Contained works as {title (display), search (folded ALL aliases)}. The `search` blob lets a
    client's Work search match ANY alias spelling — not just the display title (which is one English
    alias, e.g. 'Entering into the Deeds of a Bodhisattva' for the Bodhicaryāvatāra) — while still
    showing the canonical title. `work_titles` stays the plain title list for display/search_text."""
    acc = _acc(db)
    out = []
    for wid in acc.works.reads.ids_in_edition(eid):
        title = _lib._alias_title(db, wid)
        aliases = [row[0] for row in acc.works.reads.aliases(wid)]
        out.append({"title": title, "search": _fold(" ".join([title] + aliases))})
    return out


def _alias_texts(db, eid: int) -> list:
    """EVERY alias spelling of the edition's contained works + contributor persons. Folded into
    search_text so a client's single-box lookup matches the same variant spellings the server's
    alias-aware search does — e.g. finding a book titled 'Bodhicharyāvatāra' by its Sanskrit work
    alias 'Bodhicaryāvatāra'. The display title is only ONE alias; this adds the rest. Deduped."""
    from .names import canonical_dalai_lama
    acc = _acc(db)
    seen, out = set(), []
    def _add(t):
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    for wid in acc.works.reads.ids_in_edition(eid):
        for row in acc.works.reads.aliases(wid):          # (text, scheme)
            _add(row[0])
    # Contributor persons: every alias spelling, PLUS the canonical office form for a recognised office
    # incumbent (e.g. 'Tenzin Gyatso' → 'Dalai Lama XIV'). This is the authority knowledge — it needs the
    # vocab + DB, so we bake it ONCE here; the client's shared `nameKey` then folds 14th/Fourteenth/XIV.
    authors, translators = acc.editions.reads.contributor_persons(eid)   # [(person_id, name)]
    for pid, name in list(authors) + list(translators):
        forms = [name] + [row[1] for row in acc.persons.reads.aliases(pid)]   # primary + (id,text,scheme)
        for f in forms:
            _add(f)
            dl = canonical_dalai_lama(f)
            if dl:
                _add(dl)
    return out


def _connections(db, eid: int) -> list:
    """Other editions of the works THIS edition contains (FRBR siblings) — the "Connections" the web
    shows via /edition/<eid>/links, reduced to what a client can navigate offline: a link per other
    edition. Deduped, self excluded."""
    acc = _acc(db)
    seen, out = set(), []
    for wid in acc.works.reads.ids_in_edition(eid):
        for row in acc.works.reads.editions_of(wid):     # (edition_id, title, seq, section_locator)
            oeid, otitle = row[0], row[1]
            if oeid != eid and oeid not in seen:
                seen.add(oeid)
                out.append({"eid": oeid, "title": otitle or "(untitled)"})
    return out


def _work_author_names(db, eid: int) -> list:
    """Authors of the contained works — folded into search_text so an author query finds
    the book even when the author isn't on the edition's own by-line."""
    return _acc(db).editions.reads.contained_work_author_names(eid)


def _holdings(db, eid: int, provider) -> list:
    """The openable copies of this edition, each with a provider-neutral `StorageRef`
    (None when there's no file or no provider covers it → client streams from server)."""
    out: list = []
    for hid, fp, apath, form in _acc(db).holdings.reads.openable(eid):
        path = fp or apath
        ref = None
        if path and provider is not None:
            abspath = os.path.abspath(path)
            if provider.covers(abspath):
                ref = provider.locator(abspath)
        out.append({
            "holding_id": hid,
            "format": (form or _lib._file_ext(path)),   # form_type code, for display
            "kind": _lib._file_ext(path),                # pdf/epub — reader dispatch key
            "has_file": bool(path),
            "storage": ref.as_dict() if ref else None,
        })
    return out


def edition_row(db, eid: int, provider=None) -> Optional[dict]:
    """One replica row for edition `eid`, or None if it vanished mid-export."""
    e = _acc(db).editions.reads.get(eid)
    if not e:
        return None
    title, subtitle, publisher, year = e.title, e.subtitle, e.publisher, e.year
    volume = _acc(db).editions.reads.volumes([eid]).get(eid)
    ppl = _lib.edition_persons(db, eid)
    authors = [a["name"] for a in ppl["authors"]]
    translators = [t["name"] for t in ppl["translators"]]
    isbns = _isbns(db, eid)
    subjects = _subjects(db, eid)
    series = _series(db, eid)
    works = _works(db, eid)
    work_titles = [w["title"] for w in works]
    search_text = _fold(" ".join(
        [p for p in (title, subtitle, volume, publisher) if p]
        + authors + translators + isbns + subjects + work_titles
        + _work_author_names(db, eid) + _alias_texts(db, eid)))
    return {
        "edition_id": eid,
        "title": title or "(untitled)",
        # Volume-aware title for clients to display as-is (one rule for web/PWA/native).
        "display_title": _lib.display_title(title, volume),
        "subtitle": subtitle,
        "volume": volume,
        "publisher": publisher,
        "year": year,
        # The edition's Buddhist tradition (a `tradition.name`, or None) — the editable
        # lineage field shown on the book-detail across web/PWA/native. Edition-level so
        # anthologies/mixed volumes can override their works' lineage.
        "tradition": e.tradition,
        # When the book entered the catalogue (earliest holding) — the client's
        # "Recently added" rail key. ISO string or None (no holding yet).
        "date_added": _acc(db).holdings.reads.earliest_added(eid),
        # Opaque art handles — fetched as-is (always answer: real art or SVG fallback),
        # never constructed by the client. Same URLs the web shelves + a native client use.
        "cover_url": f"/edition/{eid}/cover.jpg",
        "spine_url": f"/edition/{eid}/spine.svg",
        "authors": authors,
        "translators": translators,
        "isbns": isbns,
        "subjects": subjects,
        "series": series,
        "work_titles": work_titles,
        # Contained works WITH their folded all-alias search blob, so Work search matches any spelling.
        "works": works,
        # FRBR cross-links the client's BookDetailsPane "Connections" section can navigate (other
        # editions of the works this edition contains). The web's full Connections also lists author
        # links; the replica stays name-only there (no person pages on the offline clients).
        "connections": _connections(db, eid),
        "holdings": _holdings(db, eid, provider),
        "search_text": search_text,
    }


def build_replica(db, *, provider=_DEFAULT, exported_at: Optional[str] = None) -> dict:
    """The full replica document. `provider` defaults to `storage.default_provider()`;
    pass an explicit provider (or None for "no provider") for tests/alternate backends.
    `exported_at` is injectable for deterministic tests; else stamped UTC now."""
    if provider is _DEFAULT:
        provider = _storage.default_provider()
    from . import subject_tree as _T
    rows = [r for r in (edition_row(db, eid, provider) for eid in _edition_ids(db)) if r]
    stamp = exported_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": stamp,
        "provider": provider.name if provider is not None else None,
        "count": len(rows),
        "editions": rows,
        # The topic hierarchy (same shape as /api/v1/subjects) so the client can build the
        # home SUBJECT rails itself — top-level ids for navigation, `is_protected` to sink
        # the safety-net shelf — without a server-composed payload. Subjects ≪ editions.
        "subject_forest": _T.subject_forest(db, kind="topic"),
    }


def main(argv=None) -> int:
    """CLI: `python -m catalogue.services.export_replica [db] [-o out.json]`. Read-only."""
    import argparse
    import json
    import sys
    from catalogue.db_store import connect

    ap = argparse.ArgumentParser(description="Export the thin device-local replica (read-only).")
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("-o", "--out", help="write JSON here (default: stdout)")
    args = ap.parse_args(argv)

    db = connect(args.db)
    try:
        doc = build_replica(db)
    finally:
        db.close()
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {doc['count']} editions → {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
