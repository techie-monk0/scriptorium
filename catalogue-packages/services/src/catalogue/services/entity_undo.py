"""Reversible DELETE and MERGE for editions and works — the entity-level twin of
`contributor_undo` (persons) and `work_undo` (an edition's work-graph). Reuses the
SAME kind-dispatched journal: it registers an 'edition' and a 'workrow' snapshot kind,
so `log_undo` / `apply_undo` / `undo_log` / the ↩ Undo button all work unchanged.

The snapshot shape is the proven one from `snapshot_persons`: capture EVERY row of the
involved ids across the entity's whole table-set, and restore = delete those ids' rows
+ re-insert verbatim. That reverses a delete (re-create the subtree) AND a merge
(both ids captured pre-merge → both restored to exactly how they were).
"""
from __future__ import annotations

import json

from catalogue.services import contributor_undo as undo
from catalogue.services import work_merge

# An edition's complete subtree (table, edition-id column). `edition` first: the FK
# parent, so re-inserted before its children; deleting it cascades them.
_EDITION_TABLES = (
    ("edition", "id"), ("holding", "edition_id"), ("edition_work", "edition_id"),
    ("edition_author", "edition_id"), ("edition_translator", "edition_id"),
    ("edition_subject", "edition_id"), ("work_detection", "edition_id"),
    ("edition_verify_resolution", "edition_id"), ("edition_text", "edition_id"),
)
# A work's complete row-set (`work` first). `relationship` (work on BOTH ends) is
# handled separately via a two-column predicate.
_WORK_TABLES = (
    ("work", "id"), ("work_alias", "work_id"), ("work_author", "work_id"),
    ("edition_work", "work_id"), ("work_subject", "work_id"),
    ("work_tradition", "work_id"), ("collection_member", "work_id"),
)


