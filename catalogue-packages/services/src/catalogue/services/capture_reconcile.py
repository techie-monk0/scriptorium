"""Auto-clear captured scans the catalogue already holds — a WRITE-side operation (NOT on read).

A scan sits in the capture inbox (`status='raw'`) until a human resolves it. But once the
catalogue actually holds the book — it was already owned at scan time, or got catalogued
afterwards — there is nothing left to resolve. This reconciler marks every such scan
`resolved`, so it leaves the staging worklist AND the home Capture pill. It still shows in the
capture log's "Added" section (which counts `status='resolved'` rows) as a short-lived receipt.

Local-only (reuses `intake_match.editions_now_holding`, the SAME held-now check the "Added"
section uses — exact ISBN across edition/holding/edition_isbn, plus title + shared-author). Run
from the events that grow the catalogue so the inbox stays truthful without a read-path write:
  • after a filesystem sweep/ingest (a scanned book may now have a holding),
  • after capture-resolve / add-by-upload creates an edition,
  • (the per-scan capture path clears an already-owned scan at scan time — see capture routes).
"""
from __future__ import annotations

import json

from catalogue.access_api import system_conn
from catalogue.services import intake_match


def reconcile_captures(db, *, limit: int = 1000) -> int:
    """Resolve every open ('raw') capture the catalogue now holds. Best-effort per row; returns
    how many scans were cleared. Stages on `db` and commits once if anything changed."""
    acc = system_conn(db)
    cleared = 0
    for sid, isbn, meta_json in acc.capture.raw_with_meta(limit):
        meta = None
        if meta_json:
            try:
                meta = json.loads(meta_json)
            except (ValueError, TypeError):
                meta = None
        try:
            held = intake_match.editions_now_holding(db, isbn=isbn or None, meta=meta)
        except Exception:
            held = []
        if held:
            acc.capture.resolve(sid)     # already in the catalogue → out of the inbox
            cleared += 1
    if cleared:
        db.commit()
    return cleared
