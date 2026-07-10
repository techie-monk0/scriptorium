"""Reversible-undo journal for the destructive contributor ops (merge / delete /
split) behind the Resolve picker.

A merge DELETES the folded-away person and re-points its edges onto the survivor; a
split creates parts and removes the blob. These have no natural inverse, so reconstructing
the prior state by hand-writing one is fragile (id reuse, alias dedup, external ids, the
edition_work override). Instead we take a uniform SNAPSHOT of every person the op touches
*before* it runs, and restore those rows verbatim on undo. (A curated DELETE is the
exception — it tombstones the row reversibly, so its inverse is a flag-flip restore: see
the lightweight `person_tombstone` kind at the foot of this module.)

A snapshot captures, for a set of person ids, the complete pre-op rows in every table
that stores a person identity or edge:

    person, person_alias, person_external_id, work_author, edition_translator

plus the `edition_work.translator_person_id` per-work override (a column on a shared
table, so it is NULLed and re-set rather than deleted). `person_ids` is the full set
to CLEAR before re-inserting — it includes persons the op CREATED (split parts), which
have no snapshot rows and so simply vanish on restore.

`log_undo` persists a snapshot into `undo_log` (within the op's own transaction, so a
rolled-back op leaves no undo entry) and returns its id as the token. `apply_undo`
restores it and consumes the row. See undo_log in schema.sql.
"""
from __future__ import annotations

import json

from catalogue.db_store.integrity import assert_integrity


def _acc(db):
    """A system Access over this connection — the row-snapshot journal + undo_log + entity-existence."""
    from catalogue.access_api import system_conn
    return system_conn(db)

# ── Kind registry — the journal (undo_log + log_undo/apply_undo) is generic; each
# kind ('person', 'work') registers how to fingerprint / find-missing-refs / restore
# its own snapshot shape, so a new reversible op reuses the whole journal. ──────────
_HANDLERS: dict = {}


def register_kind(kind, *, fingerprint, missing, restore, ids_key):
    """Register a snapshot kind. `fingerprint(db, snap)->str`, `missing(db, snap)->list`,
    `restore(db, snap)->None`; `ids_key` = the snapshot key the UI refreshes by."""
    _HANDLERS[kind] = {"fingerprint": fingerprint, "missing": missing,
                       "restore": restore, "ids_key": ids_key}


def _handler(snap):
    return _HANDLERS[snap.get("kind", "person")]


# (table, person-id column). `person` first: it is the FK parent, so it must be
# re-inserted before its edges and is the table whose CASCADE clears the children.
_PERSON_TABLES = (
    ("person", "id"),
    ("person_alias", "person_id"),
    ("person_external_id", "person_id"),
    ("work_author", "person_id"),
    ("edition_translator", "person_id"),
    ("edition_author", "person_id"),
)


def snapshot_persons(db, person_ids, *, clear_ids=None) -> dict:
    """Capture the full pre-op rows of `person_ids` across every person table, plus
    any edition_work override pointing at them. `clear_ids` (default = `person_ids`)
    is the set the restore will wipe before re-inserting — pass the wider set when the
    op also CREATES persons (split), so those created rows are removed on undo."""
    j = _acc(db).journal
    ids = sorted({int(p) for p in person_ids})
    tables = {table: j.capture(table, col, ids) for table, col in _PERSON_TABLES}
    overrides = j.capture_cols(
        "edition_work", ("edition_id", "work_id", "translator_person_id"),
        "translator_person_id", ids)
    clear = sorted({int(p) for p in (clear_ids if clear_ids is not None else ids)})
    return {"kind": "person", "person_ids": clear, "tables": tables,
            "edition_work_overrides": overrides}


def restore_snapshot(db, snap: dict) -> None:
    """Reverse an op: clear every involved person's current rows, then re-insert the
    snapshot verbatim. Persons present in `person_ids` but absent from the snapshot
    (op-created parts) are thereby removed. Caller owns the transaction/commit."""
    j = _acc(db).journal
    ids = sorted({int(p) for p in snap["person_ids"]})
    # The override is a column on the shared edition_work table — detach (NULL), never
    # delete the row. Do it first so the person delete below can't strand it.
    j.null_column("edition_work", "translator_person_id", "translator_person_id", ids)
    # Deleting the person cascades its alias/external-id/work_author/edition_translator
    # rows (all FK ON DELETE CASCADE), clearing both pre-existing and op-created ids.
    j.clear("person", "id", ids)
    # Re-insert in FK order (person first — it is _PERSON_TABLES[0]). Plain INSERT (the
    # rows were just cleared), not OR IGNORE.
    for table, _col in _PERSON_TABLES:
        j.insert_rows(table, snap["tables"].get(table) or [], or_ignore=False)
    for o in snap["edition_work_overrides"]:
        j.update_row("edition_work", {"translator_person_id": o["translator_person_id"]},
                     {"edition_id": o["edition_id"], "work_id": o["work_id"]})


