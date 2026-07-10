"""Catalogue review service — the operator walks promoted books and confirms /
corrects how they were catalogued (title, contributors, single-vs-multi work,
single-vs-multi author, author-vs-translator role), with an edition_verify
authority signal to guide the verdict.

The spine is the **Works-Curation model** (`WorksDraft`): one data shape that
describes an edition's works, each with its contributors and roles, where every
work carries an `included` flag. Three adapters build it — from a promoted
edition, from a pending proposal payload, or from a fresh re-run of the
detection engine — and one `apply_draft` materialises it back onto the edition.
Include/exclude expresses single-vs-multi *work*; the per-contributor role
expresses single-vs-multi *author* and author-vs-translator. The same model
drives web and CLI, batch and single edition.

No Flask import here — the web routes and the CLI both call this module.
Mutations are `commit`-gated with a `plan_*` preview, mirroring
`catalogue/contributor_edit.py`.
"""
from __future__ import annotations

import json
import re

from catalogue.db_store import contributor_store as cs
from catalogue.db_store import add_alias, fold_key, nfc
from .names import split_contributors
from .promote import get_or_create_person


def _acc(db):
    """A system Access over this connection — engine-routed work/edition/person/holding reads +
    writes (edition_work link CRUD, work notes, review verdict) + the journal. Caller commits."""
    from catalogue.access_api import system_conn
    return system_conn(db)

WORK_KINDS = ("root", "commentary", "work")
ROLES = ("author", "translator")


# ── WorksDraft shape ──────────────────────────────────────────────────────────
# WorksDraft = {"structure": str|None, "book_contributors": [contrib],
#               "works": [Work]}
# Work       = {"included": bool, "work_id": int|None, "title": str,
#               "kind": str|None, "locator": str|None, "contributors": [contrib]}
# contrib    = {"name": str, "role": "author"|"translator", "person_id": int|None}

def _contrib(name, role, person_id=None) -> dict:
    role = role if role in ROLES else "author"
    return {"name": (name or "").strip(), "role": role, "person_id": person_id}


def derive_structure(draft: dict) -> str:
    """single_work when ≤1 work is included, else multi_work — recomputed from the
    include flags so toggling a checkbox updates it. Only `collection_unsegmented`
    is honoured as an explicit override, since it cannot be inferred from a count
    (one container holding many undistinguished texts)."""
    if draft.get("structure") == "collection_unsegmented":
        return "collection_unsegmented"
    n = sum(1 for w in draft.get("works", []) if w.get("included", True))
    return "single_work" if n <= 1 else "multi_work"


# ── adapters ──────────────────────────────────────────────────────────────────
def _work_title(db, wid: int) -> str:
    return _acc(db).works.reads.representative_title(wid) or "(untitled)"


def _work_kind(db, wid: int):
    n = _acc(db).works.reads.notes(wid)
    return n if n in ("root", "commentary") else None


def draft_from_edition(db, edition_id: int) -> dict:
    """Build a WorksDraft from a promoted edition's edition_work +
    work_contributor rows (all included). Contributor person_id is carried so the
    widget can show resolution + offer picker actions."""
    acc = _acc(db)
    works = []
    rows = acc.works.reads.edition_work_rows(edition_id)
    # Translators live on the edition now; surface the edition's set on each work
    # so the per-work curation widget can show + re-assign them.
    trans_contribs = [
        _contrib(acc.persons.reads.get(pid).primary_name, "translator", pid)
        for pid in cs.edition_translator_ids(db, edition_id)]
    for wid, _seq, locator in rows:
        contribs = []
        for pid, role in cs.work_author_rows(db, wid):
            name = acc.persons.reads.get(pid).primary_name
            contribs.append(_contrib(name, role, pid))
        contribs += trans_contribs
        works.append({"included": True, "work_id": wid,
                      "title": _work_title(db, wid), "kind": _work_kind(db, wid),
                      "locator": locator, "contributors": contribs})
    draft = {"structure": None, "book_contributors": [], "works": works}
    draft["structure"] = derive_structure(draft)
    return draft