def _acc(db):
    """A system Access over this connection — the row-snapshot journal + edition-merge op +
    holding/edition reads. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def _exists(db, table):
    """Whether `table` exists — the journal-backed table guard (reused by edition_consolidate)."""
    return _acc(db).journal.table_exists(table)


def _capture(db, tables, ids):
    j = _acc(db).journal
    out = {}
    for table, col in tables:
        if j.table_exists(table):
            out[table] = j.capture(table, col, ids)
    return out


def _clear(db, tables, ids):
    j = _acc(db).journal
    for table, col in tables:                      # parent first → cascades children
        if j.table_exists(table):
            j.clear(table, col, ids)


def _reinsert(db, tables, snap):
    j = _acc(db).journal
    for table, _col in tables:                     # FK order: parent first
        j.insert_rows(table, snap["tables"].get(table) or [])


def _fingerprint(db, tables, ids, *, with_relationship=False):
    cap = _capture(db, tables, ids)
    if with_relationship:
        cap["relationship"] = _relationship_rows(db, ids)
    norm = {t: sorted(json.dumps(r, sort_keys=True) for r in rows) for t, rows in cap.items()}
    return json.dumps(norm, sort_keys=True)


# ── edition kind ────────────────────────────────────────────────────────────────
def snapshot_editions(db, eids) -> dict:
    ids = sorted({int(e) for e in eids})
    return {"kind": "edition", "ids": ids, "tables": _capture(db, _EDITION_TABLES, ids)}


def restore_editions(db, snap) -> None:
    _clear(db, _EDITION_TABLES, snap["ids"])
    _reinsert(db, _EDITION_TABLES, snap)


# ── work kind (row-level — distinct from work_undo's edition-graph) ───────────────
def _relationship_rows(db, ids):
    j = _acc(db).journal
    if not j.table_exists("relationship"):
        return []
    return j.capture_two_col("relationship", "from_work_id", "to_work_id", ids)


def snapshot_works(db, wids) -> dict:
    ids = sorted({int(w) for w in wids})
    snap = {"kind": "workrow", "ids": ids, "tables": _capture(db, _WORK_TABLES, ids)}
    snap["tables"]["relationship"] = _relationship_rows(db, ids)
    return snap


def restore_works(db, snap) -> None:
    j = _acc(db).journal
    ids = snap["ids"]
    if j.table_exists("relationship"):
        j.clear_two_col("relationship", "from_work_id", "to_work_id", ids)
    _clear(db, _WORK_TABLES, ids)
    _reinsert(db, _WORK_TABLES, snap)
    # after works exist (FK)
    j.insert_rows("relationship", snap["tables"].get("relationship") or [])


undo.register_kind(
    "edition", restore=restore_editions, ids_key="ids", missing=lambda db, snap: [],
    fingerprint=lambda db, snap: _fingerprint(db, _EDITION_TABLES, snap["ids"]))
undo.register_kind(
    "workrow", restore=restore_works, ids_key="ids", missing=lambda db, snap: [],
    fingerprint=lambda db, snap: _fingerprint(db, _WORK_TABLES, snap["ids"], with_relationship=True))


# ── operations (snapshot → mutate → journal; return undo_token) ───────────────────
def _title_of(db, table, id_, col):
    rows = _acc(db).journal.capture(table, "id", [id_])
    return (rows[0][col] if rows else None) or f"{table} #{id_}"


def _holding_files(db, eid) -> list[str]:
    """Every on-disk file an edition's holdings point at — the source file_path and
    any archival_pdf_path — deduped, NULLs dropped. Used to move a deleted book's
    files into the Trash folder."""
    paths: list[str] = []
    for _hid, fp, apath, _form in _acc(db).holdings.reads.openable(eid):
        for p in (fp, apath):
            if p and p not in paths:
                paths.append(p)
    return paths


def delete_edition(db, eid, *, commit=True,
                   cover_cache=None, cover_pinned=None) -> dict:
    """Move an edition to Trash: cascade-delete its whole subtree (holdings + every
    edition→x link) and MOVE each holding's file (source + archival PDF) into the
    configured Trash folder. Reversible — the DB rows restore via the returned
    `undo_token`; the files wait in Trash (recover by hand). This is the SINGLE delete
    behavior the whole UI shares (single / bulk / detect-pane), so a delete means the
    same thing everywhere: records gone-but-undoable, files in Trash. The file move runs
    only AFTER the DB delete commits, so a failed delete never strands a moved file.

    `cover_cache`/`cover_pinned` (the cover-cache and pinned dirs) bust this edition's
    cached cover/spine/pin so a later import that reuses the freed id (SQLite recycles
    primary keys) can't inherit its art. An undo restores the rows; the cover re-fetches."""
    acc = _acc(db)
    if not acc.health.owner_exists("edition", eid):
        return {"status": "skip", "reason": f"no edition #{eid}"}
    # Stability S1: refuse to HARD-delete an edition an external tool depends on — a clean, typed
    # refusal BEFORE any destructive work (snapshot / cascade / file moves). The DB purge-guard
    # trigger is the backstop beneath this. To remove such an edition, withdraw (tombstone) it.
    from catalogue.access_api import tool_policy
    from catalogue.contracts import Capability, CapabilityRestricted
    try:
        tool_policy.enforce(db, Capability.PURGE, eid)
    except CapabilityRestricted as e:
        return {"status": "blocked", "reason": str(e), "edition_id": eid}
    title = _title_of(db, "edition", eid, "title")
    snap = snapshot_editions(db, [eid])
    paths = _holding_files(db, eid)             # captured before the cascade drops holdings
    hids = [h.id for h in acc.holdings.reads.by_edition(eid)]
    acc.journal.clear("edition", "id", [eid])   # HARD cascade-delete (reversible via the snapshot)
    from catalogue.services import dangling_refs
    dangling_refs.purge_edition_refs(db, eid, hids)   # pending review items + promotion rows
    token = undo.log_undo(db, "edition_delete", f"deleted edition #{eid} ({title})", snap)
    if commit:
        db.commit()
    # A wishlist item this edition fulfilled must not dangle on the now-deleted edition: reconcile
    # reverts it to the active wishlist (write-side, so GET /api/v1/wishlist stays read-only).
    try:
        from catalogue.services import wishlist_reconcile
        wishlist_reconcile.reconcile_acquisitions(db)
    except Exception:
        pass
    if cover_cache or cover_pinned:
        from catalogue.services import covers
        covers.purge_edition_art(cover_cache, cover_pinned, eid)
    # ALWAYS move the holding files to Trash — only after the DB delete is durable, so a
    # rolled-back delete can't leave a file stranded in Trash. But KEEP a file another edition's
    # holding still references (dedup/merge can leave two editions pointing at one file): this
    # edition's holdings are already gone here, so any live reference is a different edition's.
    from catalogue.services import mount
    moved = 0
    for p in paths:
        if _acc(db).holdings.reads.file_referenced(p):
            continue                               # still referenced elsewhere — don't trash it
        try:
            if mount.move_to_trash(p):
                moved += 1
        except OSError:
            pass                                   # storage read-only / detached; ignore
    return {"status": "deleted", "edition_id": eid, "title": title,
            "undo_token": token, "files_moved": moved}


