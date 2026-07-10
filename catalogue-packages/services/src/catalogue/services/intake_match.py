"""Cross-format "is this book already in the catalogue?" verdict for phone intake.

A phone scan yields ONE ISBN, but print/epub/pdf printings of the same work carry
DIFFERENT ISBNs — so an exact `edition.isbn = ?` check misses the common case ("I
scanned the paperback; I already have the ebook"). `catalogue_verdict` answers the
real question with a layered match, short-circuiting on the first hit:

  1. exact ISBN          — the same printing is already catalogued
  2. OpenLibrary work key — clusters editions of one work across formats
  3. title + author/publisher — metadata fallback when no work key is available

Layer 3 matches on TITLE CONTAINMENT (the catalogue's full subtitled title contains
the short OpenLibrary title, or vice versa) and CORROBORATES with author and publisher
so a same-title-different-book collision is rejected. Layer 2 needs each edition's
`ol_work_key` populated — new editions get keyed at capture/ingest time
(`ensure_ol_work_key`) and `backfill_work_keys` keys the back-catalogue.

The work-key fetch is injected (best-effort, never raises) so the capture endpoint
can call this without risking the scan, and tests stay offline.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from catalogue.db_store import derive_holding_type, fold_key
from catalogue.services.isbn import normalize_isbn


def _acc(conn):
    """A system Access over this connection (engine-routed edition/holding reads + the
    ol_work_key writes)."""
    from catalogue.access_api import system_conn
    return system_conn(conn)

WorkKeyFetch = Callable[[str], Optional[str]]   # isbn → '/works/OL…W' | None
IsbnLookup = Callable[[str], Optional[dict]]    # isbn → {title, authors[], publishers[]}


# ── Small text helpers (folded, diacritic-insensitive) ───────────────────────
def _tokens(s: Optional[str], minlen: int = 1) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", fold_key(s or "")) if len(t) >= minlen}


def _title_contains(a: str, b: str) -> bool:
    """True if one title's words are a subset of the other's — handles the
    'short OL title vs long catalogue title-with-subtitle' case both ways."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta


def _overlap(a_strings, b_strings, minlen: int = 3) -> bool:
    """Do two sets of strings share a meaningful (len ≥ minlen) folded token?
    Used to corroborate author/publisher across the OL record and our edition."""
    a = {t for s in a_strings for t in _tokens(s, minlen)}
    b = {t for s in b_strings for t in _tokens(s, minlen)}
    return bool(a & b)


# ── Per-edition facets ───────────────────────────────────────────────────────
def forms_for_edition(db, eid: int) -> list:
    """Distinct holding formats (pdf|epub|physical) backing an edition — the
    'which form do I already have it in' the operator wants. Falls back to
    `derive_holding_type` for holdings whose facet was never populated."""
    forms: list = []
    for ht, form, fp, arch in _acc(db).holdings.reads.format_rows(eid):
        f = ht or derive_holding_type(form, fp, arch)
        if f and f not in forms:
            forms.append(f)
    return forms


def _edition_dict(db, eid: int, title: str) -> dict:
    return {"id": eid, "title": title or f"edition #{eid}",
            "forms": forms_for_edition(db, eid)}


