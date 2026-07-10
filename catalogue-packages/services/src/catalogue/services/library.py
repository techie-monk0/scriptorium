"""The single-dashboard service (Flask-free).

Backs `/library` — the everyday loop collapsed to one page: a search box + an
`Add book` upload, a master list of books (browse or search results), and a
universal inline editor whose payoff is **entity cross-navigation** (the FRBR
multiplicity the data model now carries):

    author  ─→ their works
    work    ─→ all its editions / translations   (volume sets grouped once)
    edition ─→ its translator(s) ─→ their other editions

All reads run off the live FRBR tables (`work_author`, `edition_translator`,
`edition_work`). No network. The browser/list contract is the row shape the
shared `_book_browser.html` master-detail shell consumes.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

from catalogue.db_store import contributor_store as cs


def _acc(db):
    """A system Access over this connection — the engine-routed display/FRBR-graph reads that back
    the read-only Library views (editions, works, persons, holdings). Read-only here."""
    from catalogue.access_api import system_conn
    return system_conn(db)


# Roles on work_author that count as "authored the composition" for cross-links
# and subtitles. (The vocab also carries attributed/compiler/reviser.)
_AUTHOR_ROLES = ("author", "attributed", "compiler", "reviser")


def display_title(title, volume) -> str:
    """Canonical book title for display: the title with the volume number appended
    (`Title · vol. N`) when the edition is a member of a multi-volume set. THE one rule,
    shared by web (`_edition_title.html` via the `display_title` Jinja global), the device
    replica (`export_replica`), and any native client — so a book's title reads identically
    everywhere instead of each surface formatting volumes its own way (the PWA had three
    inconsistent copies). `volume` may be a raw string ('v. 4') or an int. See
    [[present-edition-volume-number]]."""
    t = title or "(untitled)"
    m = re.search(r"\d+", str(volume if volume is not None else ""))
    n = int(m.group()) if m else 0
    suffix = f" · vol. {n}"
    return t if (n == 0 or t.endswith(suffix)) else t + suffix


# ── Master-list rows (the book-browser contract) ──────────────────────────
def _file_ext(path: str | None) -> str | None:
    if not path or "." not in path:
        return None
    return path.rsplit(".", 1)[-1].lower()


def _cover_handle(db, eid: int):
    """(holding_id, file_path, archival_pdf_path) of the edition's first holding, or None — the
    open-in-viewer handle the browser/home/person tiles share."""
    return _acc(db).holdings.reads.cover_handle(eid)


def _register_upload(db, dest) -> tuple:
    """Mint a bare edition + (text_status='none') electronic holding for an uploaded file, returning
    (edition_id, holding_id). Shared by the hermetic and the extraction-failed registration paths."""
    from catalogue.services import subjects as S
    from catalogue.services.mount import owning_root_id
    acc = _acc(db)
    eid = acc.editions.writes.create({"title": dest.stem}).target.id
    S.ensure_categorized(db, "edition", eid)   # never subject-less; review later
    hid = acc.holdings.writes.insert_holding(
        edition_id=eid, form="electronic", file_path=str(dest), text_status="none",
        root_id=owning_root_id(str(dest)))
    return eid, hid


def browser_row(db, eid: int) -> dict | None:
    """One book as the master-detail browser consumes it: stable id, a title,
    a one-line subtitle (authors · works · review state), the done flag, and the
    open-in-viewer handles, in the row shape the shared `_book_browser.html`
    renders unchanged. None if the edition is gone."""
    e = _acc(db).editions.reads.browser_card(eid)
    if not e:
        return None
    title, rstatus, volume = e
    # The edition's OWN by-line (book-level authors + translators), not the authors of the
    # works it contains — same rule as the content-search results.
    from catalogue.services import search as SEARCH
    authors = SEARCH.edition_people(db, eid)
    n_works = len(_acc(db).works.reads.ids_in_edition(eid))
    hrow = _cover_handle(db, eid)
    hid = hrow[0] if hrow else None
    fpath = (hrow[1] or hrow[2]) if hrow else None
    by = (", ".join(authors[:2]) + (" …" if len(authors) > 2 else "")) if authors \
        else "no author"
    return {
        "id": eid,
        "title": title or "(untitled)",
        "display_title": display_title(title, volume),
        "subtitle": f"{by} · {n_works}w · {rstatus or 'unreviewed'}",
        "done": rstatus is not None,
        "holding_id": hid,
        "has_file": bool(fpath),
        "file_ext": _file_ext(fpath),
    }


def browse(db, *, limit: int = 500) -> list[dict]:
    """All books, newest first, as browser rows — the no-query master list."""
    ids = _acc(db).editions.reads.recent_ids(limit)   # newest first; tombstones excluded
    return [r for r in (browser_row(db, i) for i in ids) if r]


# ── Home page shelves (the Netflix-style splash) ──────────────────────────
def _home_card(db, eid: int) -> dict | None:
    """A book as a home-page tile: title, lead author (for the text fallback and
    caption), and the holding whose first page renders the cover preview. None if
    the edition is gone."""
    ed = _acc(db).editions.reads.get(eid)
    if not ed:
        return None
    e = (ed.title, _acc(db).editions.reads.volumes([eid]).get(eid))
    from catalogue.services import search as SEARCH
    authors = SEARCH.edition_people(db, eid)
    hrow = _cover_handle(db, eid)
    return {
        "eid": eid,
        "title": e[0] or "(untitled)",
        "display_title": display_title(e[0], e[1]),
        "by": authors[0] if authors else "",
        "holding_id": hrow[0] if hrow else None,
        "has_file": bool(hrow and (hrow[1] or hrow[2])),
        # Art handles for the shared Shelf component (web client-render + PWA replica).
        "cover_url": f"/edition/{eid}/cover.jpg",
        "spine_url": f"/edition/{eid}/spine.svg",
    }


# NOTE: the home-page shelf composition (recent/added/subject-rollup/series) now lives ONCE in the
# shared Tier-2 layer — `library-core.js` `homeVM` (ported to Swift), computed CLIENT-side from the
# cached replica. The former server `home_shelves` here was a duplicate of that logic and is removed;
# `/` supplies only the `recent_ids` primitive (see routes/home.py). `_home_card` stays — it's still
# used by `subject_tree` for the per-subject book list.


def search(db, *, book_title: str = "", work_title: str = "",
           author: str = "", person: str = "", subject: str = "") -> list[dict]:
    """Books matching the (diacritic-insensitive) search, as browser rows.
    Delegates the matching to `search.find_books` (the same canonical fold the
    A–Z browse uses), then re-shapes each hit into the browser contract.
    `person` matches ANY contributor role (author OR translator); `author` (legacy)
    matches the author role only. `subject` is prefix-inclusive (a parent subject
    covers everything nested under it)."""
    from .search import find_books
    hits = find_books(
        db,
        book_title=book_title or None,
        work_title=work_title or None,
        authors=[author] if author else None,
        persons=[person] if person else None,
        subject=subject or None,
    )
    return [r for r in (browser_row(db, h["edition_id"]) for h in hits) if r]


# ── Type-scoped candidate rows + entity-graph decomposition (the sectioned
#    Review pane on /library). Each row carries a `seltype` so the sectioned
#    master list can scope single-type bulk selection; `decompose_*` turns one
#    selected entity M into the related-entity sections shown beside it. ──────
def _edition_row(db, eid: int) -> dict | None:
    """A `browser_row` tagged with its selection type (for the sectioned pane)."""
    r = browser_row(db, eid)
    if r is not None:
        r["seltype"] = "edition"
    return r


def work_row(db, wid: int, *, roles=None) -> dict:
    """One WORK as a sectioned-pane master row (the work twin of `browser_row`):
    stable id, display title, an authors · N-editions subtitle, and the no-file
    handles works always carry. `roles` (e.g. from `person_works`) prefixes the
    subtitle so a person's works show how they contributed."""
    n_ed = _acc(db).works.reads.edition_link_count(wid)
    authors = [_person_label(db, pid) for pid, _role in cs.work_author_rows(db, wid)]
    by = (", ".join(authors[:2]) + (" …" if len(authors) > 2 else "")) if authors else "no author"
    sub = f"{by} · {n_ed} ed."
    if roles:
        sub = f"{' · '.join(roles)} · {sub}"
    return {"id": wid, "seltype": "work", "title": _alias_title(db, wid),
            "subtitle": sub, "done": False,
            "holding_id": None, "has_file": False, "file_ext": None}


