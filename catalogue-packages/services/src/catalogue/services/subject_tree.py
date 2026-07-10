"""Hierarchy-aware subject service — the ONE layer every client shares.

Subjects are slash-paths by convention (`Buddhism/Emptiness` = "Emptiness" nested
under "Buddhism"); see catalogue/domain/subjects.py for the storage model. This
module is the read side that finally understands that convention:

  * descendant resolution      — `A` ⊇ `A/B` ⊇ `A/B/C` (prefix match on the name)
  * the ONE inheritance query   — editions tagged directly OR via a contained work,
                                  rolled up across a subject and its descendants
                                  (`editions_for_subject`), replacing the copy that
                                  was duplicated in library.py / export_replica.py
  * a pre-order forest          — `subject_forest`, drives the curation tree + API
  * a single subject's page     — `subject_page`, drives /subject/<id> + the API

Series (kind='series') reuse the same machinery but are FLAT (no '/'); a series
page just lists its volumes, ordered by `edition.volume`.

The `/` path IS the parent link — there is no parent_id column. "Descendant of X"
is `name = X OR name LIKE X || '/%'` (NOCASE, wildcards escaped).
"""
from __future__ import annotations

from catalogue.services.subjects import PROTECTED_SUBJECTS


def _g(db):
    """The subject-GRAPH store bound over this connection (engine-routed primitives)."""
    from catalogue.access_api import system_conn
    return system_conn(db).subjects.graph


# ── path helpers ──────────────────────────────────────────────────────────────
def segments(name: str) -> list[str]:
    """Path segments of a subject name: `Buddhism/Emptiness` → `['Buddhism','Emptiness']`."""
    return [s for s in (name or "").split("/") if s != ""]


def leaf_label(name: str) -> str:
    """The last path segment — the label to show indented under its parent."""
    segs = segments(name)
    return segs[-1] if segs else (name or "")


def parent_name(name: str) -> str | None:
    """The container path of `name`, or None for a top-level name."""
    segs = segments(name)
    return "/".join(segs[:-1]) if len(segs) > 1 else None


def top_level(name: str) -> str:
    """The top-level segment a name rolls up into (`Buddhism/Emptiness` → `Buddhism`)."""
    segs = segments(name)
    return segs[0] if segs else (name or "")


def materialize_ancestors(db) -> list[str]:
    """One-time backfill for LEGACY data: ensure every existing topical '/'-path has
    real container rows above it (create `Buddhism` if only `Buddhism/Emptiness`
    exists). `get_or_create_subject` keeps this invariant for paths created going
    forward, but it can't fix rows that predate it (it returns early when the leaf
    already exists). Idempotent; returns the names it created. Caller commits."""
    from catalogue.services import subjects as S
    names = _g(db).topic_names()
    have = {n.casefold() for n in names}
    created = []
    for name in sorted(names):                            # original casing preserved
        segs = name.split("/")
        for i in range(1, len(segs)):
            anc = "/".join(segs[:i])
            if anc.casefold() not in have:
                S.get_or_create_subject(db, anc, kind="topic")
                have.add(anc.casefold())
                created.append(anc)
    return created


# ── descendant resolution ─────────────────────────────────────────────────────
def descendant_ids(db, sid: int) -> list[int]:
    """`sid` plus the ids of every subject nested beneath it (any depth). Empty if
    `sid` doesn't exist."""
    name = _g(db).name_of(sid)
    if not name:
        return []
    return _g(db).descendant_ids(name)


def descendant_ids_by_name(db, name: str) -> list[int]:
    """Like `descendant_ids` but keyed by name — for callers (search/CLI) that have
    a subject name, not an id. Empty list if no such subject."""
    name = (name or "").strip().strip("/")
    if not name:
        return []
    return _g(db).descendant_ids(name)


# ── THE inheritance query (centralized) ───────────────────────────────────────
def editions_for_subject(db, sid: int, *, include_descendants: bool = True) -> list[int]:
    """Edition ids a subject covers: tagged DIRECTLY (`edition_subject`) OR through a
    contained work (`edition_work` → `work_subject`, since classical editions inherit
    their works' subjects). With `include_descendants` (the default), rolls up the
    subject AND everything nested beneath it — so asking for `Buddhism` returns the
    `Buddhism/Emptiness` books too. This is the single source of truth for that JOIN
    (home shelves, the subject page, the replica, the prefix-inclusive book filter)."""
    ids = descendant_ids(db, sid) if include_descendants else [sid]
    return _g(db).editions_covering(ids)