# ── The verdict ──────────────────────────────────────────────────────────────
def catalogue_verdict(db, isbn: str, *, ol_work_key_fetch: Optional[WorkKeyFetch] = None,
                      isbn_lookup: Optional[IsbnLookup] = None) -> dict:
    """Return whether `isbn` is already in the catalogue in ANY format.

    Shape:
      {"in_catalogue": bool,
       "matched_by": "isbn" | "work_key" | "title" | None,
       "work_key": str | None,                 # resolved OL work key, if any
       "editions": [{"id", "title", "forms":[…]}]}

    Best-effort: any failure in an optional network layer is swallowed and the
    layer skipped — the function never raises and always returns a valid verdict.
    """
    isbn = normalize_isbn(isbn or "")
    from catalogue.services import reconcile

    # Layer 1 — exact ISBN (pure local DB, instant). One book's ISBN can live in
    # three places: on the edition (display/primary), on a holding (the canonical
    # per-manifestation home — print/epub/pdf carry DIFFERENT ISBNs), or in
    # edition_isbn (an explicit alternate/variant-printing link). Match ALL three
    # so a scan of any known ISBN of a held edition is recognized.
    if isbn:
        hits: dict = {}
        for c in reconcile.find_candidate_editions(db, isbn=isbn):
            if "isbn" in c.get("why", []):
                hits.setdefault(c["edition_id"], c["title"])
        for eid, title in _acc(db).editions.reads.by_holding_isbn(isbn):
            hits.setdefault(eid, title)
        for eid, title in _acc(db).editions.reads.by_edition_isbn(isbn):
            hits.setdefault(eid, title)
        if hits:
            return {"in_catalogue": True, "matched_by": "isbn", "work_key": None,
                    "editions": [_edition_dict(db, eid, t) for eid, t in hits.items()]}

    # Layer 2 — OpenLibrary work-key clustering (catches cross-format duplicates).
    work_key: Optional[str] = None
    if ol_work_key_fetch is not None:
        try:
            work_key = ol_work_key_fetch(isbn)
        except Exception:
            work_key = None
    if work_key:
        rows = _acc(db).editions.reads.by_ol_work_key(work_key)
        if rows:
            return {"in_catalogue": True, "matched_by": "work_key", "work_key": work_key,
                    "editions": [_edition_dict(db, eid, t) for (eid, t) in rows]}

    # Layer 3 — title containment, classified by author/translator/publisher
    # corroboration into CONFIRMED vs UNCERTAIN. A partial match is never silently
    # dropped: it is surfaced as `uncertain` for the operator to accept/reject.
    meta = None
    if isbn_lookup is not None:
        try:
            meta = isbn_lookup(isbn)
        except Exception:
            meta = None
    confirmed, uncertain = _title_matches(db, meta) if meta else ([], [])
    if confirmed:
        return {"in_catalogue": True, "matched_by": "title", "work_key": work_key,
                "editions": [_edition_dict(db, eid, t) for (eid, t) in confirmed],
                "uncertain": []}
    if uncertain:
        return {"in_catalogue": False, "matched_by": "title", "work_key": work_key,
                "editions": [], "uncertain": uncertain}

    return {"in_catalogue": False, "matched_by": None, "work_key": work_key,
            "editions": [], "uncertain": []}


def cip_verdict(db, record, *, ol_work_key_fetch: Optional[WorkKeyFetch] = None,
                isbn_lookup: Optional[IsbnLookup] = None) -> dict:
    """Cross-format "already in catalogue?" verdict for a parsed CIP record
    (copyright-page intake, §14.9). Same dict shape as `catalogue_verdict`, plus an
    `isbn` key naming the ISBN that produced an ISBN-layer hit (or None). Two layers,
    short-circuiting on the first hit:

      1. ISBN — try every checksum-valid ISBN the CIP block carried; the first that
         is already catalogued wins (exact, or — with a fetch — a work-key cluster).
      2. title + author/publisher — built from the CIP's OWN fields (no network),
         reusing `_title_matches`. Recall-biased: a partial match is surfaced as
         `uncertain` for the operator, never a silent not-found.

    `record` is duck-typed (a `cip.CipRecord`): reads `.isbns`, `.title`,
    `.authors`, `.publisher`. Best-effort like `catalogue_verdict` — never raises.
    """
    isbns = [i for i in (getattr(record, "isbns", None) or []) if i]
    for isbn in isbns:
        v = catalogue_verdict(db, isbn, ol_work_key_fetch=ol_work_key_fetch,
                              isbn_lookup=isbn_lookup)
        if v.get("in_catalogue"):
            v.setdefault("uncertain", [])
            v["isbn"] = isbn
            return v

    # No ISBN match — fall back to the CIP's own title/author/publisher (local only).
    title = getattr(record, "title", None)
    if title:
        meta = {"title": title,
                "authors": list(getattr(record, "authors", None) or []),
                "publishers": [p for p in [getattr(record, "publisher", None)] if p]}
        confirmed, uncertain = _title_matches(db, meta)
        if confirmed:
            return {"in_catalogue": True, "matched_by": "title", "work_key": None,
                    "isbn": None, "uncertain": [],
                    "editions": [_edition_dict(db, eid, t) for (eid, t) in confirmed]}
        if uncertain:
            return {"in_catalogue": False, "matched_by": "title", "work_key": None,
                    "isbn": None, "editions": [], "uncertain": uncertain}

    return {"in_catalogue": False, "matched_by": None, "work_key": None,
            "isbn": (isbns[0] if isbns else None), "editions": [], "uncertain": []}


