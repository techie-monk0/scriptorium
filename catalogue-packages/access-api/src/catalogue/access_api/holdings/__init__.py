"""Holding entity module — the file-bearing leaf aggregate (owns file + provenance).

`HoldingRepo` wires the access layer (reads/writes) to a `HoldingStore` implementation — SQLite by
default, but injectable (a fake for tests, an HTTP adapter later). `.reads` (queries, READ action)
and `.writes` (plan→apply, WRITE action) are distinct surfaces, so "can this caller write?" is
answered at the surface, not per call. See entity_api_model.md §3.
"""
from .reads import HoldingReader
from .store import HoldingStore, SqliteHoldingStore
from .writes import HoldingWriter

__all__ = ["HoldingRepo", "HoldingStore", "SqliteHoldingStore"]


class HoldingRepo:
    def __init__(self, access, store: "HoldingStore | None" = None):
        store = store or SqliteHoldingStore(access)
        self.reads = HoldingReader(access, store)
        self.writes = HoldingWriter(access, store)