def draft_from_payload(payload: dict) -> dict:
    """Build a WorksDraft from a book_toc_pattern proposal payload (the existing
    pending path). Authors/translators lists become per-contributor rows."""
    works = []
    for w in payload.get("works") or []:
        contribs = [_contrib(a, "author") for a in (w.get("authors") or [])]
        contribs += [_contrib(t, "translator") for t in (w.get("translators") or [])]
        works.append({"included": True, "work_id": None,
                      "title": (w.get("title") or "").strip(),
                      "kind": w.get("kind"), "locator": w.get("locator"),
                      "contributors": contribs})
    book = [_contrib(a, "author") for a in (payload.get("book_authors") or [])]
    book += [_contrib(t, "translator") for t in (payload.get("book_translators") or [])]
    return {"structure": payload.get("structure"), "book_contributors": book,
            "works": works}


def draft_to_payload(draft: dict) -> dict:
    """Inverse of `draft_from_payload`: a WorksDraft → a book_toc_pattern payload
    fragment (book_authors/book_translators/works/structure). Only INCLUDED works
    are emitted. Lets the proposal-editor save path reuse the curation widget
    while still storing the canonical payload shape the promoter consumes."""
    works = []
    for w in draft.get("works", []):
        if not w.get("included", True):
            continue
        authors = [c["name"] for c in w.get("contributors", []) if c["role"] == "author"]
        translators = [c["name"] for c in w.get("contributors", []) if c["role"] == "translator"]
        ww: dict = {"title": (w.get("title") or "").strip() or "(untitled)"}
        if w.get("kind"):
            ww["kind"] = w["kind"]
        if authors:
            ww["authors"] = authors
        if translators:
            ww["translators"] = translators
        if w.get("locator"):
            ww["locator"] = w["locator"]
        works.append(ww)
    book = draft.get("book_contributors", [])
    return {"structure": draft.get("structure") or None,
            "book_authors": [c["name"] for c in book if c["role"] == "author"],
            "book_translators": [c["name"] for c in book if c["role"] == "translator"],
            "works": works}


# ── form parsing (shared by every curation surface — Flask-free) ───────────────
def _parse_contribs(form, prefix: str) -> list:
    """Pull contributor rows named ``<prefix><j>_name`` / ``_role`` / ``_pid``
    out of a form mapping (request.form or a plain dict). Empty-name rows drop."""
    def val(k):
        return (form.get(k) or "").strip()
    idx = sorted({int(m.group(1)) for k in form
                  for m in [re.match(rf"{re.escape(prefix)}(\d+)_name$", k)] if m})
    out = []
    for j in idx:
        name = val(f"{prefix}{j}_name")
        if not name:
            continue
        pid = val(f"{prefix}{j}_pid")
        out.append(_contrib(name, val(f"{prefix}{j}_role") or "author",
                            int(pid) if pid.isdigit() else None))
    return out


def parse_works_form(form) -> dict:
    """Parse a submitted curation form into a WorksDraft. Field scheme (matches
    templates/_works_curation.html):
      structure, bc<j>_{name,role}, w<i>_{included,work_id,title,kind,locator},
      w<i>_c<j>_{name,role,pid}.
    A work row with neither a title nor any contributor is dropped (the blank
    spare-row convention). `form` is any mapping with `.get` + key iteration, so
    the parser is unit-testable without Flask."""
    def val(k):
        return (form.get(k) or "").strip()
    work_idx = sorted({int(m.group(1)) for k in form
                       for m in [re.match(r"w(\d+)_title$", k)] if m})
    works = []
    for i in work_idx:
        title = val(f"w{i}_title")
        contribs = _parse_contribs(form, f"w{i}_c")
        if not title and not contribs:
            continue
        wid = val(f"w{i}_work_id")
        works.append({
            "included": form.get(f"w{i}_included") is not None,
            "work_id": int(wid) if wid.isdigit() else None,
            "title": title, "kind": val(f"w{i}_kind") or None,
            "locator": val(f"w{i}_locator") or None, "contributors": contribs,
        })
    return {"structure": val("structure") or None,
            "book_contributors": _parse_contribs(form, "bc"), "works": works}