def _name_matches(a: str, b: str) -> bool:
    """Two contributor names share a meaningful (len ≥ 3) folded token."""
    return bool(_tokens(a, 3) & _tokens(b, 3))


def _title_matches(db, meta: dict) -> tuple:
    """Split title-containment candidates into (confirmed, uncertain).

    confirmed → every OpenLibrary author maps to one of our contributors (full
                author agreement): a safe cross-format duplicate.
    uncertain → a PARTIAL match the operator should judge: some authors match and
                some don't, or no author data either side (title-only), or authors
                disagree but the publisher matches. Each carries a detail dict of
                exactly what matched and what didn't (authors, translators, publisher).
    A clear conflict (authors named on both sides with NO overlap and no publisher
    agreement) is dropped — that is a different book, not a partial match.
    """
    from catalogue.services.edition_verify import _edition_contributors

    ol_title = (meta or {}).get("title")
    if not ol_title:
        return [], []
    ol_authors = [a for a in (meta.get("authors") or []) if a]
    ol_pubs = [p for p in (meta.get("publishers") or []) if p]

    confirmed, uncertain = [], []
    for eid, title, publisher in _acc(db).editions.reads.titled():
        if not _title_contains(ol_title, title):
            continue
        cat_authors, cat_translators = _edition_contributors(db, eid)
        contributors = [(n, "author") for n in cat_authors] + \
                       [(n, "translator") for n in cat_translators]

        # Which OL authors found a counterpart among our authors+translators?
        ol_matched = [a for a in ol_authors
                      if any(_name_matches(a, n) for n, _ in contributors)]
        ol_unmatched = [a for a in ol_authors if a not in ol_matched]
        our_matched = [(n, r) for n, r in contributors
                       if any(_name_matches(a, n) for a in ol_authors)]
        our_unmatched = [(n, r) for n, r in contributors if (n, r) not in our_matched]

        pub_match = None
        if ol_pubs and publisher:
            pub_match = _overlap(ol_pubs, [publisher], minlen=4)

        full_author_agreement = bool(ol_authors) and not ol_unmatched
        if full_author_agreement:
            confirmed.append((eid, title))
            continue
        # Clear conflict → not a partial match → drop.
        if ol_authors and contributors and not ol_matched and pub_match is False:
            continue
        # Everything else is a partial/ambiguous match the operator should judge.
        uncertain.append({
            "id": eid, "title": title, "forms": forms_for_edition(db, eid),
            "authors_matched": [n for n, _ in our_matched],
            "authors_unmatched_ours": [f"{n} ({r})" for n, r in our_unmatched],
            "authors_unmatched_lookup": ol_unmatched,
            "publisher_ours": publisher,
            "publisher_lookup": ", ".join(ol_pubs) or None,
            "publisher_match": pub_match,
        })
    return confirmed, uncertain


def suspected_editions(db, meta: Optional[dict] = None) -> list:
    """Weak "might be the SAME book" matches the operator should CONFIRM — a similar title with at
    least one shared author, but NOT a confident match (so not auto-acquired). This is the
    different-ISBN / fuzzy-metadata case: a print edition in the catalogue vs. an ebook on the
    wishlist won't match by ISBN, but title + a shared author makes it a credible suspect to ask
    about. Local-only (reuses `_title_matches`). Returns edition dicts {id, title, forms}; [] when
    there's no title or no plausible suspect."""
    if not (meta and meta.get("title")):
        return []
    _, uncertain = _title_matches(db, meta)
    return [{"id": u["id"], "title": u["title"], "forms": u.get("forms", [])}
            for u in uncertain if u.get("authors_matched")]


def editions_now_holding(db, *, isbn: Optional[str] = None,
                         meta: Optional[dict] = None) -> list:
    """Local-only (no network) check of whether the catalogue NOW holds the book a
    phone scan referred to — used to move a once-"not in catalogue" scan into the
    capture log's "Added" section. Returns the matched edition dicts (id, title,
    forms); empty when still not held.

    Combines two of the cross-edition signals the verdict uses, both run against
    the LOCAL DB so it is cheap enough to call per row on page load:
      - exact ISBN across edition / holding / edition_isbn (Layer 1) — a later
        printing recorded under the same ISBN; and
      - title containment corroborated by a SHARED author (Layer 3), accepting a
        partial author match. Different printings of one title carry different
        ISBNs that public sources often don't link, so title+author is the
        reliable cross-edition key here; a CIP block frequently lists the book's
        SUBJECT as an extra "author", so requiring full author agreement (as the
        at-capture verdict does) would miss real holdings — one shared author with
        no outright conflict is enough to call it the same book.
    """
    out: dict = {}
    if isbn:
        v = catalogue_verdict(db, isbn)          # local Layer-1 ISBN only (no fetchers)
        if v["in_catalogue"]:
            for e in v["editions"]:
                out[e["id"]] = e
    if meta and meta.get("title"):
        confirmed, uncertain = _title_matches(db, meta)
        for eid, title in confirmed:
            out.setdefault(eid, _edition_dict(db, eid, title))
        for u in uncertain:                      # partial match: keep iff an author matched
            if u.get("authors_matched"):
                out.setdefault(u["id"], {"id": u["id"], "title": u["title"],
                                         "forms": u.get("forms", [])})
    return list(out.values())