def person_row(db, pid: int, *, roles=None) -> dict | None:
    """One PERSON as a sectioned-pane master row. None if the person is gone."""
    p = _acc(db).persons.reads.get(pid)
    if not p:
        return None
    name, role_hint, dates = p.primary_name, p.role_hint, p.dates
    bits = []
    if roles:
        bits.append(" · ".join(roles))
    bits += [x for x in (role_hint, dates) if x]
    return {"id": pid, "seltype": "person", "title": name or "(unnamed)",
            "subtitle": " · ".join(bits) or "person", "done": False,
            "holding_id": None, "has_file": False, "file_ext": None}


def search_works(db, work_title: str) -> list[dict]:
    """Works whose any alias matches the title fragment (diacritic-/digraph-folded),
    as candidate rows — the work twin of `search()`. Mirrors `routes/works.py`'s
    `works_search` matching (`work_alias.normalized_key LIKE`)."""
    from catalogue.db_store import fold_key
    wids = [r[0] for r in _acc(db).works.reads.search_hits(fold_key(work_title), limit=200)]
    return [work_row(db, wid) for wid in wids]


def search_persons(db, person: str) -> list[dict]:
    """People whose any alias matches the fragment, as candidate rows — the person
    twin of `search()`. Mirrors `library_dashboard.library_suggest_person`."""
    rows = _acc(db).persons.reads.search(person, limit=200)
    return [r for r in (person_row(db, row[0]) for row in rows) if r]


