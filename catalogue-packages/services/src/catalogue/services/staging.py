"""File staging for parallel resolution (Step 4).

Resolving one book takes minutes (LLM ladder + BDRC/84000 HTTP). SQLite
allows only one writer, so a resolver that wrote straight to the DB would
hold the single write lock for that whole time — blocking manual entry and
forbidding a second resolver process.

The fix: resolvers don't touch the DB. Each book's writes are *captured*
and dumped to one JSON file per holding; a separate `load` pass replays
them into the DB in short transactions. Cache lookups still read the live
DB (reads never contend under WAL, and cross-run cache hits are preserved).

A staged artifact is just an ordered journal of the `INSERT` statements the
book produced — `process_holding` writes only `INSERT OR REPLACE` (caches)
and `INSERT` (review_queue), all with primitive params (see catalogue/
process.py, classify.py, work_canonical_resolver.py, toc.py). So "load" = replay the
journal; no schema duplication, and new writes are captured automatically.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ARTIFACT_SCHEMA = 1


class _NoopCursor:
    """Returned for captured (non-SELECT) statements. The slow-path callers
    never inspect an INSERT's cursor, but make it safe anyway."""

    rowcount = -1
    lastrowid = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


class StagingConn:
    """A connection shim wrapping a real (read) connection.

    `SELECT` (and `PRAGMA`) pass through to the wrapped connection — cache
    lookups hit the live DB. Anything else (the `INSERT`s) is captured into
    `self.writes` instead of executing, and `commit()` is a no-op. Drop this
    in wherever `process_holding` expects a `conn` and the book resolves
    without ever taking the DB's write lock.
    """

    def __init__(self, read_conn) -> None:
        self._read = read_conn
        self.writes: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple = ()):  # noqa: D401
        head = sql.lstrip()[:6].upper()
        if head.startswith("SELECT") or head.startswith("PRAGMA"):
            return self._read.execute(sql, params)
        self.writes.append({"sql": sql, "params": _jsonable(params)})
        return _NoopCursor()

    def cursor(self):
        # Not used by the slow path, but keep the contract intact.
        return self

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        # Discard anything captured since the last flush point.
        self.writes.clear()

    def close(self) -> None:
        pass


def _jsonable(params: tuple) -> list:
    """Captured params must round-trip through JSON. The slow path only ever
    binds str / int / float / None — reject anything else loudly rather than
    silently corrupt the journal."""
    out = []
    for p in params:
        if p is None or isinstance(p, (str, int, float)):
            out.append(p)
        elif isinstance(p, (bytes, bytearray)):
            raise TypeError(
                "staging cannot journal a BLOB param; the Step-4 writes are "
                "all text/number — a BLOB means the write path changed."
            )
        else:
            raise TypeError(f"non-JSON-serializable staged param: {type(p)!r}")
    return out


def artifact_path(staging_dir: str | os.PathLike, holding_id: int) -> Path:
    return Path(staging_dir) / f"holding_{holding_id}.json"


def write_artifact(staging_dir: str | os.PathLike, holding_id: int,
                   writes: list[dict], report: dict | None = None) -> Path:
    """Atomically write one book's journal. Temp-file + rename so a reader
    (or a crash) never sees a half-written artifact."""
    d = Path(staging_dir)
    d.mkdir(parents=True, exist_ok=True)
    final = artifact_path(d, holding_id)
    tmp = final.with_suffix(".json.tmp")
    payload = {
        "schema": ARTIFACT_SCHEMA,
        "holding_id": holding_id,
        "writes": writes,
        "report": report or {},
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def load_artifacts(conn, staging_dir: str | os.PathLike,
                   loaded_dir: str | os.PathLike | None = None) -> dict:
    """Replay every `holding_*.json` in `staging_dir` into `conn`.

    Each file is one short transaction (grab the write lock, replay its
    INSERTs, commit, release) so the load coexists with manual web entry via
    `busy_timeout`. A successfully loaded file is moved to `loaded_dir`
    (default `<staging_dir>/loaded`) so it is never replayed twice — the
    review_queue INSERTs are append-only, so a double-load would duplicate.
    A file that errors is left in place and reported.
    """
    d = Path(staging_dir)
    done_dir = Path(loaded_dir) if loaded_dir else d / "loaded"
    files = sorted(p for p in d.glob("holding_*.json") if p.is_file())

    loaded = errors = total_writes = 0
    failures: list[tuple[str, str]] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("schema") != ARTIFACT_SCHEMA:
                raise ValueError(f"unknown artifact schema {data.get('schema')!r}")
            writes = data["writes"]
            conn.execute("BEGIN")
            for w in writes:
                conn.execute(w["sql"], tuple(w["params"]))
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — surface, don't abort the batch
            conn.rollback()
            errors += 1
            failures.append((f.name, f"{exc.__class__.__name__}: {exc}"))
            continue
        done_dir.mkdir(parents=True, exist_ok=True)
        os.replace(f, done_dir / f.name)
        loaded += 1
        total_writes += len(writes)

    return {
        "loaded": loaded,
        "errors": errors,
        "writes": total_writes,
        "failures": failures,
    }