def resolve_candidates(db, *, isbn: Optional[str] = None,
                       meta: Optional[dict] = None) -> list:
    """Editions a captured scan might DUPLICATE, for the operator to CONFIRM before
    resolve creates anything. Union of two local (no-network) signals:
      • exact ISBN / work-key holders (Layer-1) — a same-book hit we're sure of; and
      • title-containment suspects — both `confirmed` (full author agreement) AND the
        `uncertain` partials (e.g. a printing whose contributors we haven't recorded
        yet, matched on title + publisher).
    Unlike `editions_now_holding` — which auto-moves a scan and so demands an author
    overlap — this KEEPS the author-less / publisher-only title suspects, because the
    resolve step puts them in front of a human to judge rather than acting silently.
    (That is exactly the case a different-ISBN duplicate with unrecorded authors falls
    into.) Returns edition dicts {id, title, forms, certain}; [] when nothing plausible."""
    out: dict = {}
    isbn = normalize_isbn(isbn or "")
    if isbn:
        v = catalogue_verdict(db, isbn)          # local ISBN layer only (no fetchers)
        for e in v.get("editions", []):
            out[e["id"]] = {**e, "certain": True}
    if meta and meta.get("title"):
        confirmed, uncertain = _title_matches(db, meta)
        for eid, title in confirmed:
            out.setdefault(eid, {"id": eid, "title": title,
                                 "forms": forms_for_edition(db, eid), "certain": True})
        for u in uncertain:
            out.setdefault(u["id"], {"id": u["id"], "title": u["title"],
                                     "forms": u.get("forms", []), "certain": False})
    return list(out.values())


# ── Work-key population (new editions + back-catalogue) ──────────────────────
def ensure_ol_work_key(conn, edition_id: int, *, fetch: Optional[WorkKeyFetch],
                       commit: bool = True) -> Optional[str]:
    """Resolve + store an edition's OL work key if it has an ISBN and none yet.
    Called when an edition is created/gains an ISBN (capture resolve, ingest) so
    cross-format matching works for it immediately. Best-effort: returns None and
    writes nothing on any miss/failure; `fetch=None` is a no-op (offline/tests)."""
    if fetch is None:
        return None
    row = _acc(conn).editions.reads.ol_work_key_state(edition_id)
    if row is None:
        return None
    isbn, existing = row
    if not (isbn or "").strip() or (existing or "").strip():
        return existing or None
    try:
        key = fetch(isbn)
    except Exception:
        return None
    if key:
        _acc(conn).editions.writes.set_ol_work_key(edition_id, key, only_if_empty=True)
        if commit:
            conn.commit()
    return key


def backfill_work_keys(conn, *, fetch: WorkKeyFetch, limit: Optional[int] = None,
                       dry_run: bool = False, on_resolved=None) -> dict:
    """Populate `ol_work_key` for every edition that has an ISBN but no key yet.
    Resumable (per-row commit); the backfill CLI and the sweep post-pass both use
    this. Best-effort per edition — a lookup miss leaves that row for a later run."""
    rows = _acc(conn).editions.reads.missing_work_key(limit)
    stats = {"candidates": len(rows), "resolved": 0, "missed": 0}
    for eid, isbn in rows:
        try:
            key = fetch(isbn)
        except Exception:
            key = None
        if not key:
            stats["missed"] += 1
            continue
        stats["resolved"] += 1
        if not dry_run:
            _acc(conn).editions.writes.set_ol_work_key(eid, key)
            conn.commit()   # per-row → resumable
        if on_resolved:
            on_resolved(eid, isbn, key)
    return stats
