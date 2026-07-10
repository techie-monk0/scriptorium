"""catalogue/store.py — the single sanctioned write API for the catalogue DB.

Every mutation should go through a `Store`. It gives two guarantees a raw
``sqlite3`` connection cannot, and keeps the checks in ONE place instead of
scattered across every call site:

  1. **Schema conformance.** A `Store` can refuse to operate on a DB that is
     missing any table/column/index the current code expects. This is the guard
     for the bug that lost binds: ``person.harvest_incomplete`` was declared in
     ``schema.sql`` but absent from a long-lived DB, so every bind wrote to a
     column that did not exist, threw, and was silently rolled back.

  2. **Write post-conditions.** `write()` asserts the statement changed the
     number of rows the caller intended. A mutation that matches nothing — a
     vanished row, a rolled-back transaction, a typo'd WHERE — raises
     `WriteError` instead of passing for a success. "The intended change took
     place" is verified at the one chokepoint every write flows through.

It is also the seam for swapping the storage backend later: callers depend on
`Store`, not on ``sqlite3``. A `Store` is a transparent proxy over the
connection (it forwards ``execute``/``commit``/``rollback``/… ), so existing
``db.execute(...)`` reads keep working unchanged while writes migrate to
``store.write(...)`` over time.
"""
from __future__ import annotations

from . import db as _db


class WriteError(AssertionError):
    """A write did not affect the number of rows the caller declared it would.

    Distinct from `sqlite3` errors (which signal a malformed/illegal statement):
    `WriteError` means the statement ran fine but the *effect* was wrong — the
    row wasn't there, the value didn't take, the change was rolled back. This is
    the "did the intended change actually happen?" check."""


def _check_rows(actual: int, expect) -> bool:
    """Does `actual` rowcount satisfy the `expect` contract?

    `expect` is one of:
      * int          — exactly this many rows
      * (lo, hi)      — lo ≤ actual ≤ hi  (hi=None ⇒ unbounded above)
      * None          — no assertion (caller explicitly opts out, e.g. an
                        idempotent INSERT OR IGNORE that may match 0 rows)
    """
    if expect is None:
        return True
    if isinstance(expect, tuple):
        lo, hi = expect
        return actual >= lo and (hi is None or actual <= hi)
    return actual == expect


class Store:
    """Guarded write access to a catalogue connection.

    Construct around an open connection (from `db.connect`/`db.init_db`, or a
    `DryRunConnection`). `check_schema=True` (the default) verifies the DB is
    schema-current at construction; the per-request web path passes
    `check_schema=False` because startup already verified once and a full
    PRAGMA sweep per request is wasteful."""

    def __init__(self, conn, *, check_schema: bool = True):
        object.__setattr__(self, "_conn", conn)
        if check_schema:
            _db.assert_schema_current(conn)

    # ── classmethod constructors ────────────────────────────────────────────
    @classmethod
    def open(cls, db_path, *, check_schema: bool = True) -> "Store":
        """Open `db_path` (PRAGMAs set, FK-enforced) and wrap it. Does NOT run
        the init gate / migrations — use `db.init_db` for that, then wrap."""
        return cls(_db.connect(db_path), check_schema=check_schema)

    # ── the write chokepoint ────────────────────────────────────────────────
    def write(self, sql: str, params=(), *, rows=1):
        """Execute one mutation and assert it changed `rows` rows (see
        `_check_rows`). Returns the cursor (for `lastrowid`). Raises `WriteError`
        if the row-count contract is violated — the single place "did the write
        take?" is enforced."""
        cur = self._conn.execute(sql, params)
        if not _check_rows(cur.rowcount, rows):
            raise WriteError(
                f"write affected {cur.rowcount} row(s), expected {rows}: "
                f"{sql.strip().splitlines()[0]}  params={params!r}")
        return cur

    def write_many(self, sql: str, seq, *, total=None):
        """`executemany` for bulk writes. `total` (if given) asserts the summed
        rowcount; default None because per-statement effects vary."""
        seq = list(seq)
        cur = self._conn.executemany(sql, seq)
        if total is not None and not _check_rows(cur.rowcount, total):
            raise WriteError(
                f"write_many affected {cur.rowcount} row(s), expected {total}")
        return cur

    def insert(self, table: str, /, **cols):
        """INSERT one row from keyword columns; assert exactly one row landed;
        return its `lastrowid`."""
        keys = list(cols)
        placeholders = ", ".join("?" * len(keys))
        cur = self.write(
            f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({placeholders})",
            tuple(cols[k] for k in keys), rows=1)
        return cur.lastrowid

    # ── transaction control (forwarded, named for callers that hold a Store) ─
    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    @property
    def conn(self):
        """Escape hatch to the underlying connection (reads, PRAGMAs, exotic
        statements). Prefer `write()` for mutations so the post-condition runs."""
        return self._conn

    # ── transparent proxy: a Store IS a drop-in for the connection ──────────
    def __getattr__(self, name):
        # Reached only for names not defined on Store (execute, executescript,
        # cursor, …) → forward to the wrapped connection.
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False


def as_store(db) -> Store:
    """Return `db` if it is already a `Store`, else wrap it (without re-checking
    schema — the caller already holds a live connection). Lets a function that
    receives a bare connection still route a write through the guarded API
    without changing its signature:

        s = as_store(db)
        s.write("UPDATE person SET external_id=? WHERE id=?", (eid, pid), rows=1)
    """
    return db if isinstance(db, Store) else Store(db, check_schema=False)
