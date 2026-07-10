"""Atomic, WAL-safe snapshot of the catalogue DB.

Under WAL (the catalogue's journal mode) recent commits live in the `-wal` file
until checkpointed, so a plain `cp catalogue.db` can capture a copy that is
MISSING the latest writes — it looks like "it didn't save". `VACUUM INTO` instead
reads a consistent snapshot (even while other connections are writing), folds the
WAL into the snapshot, and writes ONE defragmented file with no `-wal`/`-shm`
baggage. Use this for every backup.

Usage:
    python -m catalogue.cli.backup                       # → catalogue-db/catalogue-backup-<ts>.db
    python -m catalogue.cli.backup path/to.db -o out.db
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime

from catalogue.db_store import connect
from catalogue.db_store import default_db_path

DEFAULT_DB = default_db_path()


def default_dest(db_path: str) -> str:
    """A timestamped sibling of the source DB: `<name>-backup-YYYYmmdd-HHMMSS.db`."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.splitext(os.path.basename(db_path))[0]
    return os.path.join(os.path.dirname(db_path) or ".", f"{base}-backup-{ts}.db")


def backup(db_path: str, dest: str | None = None) -> str:
    """Snapshot `db_path` to `dest` (default: a timestamped sibling) via VACUUM INTO.
    Returns the destination path. Refuses to overwrite an existing file (VACUUM INTO
    fails on a pre-existing target anyway — we surface a clear error first)."""
    if not os.path.exists(db_path):
        raise SystemExit(f"source DB not found: {db_path}")
    dest = dest or default_dest(db_path)
    if os.path.exists(dest):
        raise SystemExit(f"refusing to overwrite existing file: {dest}")
    conn = connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (dest,))      # consistent even under concurrent writers
    finally:
        conn.close()
    return dest


def _edition_count(path: str) -> int:
    # Count via the sanctioned connect() (FK chokepoint). The snapshot is an exact
    # VACUUM INTO copy of the source, so we read the SOURCE — that avoids opening the
    # snapshot in WAL mode and leaving -wal/-shm sidecars beside the clean backup.
    conn = connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM edition").fetchone()[0]
    finally:
        conn.close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="WAL-safe snapshot of the catalogue DB (VACUUM INTO).")
    ap.add_argument("db", nargs="?", default=DEFAULT_DB, help=f"source DB (default: {DEFAULT_DB})")
    ap.add_argument("-o", "--out", help="destination file (default: <db>-backup-<timestamp>.db)")
    args = ap.parse_args(argv)
    dest = backup(args.db, args.out)
    size_mb = os.path.getsize(dest) / 1e6
    print(f"snapshot → {dest}  ({_edition_count(args.db)} editions, {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