def decompose_edition(db, eid: int) -> list[dict]:
    """The sections shown beside a selected EDITION M: its contained works, and the
    other editions/translations of those works (deduped, current book dropped) — the
    same graph `_library_detail.html` renders, surfaced as bulk-selectable lists."""
    summaries = edition_work_summaries(db, eid)
    sections = []
    if summaries:
        sections.append({"key": "works", "label": "Works in this book", "seltype": "work",
                         "rows": [work_row(db, w["id"]) for w in summaries]})
    seen, ed_rows = set(), []
    for w in summaries:
        for oe in w.get("other_editions", []):
            if oe["id"] == eid or oe["id"] in seen:
                continue
            seen.add(oe["id"])
            r = _edition_row(db, oe["id"])
            if r:
                ed_rows.append(r)
    if ed_rows:
        sections.append({"key": "editions", "label": "Other editions / translations",
                         "seltype": "edition", "rows": ed_rows})
    return sections


def decompose_work(db, wid: int) -> list[dict]:
    """The sections beside a selected WORK M: every edition realizing it, and its author(s)."""
    s = work_summary(db, wid)
    if s is None:
        return []
    sections = []
    ed_rows = [r for r in (_edition_row(db, oe["id"]) for oe in s["other_editions"]) if r]
    if ed_rows:
        sections.append({"key": "editions", "label": "Editions of this work",
                         "seltype": "edition", "rows": ed_rows})
    p_rows = [r for r in (person_row(db, a["id"], roles=[a["role"]] if a.get("role") else None)
                          for a in s["authors"]) if r]
    if p_rows:
        sections.append({"key": "authors", "label": "Author(s)", "seltype": "person",
                         "rows": p_rows})
    return sections


def decompose_person(db, pid: int) -> list[dict]:
    """The sections beside a selected PERSON M: their works, and the editions they contributed to."""
    sections = []
    works = person_works(db, pid)
    if works:
        sections.append({"key": "works", "label": "Works", "seltype": "work",
                         "rows": [work_row(db, w["work_id"], roles=w["roles"]) for w in works]})
    b_rows = [r for r in (_edition_row(db, b["edition_id"]) for b in person_books(db, pid)) if r]
    if b_rows:
        sections.append({"key": "editions", "label": "Editions", "seltype": "edition",
                         "rows": b_rows})
    return sections


def candidate_row(db, seltype: str, eid_or_id: int) -> dict | None:
    """One row for a deep-linked entity (`?eid`/`?wid`/`?pid` with no search field),
    so a direct link synthesizes a one-item candidate list of the right type."""
    if seltype == "edition":
        return _edition_row(db, eid_or_id)
    if seltype == "work":
        return work_row(db, eid_or_id)
    if seltype == "person":
        return person_row(db, eid_or_id)
    return None


