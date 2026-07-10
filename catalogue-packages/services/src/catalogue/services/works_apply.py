"""Apply a verified single-work detection (work_detection) into the canonical
tables — the rebuild step, one edition at a time, atomically.

Per the determination:
- **classical** → link the edition to the canonical Work (`get_or_create_work` on
  the canonical#/native/English title, carrying the recorded authors), drop the
  edition's old degenerate work if it's now orphaned, and mark the work reviewed.
- **modern** → there's no Work: move the edition's author(s) to `edition_author`
  and drop the degenerate work (orphan-GC'd).

Destructive (drops degenerate works), so: idempotent, atomic per edition, and the
caller should back up / dry-run first. Multi-work apply (choosing a segmentation)
is separate.
"""
from __future__ import annotations

import time

from catalogue.db_store import contributor_store as cs
from catalogue.services import work_identity, work_detect as WD
from catalogue.services import contributor_undo as undo, work_undo


def _acc(db):
    """A system Access over this connection — engine-routed work/edition reads + writes (incl. the
    placeholder-GC hard-delete) + the subject graph. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def _edition_work_ids(db, eid):
    return _acc(db).works.reads.ids_in_edition(eid)


def _recorded_author_pids(db, wids):
    pids = []
    for wid in wids:
        for p in cs.work_author_ids(db, wid):
            if p not in pids:
                pids.append(p)
    return pids


def _drop_if_orphan(db, wid):
    acc = _acc(db)
    if not acc.works.reads.has_edition_link(wid):
        acc.works.writes.hard_delete(wid)   # cascades work_author / alias
        return True
    return False


def _is_degenerate(db, wid, eid, *, ignore_subject=False):
    """True only for the auto-minted per-edition PLACEHOLDER work — the one apply may
    drop. A work the operator CURATED (linked via the picker: an authority id, a name
    search, an add-new, a root/commentary) is never degenerate, so apply keeps it.

    A work is degenerate iff it has no canonical id, no type, is linked to only this
    edition, (normally) carries no subject, and its title is just a placeholder — no
    aliases, a filename-derived alias, or an English alias that is merely the edition (book)
    title (or the title it was minted from). `ignore_subject=True` (set for a MODERN edition,
    which has no work) drops the placeholder even when a folder-scan subject was auto-attached."""
    acc = _acc(db)
    r = acc.works.reads.review_fields(wid)
    if not r:
        return False
    if r["canonical_number"] or (r["work_type"] or "").strip():
        return False                                      # has a canonical id / type → curated
    if acc.works.reads.edition_link_count(wid) > 1:
        return False                                      # shared across editions → real
    # A subject normally marks a work as curated and PROTECTS it — that's what keeps a
    # classical text (the work IS the text) from being dropped. EXCEPT on a MODERN edition
    # (ignore_subject=True): a modern book has no work, so its auto-minted placeholder must
    # drop even though a DIRECTORY-scan subject got auto-attached (a folder subject is not an
    # operator curation signal). This is why "add a subject" no longer strands a modern work.
    if not ignore_subject and acc.works.reads.has_subject(wid):
        return False
    aliases = [(sc, t) for t, sc in acc.works.reads.aliases(wid)]
    if not aliases:
        return True                                       # bare placeholder
    if any(sc == "filename" for sc, _t in aliases):
        return True                                       # file-derived → auto-minted placeholder
    if all(sc == "english" for sc, _t in aliases):
        from catalogue.db_store import fold_key
        # The English-only placeholder's alias is just the book title — either the CURRENT
        # edition title, or the title it was minted from (the detection's stored_title).
        # Matching stored_title too means a placeholder STRANDED by an edition rename (its
        # alias still the old garbled name) is still recognised and dropped on apply.
        cand = set()
        et = acc.editions.reads.get(eid)
        if et and et.title:
            cand.add(fold_key(et.title))
        det = WD.get_detection(db, eid)
        if det and det.get("stored_title"):
            cand.add(fold_key(det["stored_title"]))
        if cand and any(fold_key(t) in cand for _sc, t in aliases):
            return True                                   # English alias == book title → the per-edition mint
    return False


def sync_placeholder_title(db, eid, new_title) -> int:
    """Keep each per-edition PLACEHOLDER work's English alias in step with the edition
    (book) title. Called when the operator renames a garbled edition so the linked
    placeholder doesn't keep — and a later apply doesn't strand — the OLD name. Matches
    placeholders against the CURRENT title, so call this BEFORE writing the new title.
    Returns the number of placeholder works renamed."""
    from catalogue.db_store import add_alias
    n = 0
    for w in _edition_work_ids(db, eid):
        if not _is_degenerate(db, w, eid):
            continue
        _acc(db).works.writes.delete_aliases_by_scheme(w, "english")
        if new_title and new_title.strip():
            add_alias(db, "work", w, new_title.strip(), "english")
        n += 1
    return n


def apply_single(db, eid, *, commit=True, record_undo=True) -> dict:
    """Materialise the cached single-work detection for one edition. Returns a
    summary; no-op (status='skip') if there's no single detection. Reversible: a
    pre-op snapshot of the edition's work-graph is journalled via the shared undo
    log (see work_undo), and the returned `undo_token` drives /works/detect/undo."""
    det = WD.get_detection(db, eid)
    if not det or det.get("kind") != "single":
        return {"status": "skip", "reason": "no single detection"}

    snap = work_undo.snapshot_edition(db, eid) if record_undo else None
    cur = _edition_work_ids(db, eid)
    # A MODERN book has no work, so its auto-minted placeholder is dropped even if a folder
    # subject got auto-attached (ignore_subject). A CLASSICAL edition keeps subject-bearing
    # works (the work IS the text). CURATED works (canonical id / type / shared / native
    # title) are kept either way — that's what makes a manually-added work survive apply.
    is_modern = det.get("determination") != "classical"
    degen = {w for w in cur if _is_degenerate(db, w, eid, ignore_subject=is_modern)}
    real = [w for w in cur if w not in degen]
    # Authors the operator set on the edition WIN over the ones detected on the
    # degenerate work; fall back to the detected set if untouched.
    author_pids = cs.edition_author_ids(db, eid) or _recorded_author_pids(db, cur)

    created = merge_candidate = False
    canonical_wid = None
    # AUTO-classical with NO operator-linked work → materialise the detection's canonical
    # work. If the operator ALREADY linked a real work, use that one (never mint a duplicate
    # — and a linked work is exactly how a 'modern' edition is reclassified to classical).
    if det.get("determination") == "classical" and not real:
        canon = det.get("canonical") or {}
        titles = det.get("title") or {}
        system, number = canon.get("system"), canon.get("number")
        canonical_wid, created, merge_candidate = work_identity.get_or_create_work(
            db, canonical=(system, number) if (system and number) else None,
            english_title=(titles.get("english") or det.get("stored_title")),
            original_titles={"sanskrit": titles.get("sanskrit"),
                             "tibetan": titles.get("tibetan")},
            author_pids=author_pids)
        for pid in author_pids:
            cs.add_work_author(db, canonical_wid, pid)
        cs.link_work(db, eid, canonical_wid)
        _acc(db).works.writes.set_review_status(canonical_wid, "ok")
        real.append(canonical_wid)

    degen.discard(canonical_wid)            # never drop the work the mint just deduped onto
    # Drop ONLY the degenerate placeholders; keep every curated/canonical work. A dropped
    # placeholder's subjects move to the edition so a folder/manual subject isn't lost when
    # the work goes (undo restores both: edition_subject + the work's own work_subject).
    removed = []
    for w in degen:
        acc = _acc(db)
        for sid in acc.works.reads.subject_ids(w):
            acc.subjects.graph.attach("edition", eid, sid)
        cs.unlink_work(db, eid, w)
        if _drop_if_orphan(db, w):
            removed.append(w)

    if canonical_wid:                                   # auto-classical: authors on the work
        out = {"status": "applied", "determination": "classical", "work_id": canonical_wid,
               "created": created, "merge_candidate": merge_candidate, "works_removed": removed}
    elif real:                                          # operator curated real work(s)
        cs.set_edition_authors(db, eid, author_pids)    # preserve the book's authors (degenerate dropped)
        out = {"status": "applied", "determination": "classical", "work_id": real[0],
               "created": False, "merge_candidate": False, "works_removed": removed}
    else:                                               # genuinely modern: no work
        cs.set_edition_authors(db, eid, author_pids)
        out = {"status": "applied", "determination": "modern",
               "edition_authors": len(set(author_pids)), "works_removed": removed}

    # Carry the folder-derived subject to its final home so it's right immediately after
    # apply: the canonical WORK (classical → edition inherits) or the EDITION itself
    # (modern → no work). Mirrors subjects.attach_dir_subjects; reverts on undo
    # (edition_subject is snapshotted; a newly-created classical work cascades).
    from catalogue.services import subjects as S
    subj = S.suggest_edition_subject(db, eid)
    if subj:
        if out.get("determination") == "classical":
            S.add_subject(db, "work", out["work_id"], subj)
        else:
            S.add_subject(db, "edition", eid, subj)

    det["applied"] = True                 # mark the detection so the report shows it
    det["applied_at"] = time.time()       # review-recency stamp (review pane keeps the last N visible)
    # Record what apply ACTUALLY did, so an edition that GAINED a work (operator linked one
    # onto a 'modern' detection) is henceforth categorised CLASSICAL — not stuck under Modern.
    det["determination"] = out["determination"]
    WD.store_detection(db, eid, "single", det, commit=False)
    if record_undo:
        snap["created_work_ids"] = [out["work_id"]] if out.get("created") else []
        out["undo_token"] = undo.log_undo(
            db, "works_apply_single",
            f"applied single-work edition #{eid} ({out['determination']})", snap)
    if commit:
        db.commit()
    return out


def apply_multi(db, eid, method, *, commit=True, record_undo=True) -> dict:
    """Materialise a CHOSEN segmentation (`method` = 'deterministic' | the local
    model | the cloud model) of one multi_work edition: create each contained work
    (get_or_create_work on its canonical#/title, with its resolved authors), link
    it via edition_work in order, and drop the old whole-book work(s). Reversible via
    the shared undo log (returns `undo_token`)."""
    from catalogue.services.promote import get_or_create_person
    det = WD.get_detection(db, eid)
    if not det or det.get("kind") != "multi":
        return {"status": "skip", "reason": "no multi detection"}
    m = (det.get("methods") or {}).get(method)
    if not m:
        return {"status": "skip", "reason": f"no method {method!r}"}

    snap = work_undo.snapshot_edition(db, eid) if record_undo else None
    cur = _edition_work_ids(db, eid)
    created, minted, kept = [], [], set()
    for seq, w in enumerate(m.get("works") or [], start=1):
        canon = w.get("canonical") or {}
        author_pids = []
        for name in (w.get("authors") or []):
            if name and name.strip():
                pid, _ = get_or_create_person(db, name)
                if pid not in author_pids:
                    author_pids.append(pid)
        system, number = canon.get("system"), canon.get("number")
        wid, was_new, _mc = work_identity.get_or_create_work(
            db, canonical=(system, number) if (system and number) else None,
            english_title=w.get("title"),
            original_titles={"sanskrit": w.get("title_sanskrit"),
                             "tibetan": w.get("title_tibetan")},
            author_pids=author_pids)
        for pid in author_pids:
            cs.add_work_author(db, wid, pid)
        cs.link_work(db, eid, wid, sequence=seq)
        _acc(db).works.writes.set_review_status(wid, "ok")
        created.append(wid)
        if was_new:
            minted.append(wid)
        kept.add(wid)

    removed = []
    for old in cur:
        if old not in kept:
            cs.unlink_work(db, eid, old)
            if _drop_if_orphan(db, old):
                removed.append(old)

    det["applied"] = True
    det["applied_at"] = time.time()       # review-recency stamp (review pane keeps the last N visible)
    det["applied_method"] = method
    WD.store_detection(db, eid, "multi", det, commit=False)
    out = {"status": "applied", "method": method, "works_created": created,
           "works_removed": removed}
    if record_undo:
        snap["created_work_ids"] = minted
        out["undo_token"] = undo.log_undo(
            db, "works_apply_multi", f"applied multi-work edition #{eid} ({method})", snap)
    if commit:
        db.commit()
    return out
