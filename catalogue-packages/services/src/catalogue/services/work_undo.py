"""Reversible-undo for the works-rebuild apply (`works_apply`) — the works twin of
`contributor_undo`, reusing the SAME journal: it registers a 'work' kind into
`contributor_undo`'s registry so `log_undo` / `apply_undo` / `undo_log` /
`/works/detect/undo` all work unchanged. Only the snapshot SHAPE differs.

`apply_single` / `apply_multi` rewrite one edition's work-graph: drop the degenerate
whole-book work, link or create the canonical / contained works, and move authorship
to the edition. We snapshot that edition's work-graph BEFORE the op and restore it
verbatim on undo. Work SHARING is honoured — dropped works are re-created with INSERT
OR IGNORE (a no-op if another edition still holds them), and works the op CREATED are
removed on undo only once they are orphaned.

`created_work_ids` is filled in AFTER the op runs (only then do we know which works
were freshly minted) and passed to `log_undo`, whose fingerprint then captures the
post-op state — the precondition re-checked at undo time.
"""
from __future__ import annotations

import json

from catalogue.services import contributor_undo as undo

# Tables that make up an edition's restorable work-graph. edition-scoped rows are
# wiped + re-inserted; the works behind the old links are re-created idempotently.
# work_detection rides along so undo also reverts the report's 'applied' flag;
# edition_subject so undo reverts a folder-subject apply attached to a modern edition.
_EDITION_TABLES = ("edition_work", "edition_author", "work_detection", "edition_subject")
# work_subject so undo rebuilds a dropped placeholder's subjects (apply moves them to the
# edition; undo reverts the edition_subject AND restores them on the rebuilt work).
_WORK_TABLES = ("work", "work_author", "work_alias", "work_subject")


def _acc(db):
    """A system Access over this connection — the row-snapshot journal + work/edition reads/writes."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def snapshot_edition(db, eid: int, *, created_work_ids=()) -> dict:
    """Capture edition `eid`'s work-graph: its edition_work + edition_author links, and
    the full rows of every work those links point at (so a dropped work can be rebuilt).
    `created_work_ids` are works the op minted — deleted on undo once orphaned."""
    j = _acc(db).journal
    snap = {"kind": "work", "edition_id": int(eid),
            "created_work_ids": sorted({int(w) for w in created_work_ids})}
    for t in _EDITION_TABLES:
        snap[t] = j.capture(t, "edition_id", [eid])
    old_wids = sorted({r["work_id"] for r in snap["edition_work"]})
    snap["work"] = j.capture("work", "id", old_wids)
    snap["work_author"] = j.capture("work_author", "work_id", old_wids)
    snap["work_alias"] = j.capture("work_alias", "work_id", old_wids)
    snap["work_subject"] = j.capture("work_subject", "work_id", old_wids)
    return snap


def restore_edition(db, snap: dict) -> None:
    """Reverse a works-apply: clear the edition's current links, rebuild any work the op
    dropped, re-insert the snapshot links, then drop op-created works left orphaned.
    Caller owns the transaction/commit."""
    acc = _acc(db)
    eid = snap["edition_id"]
    for t in _EDITION_TABLES:
        acc.journal.clear_eq(t, "edition_id", eid)
    for t in _WORK_TABLES:                       # rebuild dropped works (idempotent)
        acc.journal.insert_rows(t, snap.get(t) or [])
    for t in _EDITION_TABLES:                    # restore the edition's links
        acc.journal.insert_rows(t, snap.get(t) or [])
    for wid in snap.get("created_work_ids") or []:   # GC works the op created
        if not acc.works.reads.has_edition_link(wid):
            acc.works.writes.hard_delete(wid)


def fingerprint_edition(db, snap_or_eid) -> str:
    """Deterministic fingerprint of edition `eid`'s CURRENT work-graph — the undo
    precondition (captured post-op, re-checked at undo)."""
    eid = snap_or_eid["edition_id"] if isinstance(snap_or_eid, dict) else snap_or_eid
    s = snapshot_edition(db, eid)
    norm = {t: sorted(json.dumps(r, sort_keys=True) for r in s[t])
            for t in (*_EDITION_TABLES, *_WORK_TABLES)}
    return json.dumps(norm, sort_keys=True)


def _missing(db, snap: dict) -> list:
    eid = snap["edition_id"]
    if not _acc(db).health.owner_exists("edition", eid):
        return [f"edition#{eid}"]
    return []


def pending_undo(db, eid) -> "int | None":
    """The newest still-undoable works-apply token for edition `eid` (or None) — lets
    the report render an ↩ Undo button straight from journal state, no session needed."""
    return _acc(db).undo_log.newest_for_op_edition("works_apply%", eid)


undo.register_kind("work", fingerprint=fingerprint_edition, missing=_missing,
                   restore=restore_edition, ids_key="edition_id")