def decompose(db, seltype: str, sid: int) -> list[dict]:
    """Dispatch to the per-type decomposer for a selected entity M."""
    return {"edition": decompose_edition, "work": decompose_work,
            "person": decompose_person}.get(seltype, lambda *_: [])(db, sid)


# ── Entity cross-links (the FRBR navigation payoff) ────────────────────────
def _person_label(db, pid: int) -> str:
    p = _acc(db).persons.reads.get(pid)
    return p.primary_name if p else f"person#{pid}"


def _edition_translator_names(db, eid: int) -> list[str]:
    return _acc(db).editions.reads.translator_names(eid)


def edition_links(db, eid: int) -> dict | None:
    """The cross-navigation graph for one edition. Returns None if it's gone.

      {"edition_id", "title",
       "authors":     [{"person_id","name","n_works"}],      # work_author of its works
       "translators": [{"person_id","name","n_editions"}],   # edition_translator
       "works": [{"work_id","title","groups":[…]}]}          # each contained work's
                                                             #   OTHER editions, volume
                                                             #   sets collapsed once

    `works[].groups` is a list of either a standalone sibling edition
    {"is_set": False, "edition": {…}} or a volume set
    {"is_set": True, "set_id", "volumes": [{…}]} ordered by volume_seq.
    Sibling editions exclude this one; "this book" is flagged where it appears
    inside a set (so a vol-2 view still shows it sitting in its set)."""
    acc = _acc(db)
    ed = acc.editions.reads.get(eid)
    if not ed:
        return None
    title = ed.title

    # Works contained in this edition (ordered as in the book). Title = first alias by id.
    work_rows = [(wid, acc.works.reads.representative_title(wid))
                 for wid, _seq, _loc in acc.works.reads.edition_work_rows(eid)]

    # Authors: distinct persons authoring any contained work, with how many works
    # each has authored corpus-wide (the "→ their works" hook).
    authors = []
    for pid in acc.editions.reads.contained_work_author_ids(eid, _AUTHOR_ROLES):
        n = acc.persons.reads.authored_work_count(pid)
        authors.append({"person_id": pid, "name": _person_label(db, pid), "n_works": n})
    authors.sort(key=lambda a: a["name"].lower())

    # Translators of THIS edition, each with how many editions they translated.
    translators = []
    for pid in cs.edition_translator_ids(db, eid):
        n = len(cs.person_edition_ids_as_translator(db, pid))
        translators.append({"person_id": pid, "name": _person_label(db, pid),
                            "n_editions": n})
    translators.sort(key=lambda t: t["name"].lower())

    # Per work: its other editions/translations, volume sets collapsed once.
    works = []
    for wid, wtitle in work_rows:
        works.append({
            "work_id": wid,
            "title": wtitle or f"work#{wid}",
            "groups": _work_edition_groups(db, wid, current_eid=eid),
        })

    return {"edition_id": eid, "title": title, "authors": authors,
            "translators": translators, "works": works}


def _work_edition_groups(db, wid: int, *, current_eid: int) -> list[dict]:
    """Editions realizing work `wid`, with multi-volume sets collapsed to one
    group (the dedup-by-set rule from the volume model). The current edition is
    excluded when it stands alone, but kept (flagged) inside its own set so a
    single volume still navigates the whole set."""
    rows = _acc(db).editions.reads.realizations(wid)
    groups: list[dict] = []
    sets: dict[int, dict] = {}
    for eid, etitle, volume, language, vsid, vseq in rows:
        ed = {
            "id": eid, "title": etitle or "(untitled)", "volume": volume,
            "language": language, "volume_seq": vseq,
            "is_current": eid == current_eid,
            "translators": _edition_translator_names(db, eid),
        }
        if vsid is not None:
            g = sets.get(vsid)
            if g is None:
                g = {"is_set": True, "set_id": vsid, "volumes": []}
                sets[vsid] = g
                groups.append(g)
            g["volumes"].append(ed)
        elif eid != current_eid:          # standalone: drop the book we're viewing
            groups.append({"is_set": False, "edition": ed})
    return groups


