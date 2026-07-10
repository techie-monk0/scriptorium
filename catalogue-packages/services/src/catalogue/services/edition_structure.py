"""Single-vs-multi-work classification per edition.

A "work" here is a Sanskrit/Tibetan root text or commentary. An edition is
`single_work` if it holds one such text (the common case — most modern books are
one translation) and `multi_work` if it holds several (an anthology, or a root +
commentary bundle). The operator sets this (the /editions/structure checkbox
tool); it drives which detection runs downstream: single-work editions get the
single-Skt/Tib autodetect, multi-work editions get the segmentation pass.

The value lives in `edition.structure` ('single_work' | 'multi_work' | NULL).
`proposal_guess` seeds a starting classification from the autodetected
`book_toc_pattern` structure so the operator corrects rather than starts blank.
"""
import json

VALUES = ("single_work", "multi_work")


def _acc(db):
    """A system Access over this connection (engine-routed edition reads/writes)."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def proposal_guess(db) -> dict:
    """`{edition_id: 'single_work'|'multi_work'}` guessed from each edition's
    autodetected proposal. The proposal's `multi_work` → multi_work; everything
    else (incl. `collection_unsegmented`, which the audit showed is mostly
    misfiled single works) → single_work."""
    out: dict = {}
    for raw in _acc(db).review.reads.payloads_by_type("book_toc_pattern"):
        try:
            p = json.loads(raw)
        except (TypeError, ValueError):
            continue
        hid = p.get("holding_id")
        if hid is None:
            continue
        h = _acc(db).holdings.reads.get(hid)
        if not h:
            continue
        guess = "multi_work" if p.get("structure") == "multi_work" else "single_work"
        # multi_work wins if any proposal for the edition says so
        if out.get(h.edition_id) != "multi_work":
            out[h.edition_id] = guess
    return out


def list_editions(db):
    """Every edition with its current `structure`, the proposal guess, contained-work
    count, and a holding file path (for the viewer link). Title-sorted."""
    guess = proposal_guess(db)
    rows = _acc(db).editions.reads.list_with_structure()
    return [{"id": r[0], "title": r[1], "structure": r[2],
             "guess": guess.get(r[0]), "n_works": r[3], "holding_id": r[4]}
            for r in rows]


def set_structure(db, edition_id: int, value) -> None:
    """Set (or clear, with value=None) one edition's structure."""
    if value not in (None, *VALUES):
        raise ValueError(f"structure must be one of {VALUES} or None, got {value!r}")
    _acc(db).editions.writes.set_structure(edition_id, value)


def set_many(db, *, multi_ids=(), single_ids=()) -> int:
    """Mark the given editions multi/single in one go; returns rows touched."""
    n = 0
    for eid in multi_ids:
        set_structure(db, int(eid), "multi_work"); n += 1
    for eid in single_ids:
        set_structure(db, int(eid), "single_work"); n += 1
    return n


def seed_from_proposals(db, *, only_unset: bool = True) -> int:
    """Bulk-apply the proposal guess to `edition.structure`. By default only fills
    editions with no structure yet (won't clobber operator choices). Returns count."""
    guess = proposal_guess(db)
    n = 0
    for eid, g in guess.items():
        if only_unset:
            if _acc(db).editions.reads.structure_of(eid):
                continue
        set_structure(db, eid, g)
        n += 1
    return n