def n_editions_for_subject(db, sid: int, *, include_descendants: bool = True) -> int:
    """Count of `editions_for_subject` (distinct editions). Convenience for badges."""
    return len(editions_for_subject(db, sid, include_descendants=include_descendants))


# ── the forest (curation tree + /api/v1/subjects) ─────────────────────────────
def subject_forest(db, *, kind: str = "topic", q: str | None = None) -> list[dict]:
    """Pre-order list of subject nodes of one `kind`. Each node:

        {id, name, leaf_label, depth, parent_id, has_children, is_protected,
         n_books_direct, n_books_total}

    `n_books_total` is descendant-inclusive (distinct editions); `n_books_direct`
    counts only the subject's own tags. Order is pre-order (a parent immediately
    precedes its subtree) because subject names already sort that way under NOCASE
    ('/' < letters, and a prefix sorts before its extensions). With `q`, the tree is
    filtered to matching names PLUS their ancestors (so a deep match isn't orphaned)."""
    from catalogue.services import subjects as S
    rows = S.list_subjects(db, kind=kind)                 # name-sorted = pre-order
    by_name = {r["name"].casefold(): r for r in rows}
    protected = {p.casefold() for p in PROTECTED_SUBJECTS}

    keep: set[str] | None = None
    if q and q.strip():
        needle = q.strip().casefold()
        keep = set()
        for r in rows:
            if needle in r["name"].casefold():
                keep.add(r["name"].casefold())
                for i in range(1, len(segments(r["name"]))):  # pull in ancestors
                    keep.add("/".join(segments(r["name"])[:i]).casefold())

    nodes = []
    for r in rows:
        name = r["name"]
        if keep is not None and name.casefold() not in keep:
            continue
        sid = r["id"]
        par = parent_name(name)
        par_row = by_name.get(par.casefold()) if par else None
        has_children = _g(db).has_children(name, kind)
        nodes.append({
            "id": sid, "name": name, "leaf_label": leaf_label(name),
            "depth": len(segments(name)) - 1,
            "parent_id": par_row["id"] if par_row else None,
            "has_children": has_children,
            "is_protected": name.casefold() in protected,
            "n_works": r["n_works"], "n_editions": r["n_editions"],
            "n_books_direct": n_editions_for_subject(db, sid, include_descendants=False),
            "n_books_total": n_editions_for_subject(db, sid, include_descendants=True),
        })
    return nodes


def subject_review_items(db, *, kind: str = "topic", q: str | None = None) -> list[dict]:
    """Forest nodes mapped to master-list rows for the Subjects curation surface — shared
    by the Review module (/review/subjects) and the legacy review hub. A genuine ORPHAN
    (a non-protected leaf with no books and no sub-subjects) is the only "needs review"
    state; containers and protected subjects are always done."""
    items = []
    for n in subject_forest(db, kind=kind, q=q or None):
        uncurated = (not n["is_protected"]) and (not n["has_children"]) \
            and (n["n_books_total"] == 0)
        items.append({
            "id": n["id"], "title": n["name"], "leaf_label": n["leaf_label"],
            "depth": n["depth"], "parent_id": n["parent_id"],
            "has_children": n["has_children"],
            "n_works": n["n_works"], "n_editions": n["n_editions"],
            "subtitle": (f"{n['n_books_total']} book"
                         + ("" if n["n_books_total"] == 1 else "s")
                         + ("" if not uncurated else " · needs review")),
            "done": not uncurated,
        })
    return items


# ── a single subject's browse page (/subject/<id> + /api/v1/subject/<id>) ──────
def _volume_sort_key(vol):
    """Sort key for a series volume label: leading integer first (so 2 < 10), then
    the raw string. Missing/blank volumes sort last."""
    s = (vol or "").strip()
    if not s:
        return (1, 1 << 30, "")
    import re
    m = re.match(r"\s*(\d+)", s)
    return (0, int(m.group(1)) if m else (1 << 30), s.casefold())