def person_books(db, pid: int) -> list[dict]:
    """Every book (edition) that names this person as a contributor, for the person
    review pane. A person appears as **author** (work_author on a contained work) or
    **translator** (edition_translator, or a per-work translator override on
    edition_work). Returns one row per edition with its roles + the first holding's
    open-in-viewer handles (holding_id/has_file/file_ext) so the title links to the
    actual file. Ordered by edition id."""
    acc: dict[int, dict] = {}

    def _note(eid, title, role):
        d = acc.get(eid)
        if d is None:
            d = acc[eid] = {"edition_id": eid, "title": title or "(untitled)",
                            "roles": set()}
        d["roles"].add(role)

    # Book-level author + contained-work author + edition translator + per-work translator override —
    # the four contribution edges, one engine read (UNION ALL), each row role-tagged.
    for eid, title, role in _acc(db).editions.reads.person_book_rows(pid):
        _note(eid, title, role)

    out = []
    for eid in sorted(acc):
        d = acc[eid]
        h = _cover_handle(db, eid)
        fpath = (h[1] or h[2]) if h else None
        out.append({"edition_id": eid, "title": d["title"], "roles": sorted(d["roles"]),
                    "holding_id": h[0] if h else None,
                    "has_file": bool(fpath), "file_ext": _file_ext(fpath)})
    return out


def person_works(db, pid: int) -> list[dict]:
    """Every WORK this person contributed to, for the person review pane — the FRBR
    layer above `person_books` (one row per intellectual work, not per physical
    edition). A person is an **author** (work_author) or a **translator** (a work in
    an edition they translated). Returns one row per work with its roles + display
    title, ordered by work id."""
    acc: dict[int, dict] = {}

    def _note(wid, role):
        d = acc.get(wid)
        if d is None:
            d = acc[wid] = {"work_id": wid, "roles": set()}
        d["roles"].add(role)

    for wid, role in _acc(db).persons.reads.authored_work_roles(pid):
        _note(wid, role or "author")
    # person_work_ids also returns authored works — only the ones NOT already noted
    # are translator-only contributions (mirrors web.py:_person_card_context).
    for wid in cs.person_work_ids(db, pid):
        if wid not in acc:
            _note(wid, "translator")

    return [{"work_id": wid, "title": _alias_title(db, wid),
             "roles": sorted(acc[wid]["roles"])}
            for wid in sorted(acc)]


# ── Read-only edition/work summaries (the three-layer display) ─────────────
# Backs the shared Edition Basics + "Works In This Edition" + collapsed Work Details
# rendered on BOTH Browse (read-only) and Review (editable basics). Pure reads — safe
# to call from GET fragment endpoints and from existing render contexts.

def _alias_title(db, wid: int) -> str:
    """Display title for a work: prefer English, then any non-filename alias, then
    a filename alias, else 'work #N'. Mirrors web.py:_work_title's English-first rule
    but never falls back to a bare filename when a real alias exists."""
    return _acc(db).works.reads.alias_title(wid) or f"work #{wid}"


def _authority_links(system: str | None, number: str | None) -> list[dict]:
    """Click-through web links for a work's single canonical authority pair
    (canonical_system / canonical_number) via picker.authority_url. Empty when the
    pair is absent or the system has no public page mapping."""
    if not (system and number):
        return []
    from .picker import authority_url
    sys = system.strip().lower()
    num = number.strip()
    if ":" in num:                       # already namespaced (e.g. 'bdr:W123')
        ext = num
    elif sys in ("bdrc", "bdr"):
        ext = f"bdr:{num}"
    else:                                # toh / wikidata / viaf / dila → use as-is
        ext = f"{sys}:{num}"
    url = authority_url(ext)
    return [{"label": f"{system}:{number}", "url": url}] if url else []


# Alias schemes that carry a native-script / transliterated TITLE (vs. an English or
# bibliographic alias). Sanskrit = IAST + Devanagari ('sa'); Tibetan = Wylie + Unicode ('bo').
_SKT_SCHEMES = ("iast", "sa")
_TIB_SCHEMES = ("wylie", "bo")


