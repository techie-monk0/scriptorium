"""Where an authored PDF outline lives — the storage seam, kept separate from writing it into the file.

Authoring is decoupled from persistence: the reader edits "Contents" entries; an `OutlineStore` keeps
them; `outline_export.OutlineWrite` bakes a given list into the PDF bytes on demand. Splitting the two
is what lets the *storage implementation change later* without touching the reader that authors or the
writer that bakes.

The intended first adapter is an **overlay-of-record** synced like bookmarks/annotations: entries live
beside the file (never in it), so authoring is cheap, works offline, and merges across devices through
the sync machinery already built — and the file is only rewritten when the user explicitly bakes the
outline in (via the shared `pdf_mutation.write_pdf`). A later adapter could instead read/write the
PDF's own outline directly (`services.toc.extract_pdf_outline` for the read side), or use a different
backend; callers depend only on this Protocol, so swapping it is local.

### Technical details

An entry is a plain mapping `{"level": int>=1, "title": str, "page": int (1-based)}` — the same shape
`outline_export._entry_fields` accepts, so a store's output feeds `OutlineWrite` directly. `get_outline`
returns the authored entries for a holding (empty list if none); `set_outline` replaces them wholesale
(mirroring `set_toc` semantics, so author/bake stay consistent).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class OutlineStore(Protocol):
    """Persist a holding's authored outline. Adapters: in-memory (below), the reader sync-of-record
    (`ReaderStateOutlineStore`, synced like bookmarks), or the file's own outline. The reader and the
    bake route depend on this, never on an adapter."""

    def get_outline(self, holding_id: int) -> list[dict]: ...
    def set_outline(self, holding_id: int, entries: list[dict]) -> None: ...


class InMemoryOutlineStore:
    """Reference adapter for dev/tests. A deployment swaps in the synced-overlay (or file-backed)
    adapter without any caller change — that is the point of the `OutlineStore` seam."""

    def __init__(self) -> None:
        self._by_holding: dict[int, list[dict]] = {}

    def get_outline(self, holding_id: int) -> list[dict]:
        return [dict(e) for e in self._by_holding.get(holding_id, [])]

    def set_outline(self, holding_id: int, entries: list[dict]) -> None:
        self._by_holding[holding_id] = [dict(e) for e in entries]


def outline_op_id(holding_id: int) -> str:
    """The stable, per-copy id both server and client use for a holding's outline, so two devices
    editing the same copy's outline converge on one LWW row (the outline is one document per copy)."""
    return f"outline:holding:{holding_id}"


class ReaderStateOutlineStore:
    """`OutlineStore` backed by the reader sync-of-record (`catalogue.db_store.reader_state`), so an
    authored outline is the SAME DB-of-record + `/sync/reader`-synced overlay as bookmarks — offline
    and multi-device for free, and never written into the file until an explicit bake. `get_outline`
    reads the copy's live outline; `set_outline` upserts it (LWW, stable per-copy id). The caller
    commits — the reader-state store never does (matching `apply_bookmark` et al.)."""

    def __init__(self, store):
        self._store = store

    def get_outline(self, holding_id: int) -> list[dict]:
        row = self._store.outline_for_holding(holding_id)
        if row is None or not row.entries:
            return []
        try:
            data = json.loads(row.entries)
        except (ValueError, TypeError):
            return []
        return [dict(e) for e in data] if isinstance(data, list) else []

    def set_outline(self, holding_id: int, entries: list[dict], *, updated_at: str | None = None) -> None:
        self._store.apply_outline(
            id=outline_op_id(holding_id), holding_id=holding_id,
            entries=json.dumps([dict(e) for e in entries]),
            updated_at=updated_at or datetime.now(timezone.utc).isoformat())
