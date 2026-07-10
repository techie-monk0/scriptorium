"""DB-location abstraction (catalogue.db_store.paths): the database is user-supplied
and NOT in the repo, so every entrypoint resolves its path through one function.

Contract under test:
  default_db_path()  ->  $CATALOGUE_DB  >  $CATALOGUE_DATA_DIR/catalogue.db  >  ./catalogue-db/catalogue.db
  data_dir()         ->  the directory that holds the DB + its sidecar caches
  require_db()       ->  actionable FileNotFoundError when the file is absent
  connect_ro()       ->  same friendly error (read-only cannot create a DB)

Includes an end-to-end check that a fresh process honours a relocated $CATALOGUE_DB.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from catalogue.db_store import paths, connect_ro, default_db_path, data_dir, DB_ENV, DATA_DIR_ENV


@pytest.fixture
def clean_env(monkeypatch):
    """Neither location env set. conftest FORCES CATALOGUE_DB for hermeticity, so
    delete both here and let each test opt back in."""
    monkeypatch.delenv(DB_ENV, raising=False)
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    return monkeypatch


# ── resolution precedence ───────────────────────────────────────────────────
def test_fallback_is_repo_relative(clean_env):
    """No env → the git-ignored repo-relative default."""
    assert default_db_path() == os.path.join("catalogue-db", "catalogue.db")
    assert data_dir() == "catalogue-db"


def test_data_dir_env_places_db_inside_it(clean_env):
    clean_env.setenv(DATA_DIR_ENV, "/srv/library")
    assert data_dir() == "/srv/library"
    assert default_db_path() == os.path.join("/srv/library", "catalogue.db")


def test_catalogue_db_wins_over_data_dir(clean_env):
    """$CATALOGUE_DB is the full path and beats $CATALOGUE_DATA_DIR."""
    clean_env.setenv(DATA_DIR_ENV, "/srv/library")
    clean_env.setenv(DB_ENV, "/elsewhere/my.db")
    assert default_db_path() == "/elsewhere/my.db"
    # Sidecars (.cover-cache, covers-pinned, …) live beside the DB, so data_dir
    # tracks the DB's parent, not $CATALOGUE_DATA_DIR.
    assert data_dir() == "/elsewhere"


def test_read_at_call_time_not_cached(clean_env):
    """Resolution must re-read env each call (tests/deployments set it late)."""
    clean_env.setenv(DB_ENV, "/first/a.db")
    assert default_db_path() == "/first/a.db"
    clean_env.setenv(DB_ENV, "/second/b.db")
    assert default_db_path() == "/second/b.db"


# ── missing-DB guard ────────────────────────────────────────────────────────
def test_require_db_returns_existing_path(tmp_path):
    db = tmp_path / "catalogue.db"
    db.write_bytes(b"")
    assert paths.require_db(db) == os.fspath(db)


def test_require_db_actionable_error(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError) as ei:
        paths.require_db(missing)
    msg = str(ei.value)
    assert "not included in the repository" in msg
    assert DB_ENV in msg and DATA_DIR_ENV in msg          # tells the user the knobs


def test_connect_ro_missing_db_is_friendly(tmp_path):
    """Read-only cannot create a DB; a missing file must raise our guidance,
    not SQLite's opaque 'unable to open database file'."""
    with pytest.raises(FileNotFoundError) as ei:
        connect_ro(tmp_path / "absent.db")
    assert "Catalogue database not found" in str(ei.value)


# ── end-to-end: a relocated DB is created and read where env points ──────────
def test_resolve_create_read_cycle(clean_env, tmp_path):
    """In-process: point $CATALOGUE_DB elsewhere, init a fresh DB at the resolved
    path, then read it back read-only — proving the whole cycle honours relocation."""
    from catalogue.db_store import init_db

    target = tmp_path / "relocated" / "catalogue.db"
    target.parent.mkdir()
    clean_env.setenv(DB_ENV, str(target))

    init_db(default_db_path()).close()
    assert target.is_file()

    ro = connect_ro(default_db_path())
    try:
        # schema_meta is seeded by init_db; its presence proves we opened OUR db.
        (n,) = ro.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()
        assert n > 0
    finally:
        ro.close()


def test_fresh_process_honours_catalogue_db_env(tmp_path):
    """True end-to-end: a brand-new interpreter running the db_store create
    entrypoint writes the DB exactly where $CATALOGUE_DB points (no import-time
    caching, no CWD fallback)."""
    target = tmp_path / "e2e" / "catalogue.db"
    target.parent.mkdir()
    env = {**os.environ, DB_ENV: str(target)}
    env.pop(DATA_DIR_ENV, None)

    proc = subprocess.run(
        [sys.executable, "-m", "catalogue.db_store.db"],
        cwd=tmp_path, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert target.is_file(), f"DB not created at $CATALOGUE_DB; stdout={proc.stdout!r}"
    assert "init OK" in proc.stdout
    # It must NOT have fallen back to ./catalogue-db/ under the temp CWD.
    assert not (tmp_path / "catalogue-db").exists()
