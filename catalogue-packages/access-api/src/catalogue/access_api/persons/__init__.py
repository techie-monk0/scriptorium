"""Person entity module — the contributor authority root (author/translator of works + editions).

`PersonRepo` wires the access layer (reads/writes) to a `PersonStore` implementation — SQLite by
default, injectable (fake for tests, HTTP adapter later). `.reads` (queries, READ action) and
`.writes` (plan→apply soft-delete with the identity guard + semantic-orphan policy, WRITE action)
are distinct surfaces. See entity_api_model.md §3/§6.
"""
from .reads import PersonReader
from .store import PersonStore, SqlitePersonStore
from .writes import PersonWriter

__all__ = ["PersonRepo", "PersonStore", "SqlitePersonStore"]


class PersonRepo:
    def __init__(self, access, store: "PersonStore | None" = None):
        store = store or SqlitePersonStore(access)
        self.reads = PersonReader(access, store)
        self.writes = PersonWriter(access, store)