def state_fingerprint(db, person_ids) -> str:
    """A deterministic fingerprint of the CURRENT rows of `person_ids` across every
    person table — the precondition for a safe undo. Captured post-op and re-checked at
    undo time: if it no longer matches, the records were edited (or the id was reused)
    since, so restoring would clobber newer data and undo is refused."""
    snap = snapshot_persons(db, person_ids)
    norm = {t: sorted(json.dumps(r, sort_keys=True) for r in rows)
            for t, rows in snap["tables"].items()}
    norm["__overrides__"] = sorted(json.dumps(o, sort_keys=True)
                                   for o in snap["edition_work_overrides"])
    norm["__ids__"] = snap["person_ids"]
    return json.dumps(norm, sort_keys=True)


def _missing_refs(db, snap: dict) -> list:
    """work/edition ids the snapshot would re-link to that no longer exist — re-
    inserting their edges would violate FKs, so undo must refuse cleanly instead."""
    work_ids, edition_ids = set(), set()
    for r in snap["tables"].get("work_author", []):
        work_ids.add(r["work_id"])
    for r in snap["tables"].get("edition_translator", []):
        edition_ids.add(r["edition_id"])
    for o in snap["edition_work_overrides"]:
        edition_ids.add(o["edition_id"])
        work_ids.add(o["work_id"])
    health = _acc(db).health
    missing = [f"work#{w}" for w in sorted(work_ids) if not health.owner_exists("work", w)]
    missing += [f"edition#{e}" for e in sorted(edition_ids)
                if not health.owner_exists("edition", e)]
    return missing


def log_undo(db, op: str, summary: str, snap: dict) -> int:
    """Persist a snapshot as an undoable entry (with its post-op fingerprint); returns
    its id (the undo token). Kind-agnostic — the fingerprint is computed by the handler
    registered for `snap['kind']` (person by default)."""
    return _acc(db).undo_log.append(
        op, summary, json.dumps(snap), _handler(snap)["fingerprint"](db, snap))


def peek_undo(db, token: int) -> "dict | None":
    r = _acc(db).undo_log.get(token)
    if not r:
        return None
    return {"op": r[0], "summary": r[1], "snap": json.loads(r[2]), "precheck": r[3]}


def apply_undo(db, token: int, *, commit: bool = True) -> dict:
    """Restore the snapshot behind `token` and consume it. Returns the affected person
    ids (so the UI can refresh exactly those rows), or a clear error.

    GUARDED: undo is refused (no mutation) unless the involved records are byte-for-byte
    as the op left them — the post-op fingerprint must still match (no intervening edit
    or id-reuse to clobber) and every work/edition the snapshot re-links to must still
    exist. Atomic: a failed restore rolls back and leaves the entry."""
    entry = peek_undo(db, token)
    if not entry:
        return {"error": "nothing to undo — already undone or no longer available"}
    snap = entry["snap"]
    h = _handler(snap)
    if h["fingerprint"](db, snap) != (entry["precheck"] or ""):
        return {"error": "these records have changed since the operation — undo is no "
                         "longer available (restoring now would discard the newer edits)"}
    missing = h["missing"](db, snap)
    if missing:
        return {"error": "undo unavailable — these no longer exist: " + ", ".join(missing)}
    try:
        h["restore"](db, snap)
        _acc(db).undo_log.delete(token)
        if commit:
            assert_integrity(db)
            db.commit()
    except Exception:
        db.rollback()
        raise
    return {"undone": token, "op": entry["op"], "summary": entry["summary"],
            h["ids_key"]: snap[h["ids_key"]]}


register_kind("person", fingerprint=lambda db, snap: state_fingerprint(db, snap["person_ids"]),
              missing=_missing_refs, restore=restore_snapshot, ids_key="person_ids")


# ── person_tombstone — undo for a SOFT delete ────────────────────────────────────────
# A curated delete (contributor_edit.apply_delete) tombstones the person via the access
# engine: the row + every alias / external-id / edge RIDES the tombstone (nothing is
# purged), so the inverse is a flag-flip restore, not a row re-insert. This kind keeps the
# tiny `{person_ids}` payload; merge/split (which HARD-delete the loser/blob) keep using
# the verbatim-snapshot "person" kind above.
def snapshot_tombstone(db, pid: int) -> dict:
    """Lightweight undo record for a tombstone delete — just the id; the row still exists
    (flagged `deleted_at`), so restore clears the flag and the edges reappear."""
    return {"kind": "person_tombstone", "person_ids": [int(pid)]}


def _tombstone_fingerprint(db, snap: dict) -> str:
    """Identity of the tombstoned row, so id-reuse or an intervening restore/hard-delete
    refuses the undo. Captured post-op (the row IS tombstoned then); a still-tombstoned row
    with the same name/dates matches, anything else returns a sentinel that won't."""
    pid = snap["person_ids"][0]
    rows = _acc(db).journal.capture("person", "id", [pid])   # captures a TOMBSTONED row too
    row = rows[0] if rows else None
    if not row or row["deleted_at"] is None:  # gone, id-reused, or already un-tombstoned
        return "__absent__"
    return f"{row['primary_name']}|{row['dates']}"


def _tombstone_restore(db, snap: dict) -> None:
    """Clear `deleted_at` via the engine (staged on this connection); the caller commits."""
    from catalogue.access_api import system_conn
    from catalogue.contracts import Ref
    system_conn(db).persons.writes.restore(Ref("person", snap["person_ids"][0]))


register_kind("person_tombstone", fingerprint=_tombstone_fingerprint,
              missing=lambda db, snap: [], restore=_tombstone_restore, ids_key="person_ids")
