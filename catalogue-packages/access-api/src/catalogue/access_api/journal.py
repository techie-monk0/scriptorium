"""Row-snapshot journal + undo_log — the low-level facility the undo services route through.

`contributor_undo` / `work_undo` / `entity_undo` implement reversible operations by capturing the
verbatim pre-op rows of a set of tables, then restoring them on undo. That is generic, schema-
introspecting row surgery (PRAGMA table_info + dynamic SELECT/DELETE/INSERT over trusted table/column
constants) — BELOW the entity model, like the `Backing` port or `staging`. It is exposed here as a
TRUSTED facility (`acc.journal`) so the SQL lives in the engine, not the service layer; callers pass
their own frozen `(table, column)` specs, so there is no injection surface. `acc.undo_log` is the
undo_log table's CRUD. Writes STAGE on the caller's connection; the caller owns the transaction. See
entity_api_model.md §8.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

_J = "journal"
_U = "undo_log"


class JournalRepo:
    """Generic verbatim row capture / clear / re-insert over trusted (table, column) specs."""

    def __init__(self, access):
        self._a = access

    def _authz(self, mode):
        self._a.authorize(Action(_J, "rows", mode))

    # ── introspection ───────────────────────────────────────────────────────────
    def columns(self, table: str) -> list:
        self._authz(AccessMode.READ)
        return [r[1] for r in self._a.ro.execute(f"PRAGMA table_info({table})").fetchall()]

    def table_exists(self, table: str) -> bool:
        self._authz(AccessMode.READ)
        return self._a.ro.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    # ── capture (read) ──────────────────────────────────────────────────────────
    def capture(self, table: str, column: str, ids) -> list:
        """All-column rows of `table` where `column` IN `ids`, as dicts."""
        self._authz(AccessMode.READ)
        cols = self.columns(table)
        ph = ",".join("?" * len(ids)) or "NULL"
        rows = self._a.ro.execute(
            f"SELECT {', '.join(cols)} FROM {table} WHERE {column} IN ({ph})", list(ids)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def capture_two_col(self, table: str, col_a: str, col_b: str, ids) -> list:
        """All-column rows where `col_a` IN ids OR `col_b` IN ids (e.g. relationship)."""
        self._authz(AccessMode.READ)
        cols = self.columns(table)
        ph = ",".join("?" * len(ids)) or "NULL"
        rows = self._a.ro.execute(
            f"SELECT {', '.join(cols)} FROM {table} WHERE {col_a} IN ({ph}) OR {col_b} IN ({ph})",
            list(ids) + list(ids)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def capture_cols(self, table: str, select_cols, where_col: str, ids) -> list:
        """Specific `select_cols` of `table` where `where_col` IN ids, as dicts."""
        self._authz(AccessMode.READ)
        select_cols = list(select_cols)
        ph = ",".join("?" * len(ids)) or "NULL"
        rows = self._a.ro.execute(
            f"SELECT {', '.join(select_cols)} FROM {table} WHERE {where_col} IN ({ph})",
            list(ids)).fetchall()
        return [dict(zip(select_cols, r)) for r in rows]

    # ── mutate (staged) ─────────────────────────────────────────────────────────
    def clear(self, table: str, column: str, ids) -> None:
        """DELETE rows of `table` where `column` IN ids (FK cascades children). Staged."""
        self._authz(AccessMode.WRITE)
        ph = ",".join("?" * len(ids)) or "NULL"
        self._a.rw.execute(f"DELETE FROM {table} WHERE {column} IN ({ph})", list(ids))

    def clear_eq(self, table: str, column: str, value) -> None:
        """DELETE rows of `table` where `column` = `value`. Staged."""
        self._authz(AccessMode.WRITE)
        self._a.rw.execute(f"DELETE FROM {table} WHERE {column} = ?", (value,))

    def clear_two_col(self, table: str, col_a: str, col_b: str, ids) -> None:
        """DELETE rows where `col_a` IN ids OR `col_b` IN ids (e.g. relationship). Staged."""
        self._authz(AccessMode.WRITE)
        ph = ",".join("?" * len(ids)) or "NULL"
        self._a.rw.execute(
            f"DELETE FROM {table} WHERE {col_a} IN ({ph}) OR {col_b} IN ({ph})",
            list(ids) + list(ids))

    def insert_rows(self, table: str, rows, *, or_ignore: bool = True) -> None:
        """Re-insert captured dict rows verbatim into `table` (FK order is the caller's). Staged.
        `or_ignore` skips a row that already exists (idempotent rebuild of a shared row)."""
        self._authz(AccessMode.WRITE)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        for r in rows:
            cols = list(r.keys())
            ph = ",".join("?" * len(cols))
            self._a.rw.execute(
                f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({ph})", [r[c] for c in cols])

    def null_column(self, table: str, set_col: str, where_col: str, ids) -> None:
        """UPDATE `table` SET `set_col` = NULL where `where_col` IN ids (detach an override). Staged."""
        self._authz(AccessMode.WRITE)
        ph = ",".join("?" * len(ids)) or "NULL"
        self._a.rw.execute(
            f"UPDATE {table} SET {set_col} = NULL WHERE {where_col} IN ({ph})", list(ids))

    def update_row(self, table: str, set_values: dict, where_values: dict) -> None:
        """UPDATE `table` SET <set_values> WHERE <where_values> (trusted columns). Staged."""
        self._authz(AccessMode.WRITE)
        set_clause = ", ".join(f"{c} = ?" for c in set_values)
        where_clause = " AND ".join(f"{c} = ?" for c in where_values)
        self._a.rw.execute(
            f"UPDATE {table} SET {set_clause} WHERE {where_clause}",
            (*set_values.values(), *where_values.values()))


class UndoLogRepo:
    """CRUD over the `undo_log` table (the reversible-op journal)."""

    def __init__(self, access):
        self._a = access

    def append(self, op: str, summary: str, payload_json: str, precheck: str) -> int:
        """Persist a snapshot as an undoable entry; returns its id (the undo token). Staged."""
        self._a.authorize(Action(_U, "append", AccessMode.WRITE))
        return self._a.rw.execute(
            "INSERT INTO undo_log (op, summary, payload, precheck) VALUES (?, ?, ?, ?)",
            (op, summary, payload_json, precheck)).lastrowid

    def get(self, token: int):
        """(op, summary, payload, precheck) for a token, or None."""
        self._a.authorize(Action(_U, "get", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT op, summary, payload, precheck FROM undo_log WHERE id = ?", (token,)).fetchone()

    def delete(self, token: int) -> None:
        """Consume (drop) an undo entry. Staged."""
        self._a.authorize(Action(_U, "delete", AccessMode.WRITE))
        self._a.rw.execute("DELETE FROM undo_log WHERE id = ?", (token,))

    def newest_for_op_edition(self, op_like: str, edition_id: int):
        """The newest undo token whose op matches `op_like` and whose payload.edition_id == the id,
        or None (the per-edition ↩ Undo affordance)."""
        self._a.authorize(Action(_U, "get", AccessMode.READ))
        r = self._a.ro.execute(
            "SELECT id FROM undo_log WHERE op LIKE ? "
            "AND json_extract(payload, '$.edition_id') = ? ORDER BY id DESC LIMIT 1",
            (op_like, int(edition_id))).fetchone()
        return r[0] if r else None


class JournalRepos:
    """Bundles `acc.journal` (row snapshot) + `acc.undo_log` (the entry table)."""

    def __init__(self, access):
        self.rows = JournalRepo(access)
        self.log = UndoLogRepo(access)
