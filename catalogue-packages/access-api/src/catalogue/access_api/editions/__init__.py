"""Edition entity module — the published-manifestation root (parent of holdings, linked to works).

`EditionRepo` wires the access layer (reads/writes) to an `EditionStore` implementation — SQLite by
default, injectable (fake for tests, HTTP adapter later). `.reads` (queries, READ action) and
`.writes` (plan→apply delete with FK cascade + orphan policy + cover-art purge, WRITE action) are
distinct surfaces. See entity_api_model.md §3/§6.
"""
from .reads import EditionReader
from .store import EditionStore, SqliteEditionStore
from .writes import EditionWriter

__all__ = ["EditionRepo", "EditionStore", "SqliteEditionStore"]


class EditionRepo:
    def __init__(self, access, store: "EditionStore | None" = None):
        store = store or SqliteEditionStore(access)
        self.reads = EditionReader(access, store)
        self.writes = EditionWriter(access, store)
