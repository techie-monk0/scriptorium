"""Composable search pipeline (§4.5, §12.6).

Shape from day one: normalize → [expand] → match → rank.

The expansion step is a no-op pass-through in v1 (deferred
query-expansion-through-aliases, §4.5). The shape exists so the deferred
feature drops in by replacing one callable — never by restructuring callers.

Stages are plain callables so any can be swapped via config; the public
entry point is `SearchService.search()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from catalogue.db_store import search_normalize
from catalogue.db_store import default_db_path


def _acc(conn):
    """A system Access over this connection — engine-routed FTS + facet search reads (read-only)."""
    from catalogue.access_api import system_conn
    return system_conn(conn)


# ── Stage signatures ──────────────────────────────────────────────────────
# normalize:  str  → str          (shared normalizer; same fold the resolver uses)
# expand:     str  → list[str]    (query + any alias variants)
# match:      list[str], conn → list[Hit]
# rank:       list[Hit] → list[Hit]

@dataclass
class Hit:
    edition_id: int
    page: int | None
    snippet: str               # diacritics intact (§4.5)
    score: float = 0.0


# ── Default stage implementations ─────────────────────────────────────────
def normalize_query(q: str) -> str:
    """Search-time normalizer aligned with the FTS5 INDEX fold (§4.5).

    NOT the same as the §4.2 resolver `fold_key` — see `search_normalize`
    docstring for why. Conflating the two breaks `tathagatagarbha` /
    `tathāgatagarbha` matching, which is §4.5's headline invariant.
    """
    return search_normalize(q)


def expand_noop(q: str) -> list[str]:
    """v1 expansion: pass-through. The deferred feature (§4.5) replaces this
    with an alias-lookup expander; callers do not change."""
    return [q]


def _fts_quote(term: str) -> str:
    """Wrap one term as an FTS5 phrase literal so query syntax (AND, OR,
    NEAR, ", *, :, parens, hyphen) in user input can never reach the
    parser. FTS5 phrases are `"…"` with internal `"` escaped by doubling.
    """
    return '"' + term.replace('"', '""') + '"'


def match_fts(terms: Sequence[str], conn) -> list[Hit]:
    """FTS5 MATCH against `edition_text_fts`. Returns NFC-diacriticked
    snippets — folding is index-only (§4.5).

    Each term is quoted as an FTS5 phrase, then OR-joined. Without
    quoting, user input like `foo AND bar`, `"`, `*`, `(` either changes
    query semantics or raises `OperationalError` and 500s the request.

    `Hit.score` is `-bm25(…)` so HIGHER is more relevant — SQLite's
    raw `bm25()` returns values where SMALLER is better (often negative).
    Negating here means anything sorting `score` descending or rendering
    it to the user gets the natural orientation.
    """
    cleaned = [t for t in terms if t and t.strip()]
    if not cleaned:
        return []
    query = " OR ".join(_fts_quote(t) for t in cleaned)
    rows = _acc(conn).editions.reads.fts_search(query, 400)   # many passages/book; grouped downstream
    return [Hit(edition_id=r[0], page=r[1], snippet=r[2], score=-r[3]) for r in rows]


def rank_passthrough(hits: list[Hit]) -> list[Hit]:
    """v1 ranking: trust bm25's order (already sorted by match_fts)."""
    return hits


# ── Service ───────────────────────────────────────────────────────────────
@dataclass
class SearchService:
    """Pipeline holder. Each stage is replaceable (§12.6, §12.7)."""
    normalize: Callable[[str], str] = normalize_query
    expand:    Callable[[str], list[str]] = expand_noop
    match:     Callable[[Sequence[str], object], list[Hit]] = match_fts
    rank:      Callable[[list[Hit]], list[Hit]] = rank_passthrough

    def search(self, conn, q: str) -> list[Hit]:
        normalized = self.normalize(q)
        terms = self.expand(normalized)
        hits = self.match(terms, conn)
        return self.rank(hits)

    def search_grouped(self, conn, q: str, *, snippets_per_book: int = 5) -> list[dict]:
        """Full-text search GROUPED by edition — one card per book in best-hit order,
        each `{edition_id, title, authors, snippets}` carrying its top few matching
        passages. The single source of truth for the `/search` page AND the
        `/api/v1/content` JSON, so web + PWA + native render identical content results."""
        results, order = [], {}
        for h in self.search(conn, q):                  # bm25-ordered, best first
            grp = order.get(h.edition_id)
            if grp is None:
                ed = _acc(conn).editions.reads.get(h.edition_id)
                vol = _acc(conn).editions.reads.volumes([h.edition_id]).get(h.edition_id)
                from catalogue.services import library as _lib
                grp = {"edition_id": h.edition_id,
                       # volume-aware via the ONE shared rule (same as shelves/replica/web).
                       "title": _lib.display_title(
                           (ed.title if ed else None) or f"edition #{h.edition_id}", vol),
                       # full edition by-line (book authors + translators), not work authors.
                       "authors": edition_people(conn, h.edition_id),
                       "snippets": []}
                order[h.edition_id] = grp
                results.append(grp)                     # editions stay in best-hit order
            if len(grp["snippets"]) < snippets_per_book:
                grp["snippets"].append(h.snippet)
        return results


# ── Structured metadata search (CLI): book title / author / work title ────────────
# DISTINCT from the FTS content search above — this matches catalogue METADATA, not
# the book's text. Every comparison is on the diacritic + digraph fold (`fold_key`),
# the exact key already stored in work_alias/person_alias.normalized_key — so a query
# matches regardless of diacritics AND across EVERY alias of a work or person, not just
# its primary spelling. Each provided field is a substring (contains) match; multiple
# fields (and repeated --author) are ANDed (intersection), to narrow a result set.
import os  # noqa: E402
import re  # noqa: E402

from catalogue.db_store import fold_key  # noqa: E402
from .honorifics import is_ordinal_token, ordinal_value  # noqa: E402
from .names import canonical_dalai_lama  # noqa: E402

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _ord_fold(text: str):
    """(fold of `text` with ordinal tokens removed, ordinal value | None). Drops the
    ordinal so '14th' / 'Fourteenth' / 'XIV' compare equal regardless of where the
    ordinal sits in the string, and carries the number separately so '7th' and '14th'
    of one office don't collide. If EVERY token is an ordinal (e.g. a lone 'XIV', or a
    surname like 'Li' that reads as a roman numeral), don't strip — fall back to a plain
    fold so the query isn't reduced to nothing."""
    text = text or ""
    kept = [t for t in _WORD.findall(text) if not is_ordinal_token(t)]
    if not kept:
        return fold_key(text), None
    return fold_key(" ".join(kept)), ordinal_value(text)


def _name_matches(query: str, candidate: str) -> bool:
    """Ordinal-aware substring match: the query's non-ordinal fold is contained in the
    candidate's, and if BOTH name an ordinal they must be equal."""
    qf, qv = _ord_fold(query)
    cf, cv = _ord_fold(candidate)
    if qf and qf not in cf:
        return False
    return not (qv is not None and cv is not None and qv != cv)


def _person_matches(query: str, names) -> bool:
    """True if any of a person's names (primary + aliases) matches the query
    ordinal-awarely, OR both canonicalize to the same Dalai Lama office+incumbent (so
    the personal name 'Tenzin Gyatso' also matches 'Dalai Lama XIV')."""
    q_dl = canonical_dalai_lama(query)
    for nm in names:
        if nm and _name_matches(query, nm):
            return True
        if q_dl is not None and nm and canonical_dalai_lama(nm) == q_dl:
            return True
    return False


def _editions_by_book_title(db, needle: str) -> set:
    """Editions whose own title / subtitle / native title matches the folded needle.

    Multi-word queries match as an AND of TOKENS in any order: "discourses buddha"
    matches "Connected Discourses of the Buddha", "Long Discourses of the Buddha",
    "In the Buddha's Words: An Anthology of Discourses…", etc. Each whitespace-
    separated token must appear as a folded substring (across the title/subtitle/
    native-title fields together), but they need not be contiguous or in order. A
    single-word query is the old plain-substring behaviour. Edition titles have no
    stored fold column, so fold in Python — the corpus is small."""
    tokens = needle.split()
    if not tokens:
        return set()
    out = set()
    for eid, *titles in _acc(db).editions.reads.title_fields_all():
        folded = [fold_key(t) for t in titles if t]
        if all(any(tok in f for f in folded) for tok in tokens):
            out.add(eid)
    return out


def _editions_by_work_title(db, needle: str) -> set:
    """Editions linked to a work whose ANY alias (any scheme/script) folds to contain
    the needle — matched on the indexed normalized_key."""
    return _acc(db).editions.reads.ids_by_work_alias_key(needle)


def _editions_by_author(db, query: str) -> set:
    """Editions whose works have an AUTHOR-role contributor matching `query` (RAW, not
    pre-folded). Matching is ordinal- and office-aware (see `_person_matches`) over each
    person's primary_name + every alias, so 'Fourteenth Dalai Lama' / '14th Dalai Lama'
    / 'Dalai Lama XIV' / 'Tenzin Gyatso' all resolve to the same incumbent."""
    acc = _acc(db)
    names = acc.persons.reads.all_names()
    eids = set()
    for pid, nmlist in names.items():
        if _person_matches(query, nmlist):
            eids |= acc.editions.reads.ids_by_author_person(pid)
    return eids


def _editions_by_person(db, query: str) -> set:
    """Editions where `query` is ANY contributor — AUTHOR of a contained work
    (work_author) OR TRANSLATOR (edition_translator, or a per-work override on
    edition_work). The role-agnostic twin of `_editions_by_author`; same ordinal-/
    office-aware name match over each person's primary_name + every alias."""
    acc = _acc(db)
    names = acc.persons.reads.all_names()
    eids = set()
    for pid, nmlist in names.items():
        if _person_matches(query, nmlist):
            eids |= acc.editions.reads.ids_by_person(pid)
    return eids


def _editions_by_subject(db, name: str) -> set:
    """Editions filed under subject `name` — PREFIX-INCLUSIVE: asking for `Buddhism`
    returns its `Buddhism/Emptiness` books too (and any depth below). Matches a book
    tagged directly OR through a contained work, via the one shared inheritance rule
    (`subject_tree.editions_for_subject`). Unknown subject name → empty set."""
    from catalogue.services import subject_tree as T
    ids = T.descendant_ids_by_name(db, name)
    if not ids:
        return set()
    out = set()
    for sid in ids:                                   # union the rollup of each matched id
        out |= set(T.editions_for_subject(db, sid, include_descendants=False))
    return out


def find_books(db, *, book_title: str | None = None, authors=None,
               work_title: str | None = None, persons=None,
               subject: str | None = None) -> list[dict]:
    """Books (editions) matching ALL the given criteria, canonically. `authors`
    matches the author role only; `persons` matches ANY contributor role (the
    Browse "Person" field); `subject` matches the subject AND everything nested under
    it (prefix-inclusive). List fields are ANDed. Returns rows with the edition's
    title, authors, contained-work titles, files. Empty → []."""
    sets = []
    if book_title:
        sets.append(_editions_by_book_title(db, fold_key(book_title)))
    if work_title:
        sets.append(_editions_by_work_title(db, fold_key(work_title)))
    for a in (authors or []):
        if a:
            sets.append(_editions_by_author(db, a))   # raw — ordinal/office aware
    for p in (persons or []):
        if p:
            sets.append(_editions_by_person(db, p))   # any role
    if subject:
        sets.append(_editions_by_subject(db, subject))   # prefix-inclusive
    if not sets:
        return []
    return [_book_row(db, eid) for eid in sorted(set.intersection(*sets))]


def edition_people(db, eid: int) -> list:
    """Display by-line for an EDITION — its OWN people only: book-level authors
    (`edition_author`) and translators (`edition_translator` + per-book
    `edition_work.translator_person_id`). Authors of CONTAINED WORKS (`work_author`) are a
    work-level concern and are deliberately NOT inherited into the edition's by-line.
    Authors first, then translators marked '(tr.)'; deduped by fold, order-stable."""
    rows = _acc(db).editions.reads.edition_byline(eid)
    seen, authors, translators = set(), [], []
    for name, is_tr in rows:
        if not name:
            continue
        k = fold_key(name)
        if k in seen:                 # a person listed twice (e.g. author + translator): keep first
            continue
        seen.add(k)
        (translators if is_tr else authors).append(name)
    return authors + [f"{t} (tr.)" for t in translators]


def _book_row(db, eid: int) -> dict:
    acc = _acc(db)
    ed = acc.editions.reads.get(eid)
    title = ed.title if ed else None
    authors = edition_people(db, eid)   # the edition's own by-line (book authors + translators)
    works = [t for t in (acc.works.reads.representative_title(w)
                         for w in acc.works.reads.ids_in_edition(eid)) if t]
    files = [os.path.basename(h.file_path) for h in acc.holdings.reads.by_edition(eid)
             if h.file_path]
    return {"edition_id": eid, "title": title, "authors": authors,
            "works": works, "files": files}


# ── Unified search: a DYNAMIC type registry (§ dashboard redesign) ────────────
# One box → results grouped by entity type. The set of searchable types is DATA,
# not hardcoded branching: append a SearchType and it shows up automatically in
# the /find/suggest completions (with its singular `label` as the prefix), in the
# grouped /find page (under its `label_plural` heading), and in the filter chips.
# Adding e.g. a "Music" type tomorrow is one registry entry — nothing else changes.
#
# A type's `search(db, q, limit)` returns rows shaped {id, label, sublabel, url}:
#   label    — the headline (the work/person/edition title or subject name)
#   sublabel — a dim one-liner (authors, dates, counts, canonical id)
#   url      — where clicking the result goes

@dataclass
class SearchType:
    key: str                                   # stable id ('editions', 'works', …)
    label: str                                 # SINGULAR; the completion prefix ('Edition')
    label_plural: str                          # group heading ('Editions')
    search: Callable[[object, str, int], list] # (db, q, limit) -> [{id,label,sublabel,url}]


def _as_id(q):
    """If the whole query is an internal-id reference — a bare positive integer,
    optionally written '#42' — return that int, else None. Lets the unified search
    box jump straight to edition / work / person #N by its catalogue number. Each
    type checks the number against its OWN table, so '42' surfaces edition #42,
    work #42 and person #42 together (grouped) — the number always shown in the
    sublabel so it's discoverable, not just searchable."""
    s = (q or "").strip().lstrip("#").strip()
    return int(s) if s.isdigit() else None


def _edition_hit(r):
    eid = r["edition_id"]
    authors = ", ".join(r["authors"][:3])
    return {"id": eid, "label": r["title"] or "(untitled)",
            "sublabel": " · ".join(x for x in (f"#{eid}", authors) if x),
            "url": f"/library?eid={eid}"}


def _search_editions(db, q, limit):
    out, seen = [], set()
    eid_q = _as_id(q)
    if eid_q is not None and _acc(db).editions.reads.get(eid_q) is not None:
        out.append(_edition_hit(_book_row(db, eid_q))); seen.add(eid_q)
    for r in find_books(db, book_title=q):
        if r["edition_id"] in seen:
            continue
        out.append(_edition_hit(r))
        if len(out) >= limit:
            break
    return out[:limit]


def _work_hit(wid, csys, cnum, title):
    canon = f"{csys} {cnum}".strip() if (csys or cnum) else ""
    return {"id": wid, "label": title or f"work#{wid}",
            "sublabel": " · ".join(x for x in (f"#{wid}", canon) if x),
            "url": f"/work/{wid}"}


def _search_works(db, q, limit):
    out, seen = [], set()
    acc = _acc(db)
    wid_q = _as_id(q)
    if wid_q is not None:
        r = acc.works.reads.hit_by_id(wid_q)
        if r:
            out.append(_work_hit(*r)); seen.add(r[0])
    rows = acc.works.reads.search_hits(fold_key(q), limit)
    for row in rows:
        if row[0] in seen:
            continue
        out.append(_work_hit(*row))
    return out[:limit]


def _person_hit(pid, name, dates, ext):
    return {"id": pid, "label": name,
            "sublabel": " · ".join(x for x in (f"#{pid}", dates, ext) if x),
            "url": f"/person/{pid}"}


def _search_people(db, q, limit):
    out, seen = [], set()
    acc = _acc(db)
    pid_q = _as_id(q)
    if pid_q is not None:
        p = acc.persons.reads.get(pid_q)
        if p:
            out.append(_person_hit(p.id, p.primary_name, p.dates or "", p.external_id or ""))
            seen.add(p.id)
    rows = acc.persons.reads.search(q, limit=limit)
    for row in rows:
        if row[0] in seen:
            continue
        out.append(_person_hit(*row))
    return out[:limit]


def _search_subjects(db, q, limit):
    from . import subjects as S
    out = []
    for s in S.subjects_matching(db, q, limit=limit):
        series = s.get("kind") == "series"
        out.append({
            "id": s["id"],
            "label": s["name"] + (" (series)" if series else ""),
            "sublabel": f"{s['n_works']}w · {s['n_editions']}e",
            # Canonical subject target: the descendant-inclusive browse page.
            "url": f"/subject/{s['id']}"})
    return out


def _search_authorities(db, q, limit):
    from .work_canonical_resolver import EightyFourThousandIndex, parse_lang_prefix
    lang, term = parse_lang_prefix(q or "")
    if not term:
        return []
    idx = EightyFourThousandIndex()
    if not idx.available():
        return []
    out = []
    for m in idx.search(term, lang=lang)[:limit]:
        label = m.get("english") or m.get("sanskrit") or m.get("tibetan") or f"Toh {m['toh']}"
        out.append({"id": m["toh"], "label": label, "sublabel": f"Toh {m['toh']}",
                    "url": f"/works/authority/search?q={q}"})
    return out


# THE registry. Singular `label` is the completion prefix the user asked for.
SEARCH_TYPES = [
    SearchType("editions",    "Edition",   "Editions",    _search_editions),
    SearchType("works",       "Work",      "Works",       _search_works),
    SearchType("people",      "Person",    "People",      _search_people),
    SearchType("subjects",    "Subject",   "Subjects",    _search_subjects),
    SearchType("authorities", "Authority", "Authorities", _search_authorities),
]


def aggregate_search(db, q: str, *, limit_per_group: int = 25,
                     only: str | None = None) -> dict:
    """One query → results grouped by type, iterating SEARCH_TYPES so new types
    appear automatically. `only` restricts to a single group key (the chips)."""
    q = (q or "").strip()
    groups = []
    if q:
        for t in SEARCH_TYPES:
            if only and t.key != only:
                continue
            try:
                items = t.search(db, q, limit_per_group)
            except Exception:
                items = []                       # a flaky type (e.g. offline authority) never 500s the page
            groups.append({"key": t.key, "label": t.label, "label_plural": t.label_plural,
                           "count": len(items), "hits": items})   # 'hits' not 'items' (Jinja dict.items clash)
    return {"q": q, "groups": groups}


def suggest(db, q: str, *, per_type: int = 6) -> list[dict]:
    """Flat, type-prefixed completion list for the Typeahead box: each match
    carries its singular `type` so the renderer can prefix it ('Edition …')."""
    flat = []
    for g in aggregate_search(db, q, limit_per_group=per_type)["groups"]:
        for it in g["hits"]:
            flat.append({"type": g["label"], "label": it["label"],
                         "sublabel": it.get("sublabel", ""), "url": it["url"]})
    return flat


def main(argv=None) -> None:
    import argparse
    from catalogue.db_store import init_db
    ap = argparse.ArgumentParser(
        description="Find books by book title / author / work title. Matches "
                    "canonically — diacritic- AND digraph-insensitive — across ALL "
                    "aliases of a work/person. Multiple fields (and repeated --author) "
                    "are ANDed.")
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--book-title", help="match the edition's own title / subtitle / "
                                         "native title (substring)")
    ap.add_argument("--author", action="append", default=[],
                    help="match an author of a contained work (repeatable → AND)")
    ap.add_argument("--work-title", help="match any alias of a contained work")
    ap.add_argument("--subject", help="match a subject AND everything nested under "
                                      "it (prefix-inclusive, e.g. 'Buddhism')")
    args = ap.parse_args(argv)
    if not (args.book_title or args.author or args.work_title or args.subject):
        ap.error("give at least one of --book-title / --author / --work-title / --subject")
    db = init_db(args.db)
    rows = find_books(db, book_title=args.book_title, authors=args.author,
                      work_title=args.work_title, subject=args.subject)
    print(f"{len(rows)} book(s) match:")
    for r in rows:
        print(f"  e{r['edition_id']}: {r['title']!r}")
        if r["authors"]:
            print(f"      authors: {', '.join(r['authors'])}")
        if r["works"]:
            print(f"      works:   {', '.join(r['works'])}")
        if r["files"]:
            print(f"      files:   {', '.join(r['files'])}")


if __name__ == "__main__":
    main()