def work_summary(db, wid: int, *, exclude_eid: int | None = None) -> dict | None:
    """Read-only context for one work — the single source for Work Basics + Work
    Details. Reuses the existing helpers (commentary_root_id, work_author_rows,
    subjects_for, picker.authority_url). None if the work is gone.

    Shape::

        {"id", "title", "work_type",                       # Basics
         "root": {"id","title"} | None,                    #   only when commentary
         "authors": [{"id","name","role"}],
         "subjects": [(sid, name)],
         "other_editions": [{"id","title"}],               #   excludes exclude_eid
         "notes",
         "native": {"sanskrit": [str], "tibetan": [str]},  # Details
         "authority_links": [{"label","url"}],
         "aliases_other": [{"text","scheme"}],
         "original_language", "era", "canonical_system", "canonical_number", "tradition"}
    """
    from catalogue.db_store import contributor_store as cs
    from catalogue.services import work_identity, subjects as S

    acc = _acc(db)
    w = acc.works.reads.summary_fields(wid)
    if not w:
        return None
    (work_type, original_language, era, canon_sys, canon_num,
     skt_col, tib_col, notes, tradition) = w

    aliases = acc.works.reads.aliases_with_id(wid)
    display_title = _alias_title(db, wid)
    display_alias_id = aliases[0][0] if aliases else None

    # Authors — work-level, with role + person name + link target.
    authors = []
    for pid, role in cs.work_author_rows(db, wid):
        authors.append({"id": pid, "name": _person_label(db, pid), "role": role})

    # Root text (only meaningful for a commentary).
    root = None
    if work_type == "commentary":
        rid = work_identity.commentary_root_id(db, wid)
        if rid:
            root = {"id": rid, "title": _alias_title(db, rid)}

    # Other editions realizing this work (links into Browse), current edition dropped.
    other_editions = []
    for oeid, otitle in acc.editions.reads.other_editions(wid):
        if exclude_eid is not None and oeid == exclude_eid:
            continue
        other_editions.append({"id": oeid, "title": otitle or f"edition {oeid}"})

    # Native titles: from native-script aliases, falling back to the denormalized columns.
    skt = [a[1] for a in aliases if a[2] in _SKT_SCHEMES]
    tib = [a[1] for a in aliases if a[2] in _TIB_SCHEMES]
    if not skt and skt_col:
        skt = [skt_col]
    if not tib and tib_col:
        tib = [tib_col]

    # Every OTHER alias — not the display title, not a filename, not a native-title one.
    native_schemes = _SKT_SCHEMES + _TIB_SCHEMES
    aliases_other = [{"text": a[1], "scheme": a[2]} for a in aliases
                     if a[0] != display_alias_id and a[2] != "filename"
                     and a[2] not in native_schemes]

    return {
        "id": wid,
        "title": display_title,
        "work_type": work_type,
        "root": root,
        "authors": authors,
        "subjects": S.subjects_for(db, "work", wid),
        "other_editions": other_editions,
        "notes": notes,
        "native": {"sanskrit": skt, "tibetan": tib},
        "authority_links": _authority_links(canon_sys, canon_num),
        "aliases_other": aliases_other,
        "original_language": original_language,
        "era": era,
        "canonical_system": canon_sys,
        "canonical_number": canon_num,
        "tradition": tradition,
    }


def edition_work_summaries(db, eid: int) -> list[dict]:
    """The contained works of an edition (ordered as in the book) as read-only
    `work_summary` dicts — for the shared "Works In This Edition" section.

    Applies the SURFACING predicate so a modern single edition's degenerate
    placeholder work is never shown (→ empty list → the section is hidden): a work
    is surfaced when the edition is multi-work, OR it shares the edition with other
    works, OR it carries a canonical number, OR it is a root/commentary. This matches
    `_detect_view`'s own gating so Browse and Review agree."""
    acc = _acc(db)
    links = acc.editions.reads.edition_work_notes(eid)
    wids = [r[0] for r in links]
    notes = {r[0]: r[1] for r in links}
    structure = acc.editions.reads.structure_of(eid)
    # Contained works that are the TARGET of this edition's Layer-2 modern commentary —
    # rendered with a ⬑ back-ref so the per-work block connects to the edition-level banner.
    commentary_target_ids = acc.editions.reads.commentary_target_work_ids(eid)
    out = []
    for wid in wids:
        wf = acc.works.reads.summary_fields(wid)
        if not wf:
            continue
        work_type, canon_num = wf[0], wf[4]
        surface = (structure == "multi_work" or len(wids) > 1
                   or canon_num or work_type in ("root", "commentary"))
        if surface:
            s = work_summary(db, wid, exclude_eid=eid)
            if s:
                # Per-appearance note lives on the join, not the work — inject it
                # here so the read-only "Works In This Edition" view can show it.
                s["edition_note"] = notes.get(wid)
                s["is_modern_commentary_target"] = wid in commentary_target_ids
                out.append(s)
    return out


