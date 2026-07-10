"""SQLite connection chokepoint — the SOLE module that opens a raw connector.

Every connection to a catalogue DB is opened here, so PRAGMA + FK enforcement is
centralized and `tests/test_integrity.py` can assert nothing else calls the raw
connector. This is also where the **read/write split** is rooted:

  • `connect` / `connect_rw` — read-write (FK ON, WAL, busy_timeout); the default.
  • `connect_ro`            — OS-enforced read-only (`mode=ro`): any write raises, so a
                              reader/preview path *physically cannot* mutate the DB.

The future import-linter `forbidden` contract targets THIS module precisely: only
`access_api` may import it, so all other packages must go through the typed reader/writer
surfaces rather than opening their own connection. (`db.py` re-exports these for the
long-standing `from catalogue.db_store import connect` imports.)
"""
from __future__ import annotations

import os
from pathlib import Path

# Prefer stdlib sqlite3; fall back to pysqlite3 only if needed (§4.5).
try:
    import sqlite3 as _sqlite  # type: ignore
    _SQLITE_SOURCE = "stdlib"
except ImportError:  # pragma: no cover — stdlib always present
    import pysqlite3 as _sqlite  # type: ignore
    _SQLITE_SOURCE = "pysqlite3"


def connect(db_path: str | os.PathLike) -> "_sqlite.Connection":
    """Open a read-write connection with PRAGMAs set. **Does not** run the init gate —
    the gate is a startup check (DDL probe), not a per-request one; running it on every
    connect serializes concurrent connections under WAL.
    """
    # Guard the #1 silent-data-loss footgun: passing an OPEN connection (or any
    # non-path) where a path is expected. `str(connection)` is
    # '<sqlite3.Connection object at 0x…>', so _sqlite.connect would happily CREATE
    # a phantom DB file by that name and writes would vanish into it. Reject early.
    if isinstance(db_path, _sqlite.Connection):
        raise TypeError(
            "connect() received an open sqlite3.Connection, not a path — this would "
            "create a phantom DB file named '<sqlite3.Connection object at 0x…>' and "
            "silently send writes there. Pass the DB PATH, or reuse the connection directly.")
    if not isinstance(db_path, (str, os.PathLike)):
        raise TypeError(f"connect() expects a path, got {type(db_path).__name__}: {db_path!r}")

    conn = _sqlite.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    # Self-verify the switch actually took: `PRAGMA foreign_keys=ON` is silently a
    # no-op on a SQLite built without FK support. Without enforcement, cascades
    # don't fire and bad inserts aren't rejected → silent referential corruption.
    # Refuse the connection rather than hand back one that can't protect the data.
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise RuntimeError(
            "Refusing connection: SQLite did not enable foreign_keys "
            "(this build lacks FK enforcement). Referential integrity would be "
            "unprotected — cascades won't fire and invalid links won't be rejected.")
    conn.execute("PRAGMA journal_mode = WAL")
    # Wait (instead of failing instantly) when another connection holds the
    # write lock — e.g. the staging load pass committing while the web app
    # accepts a manual entry. WAL gives many readers + one writer; the timeout
    # makes the brief writer-vs-writer overlaps queue politely. 30s covers a long
    # bulk-import transaction holding the single write lock while the web app
    # accepts an edit — short of that, the loser hit 'database is locked'.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# Explicit name for the read/write split. Identical to `connect` — the default surface.
connect_rw = connect


def connect_ro(db_path: str | os.PathLike) -> "_sqlite.Connection":
    """Open a **read-only** connection (`mode=ro`). Any write raises
    `OperationalError` — OS-enforced, so a reader/preview path handed one of these
    *physically cannot* mutate the DB, even with a bug. The reader side of the
    read/write split. Respects WAL + locking (unlike `immutable=1`), so it coexists
    with a live writer; sets no FK/WAL pragmas because it never writes. The file must
    already exist (you cannot create a DB read-only)."""
    if isinstance(db_path, _sqlite.Connection):
        raise TypeError("connect_ro() expects a path, not an open sqlite3.Connection.")
    if not isinstance(db_path, (str, os.PathLike)):
        raise TypeError(f"connect_ro() expects a path, got {type(db_path).__name__}: {db_path!r}")
    # mode=ro cannot create a DB, so a missing file yields SQLite's opaque "unable to
    # open database file". The catalogue DB is user-supplied (not in the repo), so a
    # missing file is a first-run condition worth an actionable message.
    from .paths import require_db
    require_db(db_path)
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"   # as_uri handles escaping
    conn = _sqlite.connect(uri, uri=True)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def new_export_db(path: str | os.PathLike) -> "_sqlite.Connection":
    """Raw connection for BUILDING a fresh, standalone, NON-catalogue SQLite file — e.g.
    the offline content-search index (export_content_index.py) the PWA downloads. This is
    NOT the catalogue DB: no FK enforcement, no WAL (a rollback-journal file is
    self-contained, so the built bytes are complete after commit+close — WAL would strand
    data in a -wal sidecar that the byte-reader would miss), and no init gate.

    Centralized here so the 'only connection.py opens the raw sqlite connector' convention
    holds (see tests/test_integrity.py::test_all_db_access_goes_through_connect)."""
    return _sqlite.connect(str(path))
