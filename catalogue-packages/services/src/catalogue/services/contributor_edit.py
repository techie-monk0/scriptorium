"""Contributor cleanup ops behind the Resolve picker's Split / Delete / Merge actions.

Three operations on a `person` row, each re-pointing every edge that references it and
reporting exactly what changed (so the operator is told, before and after):

  * SPLIT a comma-joined blob ("Jamgön Kongtrul Lodrö Taye, Kalu Rinpoche
    Translation Group") into its parts. Each part is resolved to an EXISTING person
    by fold-key (reusing it) or CREATED, then inherits the blob's (work, role) edges
    and translator slots; the blob row is removed.
  * DELETE a person: detach it from every work/edition and drop the row.
  * MERGE a duplicate person into a canonical one ("Atifa" → "Atiśa", "Alex Berzin"
    → "Alexander Berzin"): re-point every work/edition edge onto the canonical row,
    move its aliases (deduped) + external ids, keep dates, drop the duplicate. The
    real fix for name-variant duplicates that fold-key + authority matching miss.

`plan_*` computes the effect WITHOUT mutating (for the confirm preview); `apply_*`
performs it and returns the same shape plus what was created. Edge dedup relies on
work_contributor's PK (work_id, person_id, role); the FK cascade/SET-NULL is not
relied on — every reference is handled explicitly so the report is accurate.
"""
from __future__ import annotations

import os

from catalogue.db_store import contributor_store as cs
from catalogue.db_store import fold_key
from catalogue.db_store.integrity import IntegrityError, assert_integrity, verified_commit
from . import contributor_undo as undo
from .promote import get_or_create_person

ROLES = ("author", "translator")