def subject_page(db, sid: int) -> dict | None:
    """Everything the subject browse page needs, for web + PWA + native:

        {subject:{id,name,kind,leaf_label}, crumbs:[{id,name,leaf_label}…],
         children:[forest-node…], books:[home-card…], n_books}

    `children` are the immediate sub-subjects (with their own rolled counts, so you
    can drill down / fold-unfold). `books` is the descendant-inclusive edition list
    (a series is ordered by `edition.volume`; a topic newest-first). None if `sid`
    is gone."""
    from catalogue.services import library as L
    from catalogue.access_api import system_conn
    row = _g(db).id_name_kind(sid)
    if not row:
        return None
    _id, name, kind = row
    subject = {"id": _id, "name": name, "kind": kind, "leaf_label": leaf_label(name)}

    # Breadcrumb: each existing ancestor path, top-down.
    crumbs = []
    segs = segments(name)
    for i in range(1, len(segs)):
        anc = "/".join(segs[:i])
        aid = _g(db).id_by_name(anc)
        if aid is not None:
            crumbs.append({"id": aid, "name": anc, "leaf_label": leaf_label(anc)})

    # Immediate children = forest nodes one level deeper whose parent is this subject.
    children = [n for n in subject_forest(db, kind=kind) if n["parent_id"] == _id]

    eids = editions_for_subject(db, sid, include_descendants=True)
    if kind == "series":
        vols = system_conn(db).editions.reads.volumes(eids) if eids else {}
        eids = sorted(eids, key=lambda e: _volume_sort_key(vols.get(e)))
    else:
        eids = sorted(eids, reverse=True)                 # newest first
    books = [c for c in (L._home_card(db, e) for e in eids) if c]
    return {"subject": subject, "crumbs": crumbs, "children": children,
            "books": books, "n_books": len(books)}


def subject_shelves(db, sid: int, *, per_row: int = 40) -> dict | None:
    """A subject as a Netflix-style page of SHELVES (same look as the home splash, one
    level down): `{subject, crumbs, shelves:[{id,name,count,books,more_url}], n_books}`.

    For a container, each immediate child becomes a shelf of its descendant-inclusive
    books (the shelf title drills into `/subject/<child>`); any books filed directly on
    the subject but under no child get a leading shelf. For a leaf (or a series) it's a
    single shelf of its books. Each shelf book is a `_home_card`. None if `sid` is gone."""
    from catalogue.services import library as L
    base = subject_page(db, sid)
    if base is None:
        return None
    cache: dict[int, dict | None] = {}

    def card(eid: int):
        if eid not in cache:
            cache[eid] = L._home_card(db, eid)
        return cache[eid]

    shelves = []
    children = base["children"]
    if children:
        for ch in children:
            eids = sorted(editions_for_subject(db, ch["id"]), reverse=True)[:per_row]
            books = [c for c in (card(e) for e in eids) if c]
            if books:
                shelves.append({"id": ch["id"], "name": ch["leaf_label"],
                                "count": ch["n_books_total"], "books": books,
                                "more_url": f"/subject/{ch['id']}"})
        # Books filed directly on this subject but under no child — keep them visible.
        direct = set(editions_for_subject(db, sid, include_descendants=False))
        covered = set()
        for ch in children:
            covered |= set(editions_for_subject(db, ch["id"]))
        leftover = sorted(direct - covered, reverse=True)
        if leftover:
            books = [c for c in (card(e) for e in leftover[:per_row]) if c]
            if books:
                shelves.insert(0, {"id": sid, "name": base["subject"]["leaf_label"],
                                   "count": len(leftover), "books": books,
                                   "more_url": f"/subject/{sid}?view=grid"})
    elif base["books"]:                                   # leaf / series: one shelf
        shelves.append({"id": sid, "name": base["subject"]["leaf_label"],
                        "count": base["n_books"], "books": base["books"][:per_row],
                        "more_url": f"/subject/{sid}?view=grid"})
    return {"subject": base["subject"], "crumbs": base["crumbs"],
            "shelves": shelves, "n_books": base["n_books"]}