def draft_from_detection(db, edition_id: int, *, single_author_multi_work: bool = False,
                         multi_author: bool = False) -> dict:
    """Re-run the real detection engine over the edition's holding (no DB writes,
    via StagingConn) with an optional volume preset, and return the proposed
    WorksDraft. Heavy (LLM ladder + extraction) — a deliberate operator action.
    Returns {"error": …} if the edition has no usable holding."""
    hr = _acc(db).holdings.reads.by_edition(edition_id)
    if not hr:
        return {"error": f"edition {edition_id} has no holding to re-detect"}
    holding_id = hr[0].id
    # Deferred imports: heavy (LLM clients), and keeps this module Flask/network-free
    # to import.
    from .classify import default_ladder
    from .process import ProcessConfig, apply_volume_preset, process_holding
    from .staging import StagingConn

    cfg = ProcessConfig(use_text_layer_toc=True, analyze_book=True, ladder=default_ladder())
    apply_volume_preset(cfg, single_author_multi_work=single_author_multi_work,
                        multi_author=multi_author)
    sc = StagingConn(db)
    process_holding(sc, holding_id, cfg)
    for w in sc.writes:
        if "review_queue" in w["sql"] and (w["params"] or [None])[0] == "book_toc_pattern":
            return draft_from_payload(json.loads(w["params"][1]))
    return {"error": "detection produced no proposal"}


# ── apply ───────────────────────────────────────────────────────────────────────
def _resolve_contributor(db, c: dict, *, created: list) -> int | None:
    """Resolve a draft contributor to a person id: an explicit, still-existing
    person_id wins; otherwise resolve/create by name (fold-key dedup). A blob
    name ("A, B") is NOT split here — the operator splits via the picker; we keep
    the row as one person so curation stays predictable."""
    pid = c.get("person_id")
    if pid and _acc(db).persons.reads.get(pid) is not None:
        return pid
    name = nfc((c.get("name") or "").strip())
    if not name:
        return None
    pid, was_new = get_or_create_person(db, name, c.get("role"))
    if was_new:
        created.append(pid)
    return pid


def _set_work_title(db, wid: int, title: str) -> None:
    title = (title or "").strip() or "(untitled)"
    prim = _acc(db).works.reads.primary_alias(wid)
    if prim:
        _acc(db).works.writes.update_alias(prim[0], title)
    else:
        add_alias(db, "work", wid, title, "english")


def _reconcile_contributors(db, wid: int, contribs: list, *, created: list,
                            orphan_persons: set) -> list:
    """Make `wid`'s AUTHOR set (work_author) match the author rows in `contribs`,
    and return the translator person-ids found (the caller puts them on the
    EDITION). Persons that lose their last author edge are queued for guarded GC."""
    desired_authors = set()               # {(person_id, role)}
    translator_pids = []
    for c in contribs:
        pid = _resolve_contributor(db, c, created=created)
        if pid is None:
            continue
        role = c.get("role") if c.get("role") in ROLES else "author"
        if role == "translator":
            if pid not in translator_pids:
                translator_pids.append(pid)
        else:
            desired_authors.add((pid, "author"))
    orphan_persons.update(cs.set_work_authors(db, wid, desired_authors))
    return translator_pids


def _gc_work(db, wid: int) -> bool:
    """Delete a work iff no edition_work references it any more (cascades clear
    work_contributor / work_alias). Returns True if deleted."""
    acc = _acc(db)
    if acc.works.reads.has_edition_link(wid):
        return False
    acc.works.writes.hard_delete(wid)
    return True


def _gc_persons(db, pids: set) -> list:
    """Delete each person that no surviving edge references (mirrors
    promote.revert_proposal's guard). Returns the deleted ids."""
    acc = _acc(db)
    gone = []
    for pid in pids:
        if not cs.person_referenced(db, pid):
            acc.journal.clear("person", "id", [pid])   # guarded hard delete (cascades edges)
            gone.append(pid)
    return gone


def plan_apply_draft(db, edition_id: int, draft: dict) -> dict:
    """Preview applying `draft` to an edition: which works are added / removed /
    kept, and the resulting structure. No mutation."""
    current = draft_from_edition(db, edition_id)
    cur_ids = {w["work_id"] for w in current["works"]}
    kept_ids = {w.get("work_id") for w in draft.get("works", [])
                if w.get("included", True) and w.get("work_id")}
    added = [w["title"] for w in draft.get("works", [])
             if w.get("included", True) and not w.get("work_id")]
    removed = [_work_title(db, wid) for wid in cur_ids - kept_ids]
    kept = [w["title"] for w in draft.get("works", [])
            if w.get("included", True) and w.get("work_id") in cur_ids]
    return {"edition_id": edition_id, "structure_after": derive_structure(draft),
            "works_added": added, "works_removed": removed, "works_kept": kept,
            "n_included": len(added) + len(kept)}