def edition_commentaries(db, eid: int) -> list[dict]:
    """The classical works THIS edition is a modern commentary on (Layer 2 —
    `edition_commentary_on`), each as {"id", "title"}. The modern commentary belongs to
    the edition as a whole (its modern author), not to any contained work — so it is an
    edition→work edge, distinct from the work↔work `relationship` (Layer 1). Many-to-many;
    a target may also be a contained work (internal) or held elsewhere (external).
    See docs/design/commentary_relationships_model.md."""
    wids = sorted(_acc(db).editions.reads.commentary_target_work_ids(eid))
    return [{"id": wid, "title": _alias_title(db, wid)} for wid in wids]


def edition_persons(db, eid: int) -> dict:
    """Edition-level persons with ids (for the read-only Edition Basics by-line):
    book authors (`edition_author`) and translators (`edition_translator` + per-work
    overrides). Authors of CONTAINED works are NOT included — they belong to the work
    sections. Same scope as `search.edition_people`, but carrying person ids for links."""
    from catalogue.db_store import contributor_store as cs
    authors = [{"id": p, "name": _person_label(db, p)} for p in cs.edition_author_ids(db, eid)]
    translators = [{"id": p, "name": _person_label(db, p)}
                   for p in cs.edition_translator_ids(db, eid)]
    return {"authors": authors, "translators": translators}


# ── Add-by-upload (file → edition + holding → pipeline) ────────────────────
_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"


def _safe_name(name: str) -> str:
    base = Path(name).name[:120]
    return "".join(c if c in _SAFE else "_" for c in base) or "upload"


def ingest_upload(db, src_path, *, dest_dir, filename: str | None = None,
                  process: bool = True) -> dict:
    """Register an uploaded PDF/EPUB as a new edition + holding, then (optionally)
    run the real extract/segment pipeline so it opens in the editor with works
    detected. Returns {"edition_id", "holding_id", "title", "processed",
    "error"}.

    Steps: copy the file into `dest_dir` under a collision-proof name, then run
    `sweep._process` (the same per-file ingest the corpus sweep uses — hashes,
    extracts text, upserts edition+holding). When `process` is True and the
    holding has usable text, run `process.process_holding` to detect TOC/works
    (its proposal lands in the review queue, same as a swept book).

    Pipeline failure never loses the upload — the edition+holding still exist and
    open in the editor; the error is returned for surfacing. `process=False`
    (tests) registers the file without invoking the heavy extractor/LLM."""
    from . import sweep

    src = Path(src_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(filename or src.name)
    dest = dest_dir / f"{uuid.uuid4().hex}_{safe}"
    shutil.copyfile(src, dest)

    result = {"edition_id": None, "holding_id": None, "title": dest.stem,
              "processed": False, "error": None}

    if not process:
        # Lightweight registration only — no extraction (hermetic path).
        eid, hid = _register_upload(db, dest)
        db.commit()
        result.update(edition_id=eid, holding_id=hid)
        return result

    cfg = sweep.SweepConfig(mount_root=dest_dir)
    report = sweep.SweepReport()
    try:
        sweep._process(db, cfg, dest, report)
        db.commit()
    except Exception as exc:                       # never let ingest 500 the upload
        result["error"] = f"extract: {exc}"

    hrow = _acc(db).holdings.reads.by_file_path(str(dest))   # dest is uuid-unique → one match
    if not hrow:
        # Extraction logged a problem and recorded no holding — register a bare
        # record so the upload is still navigable.
        eid, hid = _register_upload(db, dest)
        db.commit()
        result.update(edition_id=eid, holding_id=hid,
                      error=result["error"] or "no text extracted")
        return result

    hid, eid = hrow.id, hrow.edition_id
    ed = _acc(db).editions.reads.get(eid)
    result.update(edition_id=eid, holding_id=hid, title=ed.title if ed else None)

    # Detect contained works (TOC → proposal), best-effort.
    try:
        from . import process as proc
        proc.process_holding(db, hid)
        db.commit()
        result["processed"] = True
    except Exception as exc:
        result["error"] = result["error"] or f"process: {exc}"
    return result