def _acc(db):
    """A system Access over this connection — engine-routed person/work/edition reads (the
    mutations already route through the persons engine via system_conn)."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def split_parts(name: str) -> list:
    """The comma-separated pieces of a contributor blob (trimmed, non-empty)."""
    return [p.strip() for p in (name or "").split(",") if p.strip()]


def _work_label(db, wid: int) -> str:
    return _acc(db).works.reads.representative_title(wid) or f"work#{wid}"


def _work_books(db, wid: int) -> list:
    """The book(s) (edition + holding) a work appears in — so the operator sees not
    just the work but the physical book the edge belongs to."""
    out = []
    for eid, title, hid, path in _acc(db).works.reads.books_of_work(wid):
        book = title or (os.path.basename(path) if path else None) or f"edition#{eid}"
        out.append({"edition_id": eid, "holding_id": hid, "book": book})
    return out


def _references(db, pid: int):
    """(authored works [(work_id, role)], translated edition ids) for a person —
    authors on work_author, translators on the edition (edition_translator + the
    per-work override)."""
    works = _acc(db).persons.reads.authored_work_roles(pid)
    editions = cs.person_edition_ids_as_translator(db, pid)
    return works, editions


def _refs_payload(db, pid: int) -> dict:
    works, editions = _references(db, pid)
    return {
        "works": [{"work_id": w, "role": r, "label": _work_label(db, w),
                   "books": _work_books(db, w)} for w, r in works],
        "editions": [{"edition_id": e} for e in editions],
    }


def _assert_links_moved(db, into_id: int, works, editions) -> None:
    """Post-MERGE guarantee that no link was LOST: every work the merged-away record
    authored is now authored by the canonical `into_id`, and every edition it
    translated is now translated by it. This is the check that actually verifies a
    merge re-pointed everything — a plain dangling-reference scan cannot, because the
    person link FKs are `ON DELETE CASCADE`, so a missed re-point silently DROPS the
    edge (the loser's row is cascade-deleted) instead of leaving it dangling. Raises
    `IntegrityError` on the first lost link; the caller rolls the whole merge back."""
    for w in works:
        if not _acc(db).works.reads.has_author_link(w["work_id"], into_id):
            raise IntegrityError(
                f"merge LOST authorship of work {w['work_id']} — not re-pointed to "
                f"person #{into_id}")
    if editions:
        now = set(cs.person_edition_ids_as_translator(db, into_id))
        missing = [e["edition_id"] for e in editions if e["edition_id"] not in now]
        if missing:
            raise IntegrityError(
                f"merge LOST translatorship of edition(s) {missing} — not re-pointed "
                f"to person #{into_id}")


# ── SPLIT ─────────────────────────────────────────────────────────────────────
def plan_split(db, pid: int, parts=None) -> dict:
    """Preview a split: each part (with the existing person it maps to, or None = new,
    and a SUGGESTED role), and the works/editions — naming work + book — that will be
    re-pointed. No mutation. The operator overrides the per-part role on apply."""
    person = _acc(db).persons.reads.get(pid)
    if not person:
        return {"error": "no such person"}
    parts = parts or split_parts(person.primary_name)
    if len(parts) < 2:
        return {"error": "nothing to split — name has no comma-separated parts"}
    refs = _refs_payload(db, pid)
    roles = [w["role"] for w in refs["works"]]
    suggested = roles[0] if roles and all(r == roles[0] for r in roles) else "author"
    mapped = []
    for p in parts:
        ex = _acc(db).persons.reads.find_by_alias_fold(p, exclude=pid)
        mapped.append({"name": p, "existing_id": ex, "role": suggested})
    return {"pid": pid, "name": person.primary_name, "parts": mapped, **refs}


def apply_split(db, pid: int, parts=None, assignments=None, *, commit: bool = True,
                record_undo: bool = False) -> dict:
    """Split the blob: resolve/create each part, attach it to ALL the blob's works
    with the role the operator chose (default = the suggested role), point each
    translator slot at the part assigned 'translator' (else clear it), delete the
    blob. `assignments` is [{name, role}] (role in author/translator).

    `record_undo=True` journals a reversible snapshot (see contributor_undo) and
    returns its token as `undo_token`."""
    plan = plan_split(db, pid, parts)
    if plan.get("error"):
        return plan
    role_by_name = {a["name"]: a.get("role") for a in (assignments or []) if a.get("name")}
    targets = []
    for part in plan["parts"]:
        role = role_by_name.get(part["name"]) or part["role"]
        if role not in ROLES:
            role = "author"
        tpid, was_new = get_or_create_person(db, part["name"])
        targets.append({"id": tpid, "name": part["name"], "role": role, "created": was_new})
    # Snapshot NOW: parts exist (created rows are real) but the blob still holds all its
    # edges and pre-existing targets are untouched, so this captures the true pre-split
    # state. Created parts have no rows to snapshot — they're in clear_ids so undo drops
    # them; the blob + existing targets are restored verbatim.
    snap = undo.snapshot_persons(
        db, [pid] + [t["id"] for t in targets if not t["created"]],
        clear_ids=[pid] + [t["id"] for t in targets]) if record_undo else None
    # The split MUTATION goes through the access-API engine, STAGED onto THIS connection (bind_conn)
    # so it's atomic with our snapshot + journal: each target is attached to the blob's works
    # (author) or their editions (translator), then the blob is detached + HARD-deleted.
    from catalogue.access_api import system_conn
    from catalogue.contracts import Ref
    system_conn(db).persons.writes.split(Ref("person", pid), targets)
    token = (undo.log_undo(db, "split", f"split of “{plan['name']}” (#{pid})", snap)
             if record_undo else None)
    if commit:
        verified_commit(db)
    return {"split": pid, "name": plan["name"],
            "into": [{"id": t["id"], "name": t["name"], "role": t["role"]} for t in targets],
            "created": [{"id": t["id"], "name": t["name"]} for t in targets if t["created"]],
            "works_repointed": plan["works"], "editions_repointed": plan["editions"],
            "undo_token": token}


# ── DELETE ──────────────────────────────────────────────────────────────────────
def plan_delete(db, pid: int) -> dict:
    """Preview a delete: the works detached and editions whose translator slot is
    cleared. No mutation."""
    person = _acc(db).persons.reads.get(pid)
    if not person:
        return {"error": "no such person"}
    return {"pid": pid, "name": person.primary_name, **_refs_payload(db, pid)}


def apply_delete(db, pid: int, *, commit: bool = True, record_undo: bool = False) -> dict:
    """Soft-delete the person: the access engine TOMBSTONES the row (`deleted_at`, id
    frozen) and its aliases / external-ids / edges ride along — nothing is purged, so the
    works the person authored simply lose a *live* author (the engine's orphan policy flags
    them) and a restore makes them whole again. Unlike merge/split (absorption → hard
    delete), a curated delete is reversible by a flag-flip.

    `record_undo=True` journals a reversible `person_tombstone` snapshot (see
    contributor_undo) and returns its token as `undo_token`."""
    plan = plan_delete(db, pid)
    if plan.get("error"):
        return plan
    # The delete MUTATION goes through the access-API engine, STAGED onto THIS connection
    # (bind_conn) so it's atomic with the undo journal below; the caller commits.
    from catalogue.access_api import system_conn
    from catalogue.contracts import Ref
    acc = system_conn(db)
    impact = acc.persons.writes.plan_delete(Ref("person", pid))
    if not impact.appliable:
        return {"error": (impact.blocks[0].message if impact.blocks
                          else f"cannot delete person #{pid}")}
    acc.persons.writes.apply(impact)                 # tombstone, staged on db
    token = (undo.log_undo(db, "delete", f"deletion of “{plan['name']}” (#{pid})",
                           undo.snapshot_tombstone(db, pid))
             if record_undo else None)
    if commit:
        verified_commit(db)
    return {"deleted": pid, "name": plan["name"],
            "works_detached": plan["works"], "editions_detached": plan["editions"],
            "undo_token": token}


# ── MERGE ─────────────────────────────────────────────────────────────────────
def _person_brief(db, pid: int) -> dict:
    acc = _acc(db)
    p = acc.persons.reads.get(pid)
    if not p:
        return {}
    aliases = [text for _aid, text, _scheme in acc.persons.reads.aliases(pid)]
    return {"id": pid, "name": p.primary_name, "dates": p.dates, "external_id": p.external_id,
            "status": p.verification_status, "aliases": aliases}


def plan_merge(db, pid: int, into_id: int, *, allow_cross_authority: bool = False) -> dict:
    """Preview folding duplicate `pid` into canonical `into_id`: the works/editions
    that move, the aliases gained, and how dates/external_id resolve. No mutation.

    By default the merge is REFUSED when the two rows carry different `external_id`
    strings (the operator must unbind one first — a safety rail against fusing two
    distinct authorities). `allow_cross_authority=True` lifts that rail for callers
    that have ALREADY established the two ids are equivalent (person_dedup's
    union-find over cross-links). The caller owns the equivalence decision; this
    function never does network. See authority_dedup_model.md §6."""
    if pid == into_id:
        return {"error": "cannot merge a person into itself"}
    dup = _person_brief(db, pid)
    canon = _person_brief(db, into_id)
    if not dup:
        return {"error": f"the record you're merging from (#{pid}) no longer exists "
                         f"— it was already merged or deleted"}
    if not canon:
        return {"error": f"no such merge target (#{into_id})"}
    if (dup["external_id"] and canon["external_id"]
            and dup["external_id"] != canon["external_id"]
            and not allow_cross_authority):
        return {"error": f"both bound to different authorities "
                         f"({dup['external_id']} vs {canon['external_id']}) — "
                         "unbind one before merging"}
    canon_keys = {fold_key(a) for a in canon["aliases"]}
    aliases_gained = [a for a in dup["aliases"] if fold_key(a) not in canon_keys]
    return {"dup": dup, "canon": canon, "aliases_gained": aliases_gained,
            "dates_after": canon["dates"] or dup["dates"],
            "external_id_after": canon["external_id"] or dup["external_id"],
            **_refs_payload(db, pid)}


def apply_merge(db, pid: int, into_id: int, *, keep_name_alias: bool = True,
                allow_cross_authority: bool = False, commit: bool = True,
                record_undo: bool = False) -> dict:
    """Fold duplicate `pid` into canonical `into_id` and report what moved. Reuses the
    shared `names._merge_person` (repoint contributor + translator edges, move aliases
    deduped, keep dates) and additionally carries over the duplicate's external ids.

    `keep_name_alias` (default True) governs the duplicate's DISPLAY NAME: True
    guarantees it survives as an alias of the winner (added if it wasn't already one
    of the dup's alias rows — so a merged-away name is never lost); False ensures the
    dup's name is NOT kept as an alias of the winner."""
    plan = plan_merge(db, pid, into_id, allow_cross_authority=allow_cross_authority)
    if plan.get("error"):
        return plan
    # Snapshot both rows BEFORE any mutation so the merge is fully reversible.
    snap = undo.snapshot_persons(db, [pid, into_id]) if record_undo else None
    # The merge MUTATION now goes through the access-API engine, STAGED onto THIS connection
    # (bind_conn) so it's atomic with our snapshot + journal below — the caller (us) owns the commit.
    # The engine re-points every contributor edge + aliases (keep_name_alias) + external-ids (+ empty
    # backfill) + the person-owned non-FK refs, then HARD-deletes the loser. NOTE: it ALSO re-points
    # the loser's pending review-item / promotion refs onto the winner (the old path left those for
    # the dangling-ref sweep); the row snapshot doesn't carry them, so an undo restores the person rows
    # + edges but leaves a re-pointed review item on the winner — a minor attribution drift, not a
    # dangling ref.
    from catalogue.access_api import system_conn
    from catalogue.contracts import Ref
    system_conn(db).persons.writes.merge(
        Ref("person", pid), Ref("person", into_id),
        keep_name_alias=keep_name_alias, allow_cross_authority=allow_cross_authority)
    # Atomic post-condition. The links the LOSER held (captured in `plan` before the
    # merge) must now be the canonical's; plus a generic dangling-reference scan when
    # we own the commit. ANY failure rolls the WHOLE op back so nothing is half-merged
    # — and re-raises for commit=False callers (bulk / batch / on-bind) to abort on.
    try:
        _assert_links_moved(db, into_id, plan["works"], plan["editions"])
        if commit:
            assert_integrity(db)
    except IntegrityError:
        db.rollback()
        raise
    # Journal the undo entry inside the op's own transaction — a rolled-back merge
    # (above) leaves no entry, and the snapshot commits atomically with the merge.
    token = (undo.log_undo(db, "merge",
                           f"merge of “{plan['dup']['name']}” into “{plan['canon']['name']}”",
                           snap) if record_undo else None)
    if commit:
        db.commit()
    return {"merged": pid, "name": plan["dup"]["name"], "into": into_id,
            "into_name": plan["canon"]["name"], "aliases_gained": plan["aliases_gained"],
            "works_repointed": plan["works"], "editions_repointed": plan["editions"],
            "external_id": plan["external_id_after"], "undo_token": token}
