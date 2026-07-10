"""Reconcile the wishlist against the catalogue — a WRITE-side operation (NOT run on read).

Called from the events that actually change the catalogue, so `GET /api/v1/wishlist` stays purely
read-only:
  • after a filesystem sweep/ingest (a wishlisted book may now have a holding),
  • after an edition is deleted (an `acquired` item must not dangle on a dead edition),
  • (the per-scan capture acquisition loop covers the capture path separately).

For each live wishlist item it makes one of four LOCAL-only (no network) decisions:
  • confident match (exact ISBN, or title + full-author agreement) → `acquired`;
  • previously acquired but the book is GONE → reverted to the active wishlist (no orphan);
  • weak match (similar title + a shared author, different/again no ISBN) → `suspected`, with the
    candidate editions stored so the operator is ASKED "is this the same book?" next time they look;
  • nothing → left as-is.
"""
from __future__ import annotations

from catalogue.access_api import system_conn
from catalogue.services import intake_match


def reconcile_acquisitions(db) -> int:
    """Apply the reconciliation above to every live wishlist item. Best-effort per item; returns how
    many items changed. Stages on `db` and commits once if anything changed."""
    acc = system_conn(db)
    changed = 0
    for it in acc.wishlist.list():
        title = it.title or it.raw_title
        meta = {"title": title, "authors": list(it.authors)} if title else None
        try:
            confirmed = intake_match.editions_now_holding(db, isbn=it.isbn, meta=meta)
        except Exception:
            confirmed = []

        if it.status == "acquired":
            matched_live = (it.matched_edition_id is not None
                            and acc.editions.reads.get(it.matched_edition_id) is not None)
            if not matched_live and not confirmed:
                acc.wishlist.revert_acquired(
                    it.id, "resolved" if (it.title or it.isbn) else "unresolved")
                changed += 1
            continue

        if confirmed:
            if it.status != "acquired":
                acc.wishlist.mark_acquired(it.id, confirmed[0]["id"])
                changed += 1
            continue

        # No confident match — is there a weak suspect to ASK about? Only promote a resolved/
        # unresolved item to 'suspected' (don't disturb 'ambiguous'/'suspected' already mid-decision).
        if it.status in ("resolved", "unresolved"):
            try:
                suspects = intake_match.suspected_editions(db, meta)
            except Exception:
                suspects = []
            if suspects:
                acc.wishlist.resolve(it.id, {"candidates": suspects}, "suspected")
                changed += 1

    if changed:
        db.commit()
    return changed