def merge_editions(db, dup, into, *, commit=True, cover_cache=None, cover_pinned=None) -> dict:
    """Fold duplicate edition `dup` into `into`: re-point holdings + work links +
    contributors + subjects, then drop `dup`. Reversible (both captured pre-merge).

    `cover_cache`/`cover_pinned` bust the folded-away `dup` id's cached art, so a later
    import that reuses that freed id can't inherit it (see `delete_edition`)."""
    if dup == into:
        return {"error": "cannot merge an edition into itself"}
    acc = _acc(db)
    for e in (dup, into):
        if not acc.health.owner_exists("edition", e):
            return {"error": f"no such edition #{e}"}
    snap = snapshot_editions(db, [dup, into])
    acc.editions.writes.merge(dup, into)             # re-point edition-keyed edges + drop the loser
    token = undo.log_undo(db, "edition_merge",
                          f"merged edition #{dup} into #{into}", snap)
    if commit:
        db.commit()
    if cover_cache or cover_pinned:
        from catalogue.services import covers
        covers.purge_edition_art(cover_cache, cover_pinned, dup)
    return {"status": "merged", "dup": dup, "into": into,
            "into_title": _title_of(db, "edition", into, "title"), "undo_token": token}


def sole_work_editions(db, wid) -> list:
    """Editions whose ONLY linked work is `wid` — deleting the work would leave them
    with no work, so the operator must re-point them first. Returns `[{id, title}]`."""
    acc = _acc(db)
    out = []
    for ed in acc.editions.reads.by_work(wid):
        if len(acc.works.reads.ids_in_edition(ed.id)) == 1:
            out.append({"id": ed.id, "title": ed.title or f"edition #{ed.id}"})
    return out


def delete_work(db, wid, *, commit=True) -> dict:
    """Delete a work and its row-set (aliases/authors/links/relationships cascade);
    every edition that referenced it loses the link. Reversible.

    GUARDED: refused if any edition's ONLY work is `wid` — that edition would be left
    with no work. The operator must change those editions' work first; they're returned
    as `blocking_editions` so the UI can link straight to them."""
    if not _acc(db).health.owner_exists("work", wid):
        return {"status": "skip", "reason": f"no work #{wid}"}
    blocking = sole_work_editions(db, wid)
    if blocking:
        return {"error": "this is the only work on "
                + ("an edition" if len(blocking) == 1 else f"{len(blocking)} editions")
                + " — change their work before deleting it",
                "blocking_editions": blocking}
    snap = snapshot_works(db, [wid])
    _acc(db).journal.clear("work", "id", [wid])   # HARD cascade-delete (reversible via the snapshot)
    from catalogue.services import dangling_refs
    dangling_refs.purge_work_refs(db, wid)            # pending review items + promotion arrays
    token = undo.log_undo(db, "work_delete", f"deleted work #{wid}", snap)
    if commit:
        db.commit()
    return {"status": "deleted", "work_id": wid, "undo_token": token}


def merge_works(db, dup, into, *, commit=True) -> dict:
    """Fold duplicate work `dup` into `into` via the existing work_merge engine, made
    reversible (both works captured pre-merge)."""
    snap = snapshot_works(db, [dup, into])
    res = work_merge.apply_work_merge(db, dup, into, commit=False)
    if res.get("error"):
        return res
    token = undo.log_undo(db, "work_merge", f"merged work #{dup} into #{into}", snap)
    if commit:
        db.commit()
    return {**res, "status": "merged", "undo_token": token}
