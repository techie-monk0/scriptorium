"""Work entity module — the FRBR composition root (shared across editions; hub of the work graph).

`WorkRepo` wires the access layer (reads/writes) to a `WorkStore` implementation — SQLite by default,
injectable (fake for tests, HTTP adapter later). `.reads` (queries, READ action) and `.writes`
(plan→apply `delete` + `merge`, WRITE action) are distinct surfaces. See entity_api_model.md §3/§6.
"""
from .reads import WorkReader
from .store import SqliteWorkStore, WorkStore
from .writes import WorkWriter

__all__ = ["WorkRepo", "WorkStore", "SqliteWorkStore"]


class WorkRepo:
    def __init__(self, access, store: "WorkStore | None" = None):
        store = store or SqliteWorkStore(access)
        self.reads = WorkReader(access, store)
        self.writes = WorkWriter(access, store)
