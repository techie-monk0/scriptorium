"""Work merge — fold a duplicate `work` row into a canonical one (FRBR dedup).

The analog of `contributor_edit`/`names._merge_person`, but for WORKS. The FRBR
model makes the `work` shared across editions (one composition → many
translations); today every edition mints its own work, so duplicate compositions
exist as separate rows. Merging them is where back-catalogue multiplicity comes
from. This module re-points every edge that references the loser work onto the
winner, moves its aliases (deduped on fold-key), carries any canonical/native-title
fields the winner lacks, and deletes the loser.

`plan_merge` previews the effect WITHOUT mutating (for the confirm step);
`apply_work_merge` performs it and returns the same shape plus what moved. Every
reference is re-pointed explicitly (not relied on via cascade) so the report is
accurate, mirroring contributor_edit's contract.

⚠ A multi-VOLUME set (Lamrim Chenmo vols 1–3, …) is NOT a duplicate — group it with
catalogue/work_dedup.group_volume_set instead. Merging volumes destroys the set.

Tables re-pointed off the loser work: edition_work, work_contributor, work_alias,
relationship (both ends), collection_member, work_subject, work_tradition.
"""
from __future__ import annotations

from catalogue.db_store import fold_key
from catalogue.db_store.integrity import verified_commit


def _acc(db):
    """A system Access over this connection — the work-merge engine (`acc.works.writes`),
    which re-points every loser edge + the non-FK review/promotion closure and HARD-deletes
    the loser (absorption). This service keeps the legacy report shape its callers expect."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def _work_brief(db, wid: int) -> dict:
    """Title, all aliases, canonical id, author person-ids, and the editions the
    work appears in — enough for the operator to judge a merge."""
    acc = _acc(db)
    w = acc.works.reads.get(wid)
    if w is None:
        return {}
    aliases = [t for t, _scheme in acc.works.reads.aliases(wid)]
    title = aliases[0] if aliases else f"work#{wid}"
    editions = [{"edition_id": e.id, "title": e.title}
                for e in acc.editions.reads.by_work(wid)]
    return {"id": wid, "title": title, "aliases": aliases,
            "canonical_system": w.canonical_system, "canonical_number": w.canonical_number,
            "author_person_ids": list(w.author_ids), "editions": editions}


def author_set(db, wid: int) -> frozenset:
    """The set of author person-ids on a work (role='author'). The dedup pass keys
    on this alongside fold_key(title) — same title + same authors ⇒ Tier-2 candidate."""
    w = _acc(db).works.reads.get(wid)
    return frozenset(w.author_ids) if w else frozenset()


def plan_merge(db, wid: int, into_id: int) -> dict:
    """Preview folding duplicate `wid` into canonical `into_id`: the editions/
    contributors/relationships that move, the aliases gained, and how the canonical
    fields resolve. No mutation. Guards self-merge and conflicting canonical ids."""
    if wid == into_id:
        return {"error": "cannot merge a work into itself"}
    dup = _work_brief(db, wid)
    canon = _work_brief(db, into_id)
    if not dup:
        return {"error": f"no such work #{wid}"}
    if not canon:
        return {"error": f"no such work #{into_id} (merge target)"}
    dn, cn = dup["canonical_number"], canon["canonical_number"]
    if dn and cn and dn != cn:
        return {"error": f"both carry different canonical numbers "
                         f"({dup['canonical_system']}:{dn} vs "
                         f"{canon['canonical_system']}:{cn}) — resolve before merging"}
    canon_keys = {fold_key(a) for a in canon["aliases"]}
    aliases_gained = [a for a in dup["aliases"] if fold_key(a) not in canon_keys]
    return {
        "dup": dup, "canon": canon, "aliases_gained": aliases_gained,
        "canonical_after": (canon["canonical_system"] or dup["canonical_system"],
                            cn or dn),
        "editions_repointed": dup["editions"],
    }


def apply_work_merge(db, wid: int, into_id: int, *, commit: bool = True) -> dict:
    """Fold duplicate work `wid` into canonical `into_id`; report what moved.

    The mutation runs through the work-merge ENGINE (`acc.works.writes` plan→apply,
    staged on this connection): it re-points edition_work / work_author / relationship /
    edition_commentary_on / collection_member / work_subject / work_tradition, moves
    work_alias (deduped on fold-key), re-points the WORK-owned review/promotion non-FK
    refs, backfills the winner's empty canonical/native-title fields, and HARD-deletes
    the loser. (The engine adds the edition_commentary_on + non-FK closure the old inline
    path left dangling — a correctness upgrade.) The legacy report shape is preserved.
    Idempotent-ish: a second call errors with 'no such work'."""
    plan = plan_merge(db, wid, into_id)
    if plan.get("error"):
        return plan
    acc = _acc(db)
    loser, winner = acc.works.reads.get(wid), acc.works.reads.get(into_id)
    if loser is None or winner is None:                # raced away since plan_merge read it
        return {"error": f"no such work #{wid if loser is None else into_id}"}
    impact = acc.works.writes.plan_merge(loser.ref(), winner.ref())
    if not impact.appliable:
        return {"error": "; ".join(b.message for b in impact.blocks)}
    acc.works.writes.apply(impact)                     # stages on this conn (commit is the caller's)

    if commit:
        verified_commit(db)
    return {"merged": wid, "title": plan["dup"]["title"], "into": into_id,
            "into_title": plan["canon"]["title"], "aliases_gained": plan["aliases_gained"],
            "editions_repointed": plan["editions_repointed"],
            "canonical_after": plan["canonical_after"]}
