"""Non-FK reference registry — where each entity's id/keys appear OUTSIDE a foreign key.

Declared, not discovered: a new non-FK store can't silently re-introduce the orphan bug
(it must be added here, which makes delete-purge + the OrphanSweep see it). See the orphan
audit and docs/access/entity_api_model.md §6.
"""
from __future__ import annotations

import os
import re

# Caches keyed by a holding's `file_hash`. These are NOT foreign keys, so no DB cascade
# reaches them — they must be purged here. Purged only when the LAST holding referencing a
# given file_hash is deleted (two holdings of the same file share its caches).
HOLDING_FILE_HASH_CACHES: tuple[str, ...] = (
    "raw_extract_cache",
    "parsed_toc_cache",
    "page_text_cache",
    "section_cache",
)

# Cover/spine/pin files id-keyed to an edition (`e<id>*`). NOT foreign keys — no cascade
# reaches them, so a recycled edition id would inherit the deleted book's art (orphan-audit
# #3). Mirrors services.covers.purge_edition_art's key scheme, declared here so the access
# layer owns the edition's file closure without importing services (which sits above it).
EDITION_ART_EXTS: tuple[str, ...] = (".jpg", ".png", ".gif", ".svg", ".miss", ".part")


def edition_art_files(cover_cache, cover_pinned, eid) -> list[str]:
    """Every cover/spine/pin path an edition could own. Caller filters to those that exist."""
    out: list[str] = []
    if cover_cache:
        for key in (f"e{eid}", f"spine-e{eid}"):
            out += [os.path.join(cover_cache, key + ext) for ext in EDITION_ART_EXTS]
    if cover_pinned:
        out += [os.path.join(cover_pinned, f"e{eid}" + ext) for ext in EDITION_ART_EXTS]
    return out


# The reverse of `edition_art_files`: pull the edition id back out of an art filename. One place
# owns the `e<id>` scheme so the OrphanSweep (which reconciles art dirs against live editions) and
# the cli cover-sweep agree by construction. Matches `e<id><ext>`, `spine-e<id><ext>`, and the
# `.miss` / `.part` markers.
_ART_NAME_RE = re.compile(r"^(?:spine-)?e(\d+)\.[^.]+(?:\.part)?$")


def edition_id_from_art_name(name: str) -> "int | None":
    """The edition id encoded in a cover/spine/pin art filename, or None if `name` isn't art."""
    m = _ART_NAME_RE.match(name)
    return int(m.group(1)) if m else None


# ── owning-entity registry for non-FK JSON refs (review_queue / promotion) ──────────
#
# A `review_queue` item embeds entity ids inside its `payload_json` (no FK), so a delete/merge must
# scrub the items it OWNS. Ownership is the decision the accept-path mutates — NOT every id the
# payload happens to mention. `work_id` in particular is only ever a SECONDARY ref: the two types
# that carry it (`title_proposal`, `edition_metadata`) are EDITION-owned (accept mutates
# `edition.title`), so matching on a bare `work_id == wid` wrongly purged ~254 proposals for LIVE
# editions when a work was deleted (the orphan-audit tail #1 over-purge). Declaring ownership here
# fixes it by construction: a work delete touches only WORK-owned types.
#
# item_type → (owning entity kind, the payload key carrying that owner's id).
REVIEW_ITEM_OWNERS: dict[str, tuple[str, str]] = {
    "work_authorship":    ("work", "work_id"),
    "work_canonical":     ("work", "work_id"),
    "person_authority":   ("person", "person_id"),
    "person_work_joint":  ("person", "person_id"),
    "title_proposal":     ("edition", "edition_id"),
    "edition_metadata":   ("edition", "edition_id"),
    "edition_dedup":      ("edition", "edition_id"),
    "book_toc_pattern":   ("holding", "holding_id"),
    "ingest":             ("holding", "holding_id"),
}

# promotion JSON id-arrays, keyed by owning entity → the trusted column literal. A root's delete
# scrubs its own id from the matching array (never another entity's).
PROMOTION_ID_ARRAYS: dict[str, str] = {
    "work": "work_ids",
    "person": "person_ids",
}


def review_items_owned_by(entity_kind: str) -> tuple[tuple[str, str], ...]:
    """The `(item_type, payload_key)` pairs an entity of `entity_kind` OWNS — the only review items
    its delete/merge may scrub or re-point. Empty for an entity that owns no review decision."""
    return tuple((t, key) for t, (owner, key) in REVIEW_ITEM_OWNERS.items() if owner == entity_kind)
