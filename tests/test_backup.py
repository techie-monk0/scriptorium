"""WAL-safe DB snapshot via catalogue/cli/backup.py (VACUUM INTO)."""
from __future__ import annotations

import os

import pytest

from catalogue.cli.backup import backup, default_dest
from catalogue.db_store import connect, init_db


def _seed(path):
    db = init_db(path)
    db.execute("INSERT INTO edition (title) VALUES ('Snapshot Me')")
    db.commit(); db.close()


def test_backup_captures_committed_rows(tmp_path):
    src = tmp_path / "live.db"
    _seed(src)
    dest = backup(str(src))
    assert os.path.exists(dest)
    n = connect(dest).execute("SELECT COUNT(*) FROM edition").fetchone()[0]
    assert n == 1                                  # the committed row is in the snapshot


def test_backup_includes_uncheckpointed_wal_writes(tmp_path):
    """The whole point: a commit still sitting in the -wal file (not yet checkpointed
    into the main .db) MUST appear in the snapshot — a plain file copy could miss it."""
    src = tmp_path / "live.db"
    _seed(src)
    live = connect(src)                            # WAL mode; keep it open so no auto-checkpoint
    live.execute("INSERT INTO edition (title) VALUES ('In The WAL')")
    live.commit()
    assert os.path.exists(str(src) + "-wal")       # the write is in the WAL, not yet folded in
    dest = backup(str(src))
    live.close()
    titles = {r[0] for r in connect(dest).execute("SELECT title FROM edition")}
    assert titles == {"Snapshot Me", "In The WAL"}


def test_backup_refuses_to_overwrite(tmp_path):
    src = tmp_path / "live.db"; _seed(src)
    dest = tmp_path / "out.db"; dest.write_text("x")
    with pytest.raises(SystemExit):
        backup(str(src), str(dest))


def test_default_dest_is_timestamped_sibling(tmp_path):
    d = default_dest(str(tmp_path / "catalogue.db"))
    assert d.startswith(str(tmp_path)) and "catalogue-backup-" in d and d.endswith(".db")