def apply_draft(db, edition_id: int, draft: dict, *, commit: bool = True) -> dict:
    """Materialise `draft` onto the edition: included existing works are updated
    (title / kind / locator / contributors / translator slot), included new works
    are created and linked, excluded/removed works are unlinked from this edition
    and orphan-GC'd, and everything is re-sequenced. Persons that lose their last
    edge are GC'd (guarded). Returns a summary of what changed."""
    included = [w for w in draft.get("works", []) if w.get("included", True)]
    keep_ids = {w.get("work_id") for w in included if w.get("work_id")}
    created_persons: list = []
    orphan_persons: set = set()
    created_works: list = []
    removed_works: list = []

    # 1. Unlink works no longer included, GC if globally orphaned.
    acc = _acc(db)
    current_ids = acc.works.reads.ids_in_edition(edition_id)
    for wid in current_ids:
        if wid not in keep_ids:
            orphan_persons.update(cs.work_author_ids(db, wid))   # edition translators GC'd below
            acc.works.writes.unlink_from_edition(edition_id, wid)
            if _gc_work(db, wid):
                removed_works.append(wid)

    # 2. Create / update each included work, in draft order. Authors go on the work
    #    (work_author); translators are collected and set on the EDITION after the loop.
    edition_translator_pids: list = []
    for seq, w in enumerate(included, start=1):
        kind = (w.get("kind") or "").strip()
        notes = kind if kind in ("root", "commentary") else None
        wid = w.get("work_id")
        if wid:
            acc.works.writes.set_scalars(wid, {"notes": notes})
        else:
            wid = acc.works.writes.insert_work({"notes": notes})
            from catalogue.services import subjects as S
            S.ensure_categorized(db, "work", wid)   # never subject-less; review will flag it
            created_works.append(wid)
        _set_work_title(db, wid, w.get("title"))
        for pid in _reconcile_contributors(
                db, wid, w.get("contributors") or [], created=created_persons,
                orphan_persons=orphan_persons):
            if pid not in edition_translator_pids:
                edition_translator_pids.append(pid)
        locator = (w.get("locator") or "").strip() or None
        acc.works.writes.link_to_edition(edition_id, wid, seq, locator)

    # The edition's translator set = union across its included works; replacing it
    # surfaces any dropped translator for guarded GC.
    orphan_persons.update(cs.set_edition_translators(db, edition_id, edition_translator_pids))
    gc_persons = _gc_persons(db, orphan_persons - set(created_persons))
    if commit:
        db.commit()
    return {"edition_id": edition_id, "structure": derive_structure(draft),
            "works_created": created_works, "works_removed": removed_works,
            "persons_created": created_persons, "persons_gc": gc_persons}


# ── verdict (read/write) ─────────────────────────────────────────────────────
def get_review(db, edition_id: int) -> dict:
    r = _acc(db).editions.reads.review_verdict(edition_id)
    if not r:
        return {}
    return {"status": r[0], "flags": json.loads(r[1]) if r[1] else {},
            "note": r[2], "reviewed_at": r[3]}


def set_review(db, edition_id: int, *, status: str | None = None, flags: dict | None = None,
               note: str | None = None, commit: bool = True) -> dict:
    """Write the verdict. `flags` is MERGED into any existing flags (so a triage
    action can flip one flag without clobbering the rest). Stamps reviewed_at when
    a terminal status (ok/needs_fix) is set."""
    if status == "ok":
        from catalogue.services import subjects as S
        if S.has_uncategorized(db, "edition", edition_id):
            raise S.UncategorizedError(
                "This edition is still tagged “Uncategorized”. Assign a real subject "
                "(and remove the Uncategorized tag) before marking it reviewed.")
    cur = get_review(db, edition_id)
    merged = {**(cur.get("flags") or {}), **(flags or {})}
    new_status = status if status is not None else cur.get("status")
    new_note = note if note is not None else cur.get("note")
    _acc(db).editions.writes.set_review_verdict(
        edition_id, new_status, json.dumps(merged), new_note,
        stamp=new_status in ("ok", "needs_fix"))
    if commit:
        db.commit()
    return get_review(db, edition_id)


def _set_flag(db, eid: int, name: str, value: bool) -> None:
    set_review(db, eid, flags={name: value}, commit=False)
