"""Where the catalogue data lives — the single source of truth for DB location.

The database is **personal data**, kept out of the public release. It is **not
tracked in this repo**: the live ``catalogue.db`` lives in its own repo/dir (e.g.
``~/Dev/catalogue-db``), pointed at via ``$CATALOGUE_DB`` — set in
``private/serve.env`` (git-ignored) and sourced by ``scripts/library-serve.sh``. Its
sidecars — WAL, backups, snapshots, caches — stay local alongside it. Every
entrypoint — the webui, the CLI, the batch pipelines — resolves the DB path through
this module so there is exactly one policy to reason about.

Resolution order (`default_db_path()`):
  1. ``$CATALOGUE_DB``        — full path to the ``.db`` file (highest precedence)
  2. ``$CATALOGUE_DATA_DIR``  — a directory; the DB is ``<dir>/catalogue.db``
  3. ``catalogue_db`` in ``private/local_defaults.json`` — the machine-local config, the
     single source of truth read by *every* entrypoint (webui, CLI, pipelines, tests,
     analysis scripts); so the DB location is never hardcoded anywhere
  4. ``./private/catalogue-db/catalogue.db`` — repo-relative fallback

The DB's sidecar artifacts (``.cover-cache``, ``.webdav-cache``, ``covers-pinned``)
live alongside it and are derived from ``data_dir()`` / the DB's parent directory,
so pointing ``$CATALOGUE_DB`` elsewhere moves the whole data set as a unit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

#: Env var naming the DB file directly (wins over everything).
DB_ENV = "CATALOGUE_DB"
#: Env var naming the directory that holds the DB + its sidecar caches.
DATA_DIR_ENV = "CATALOGUE_DATA_DIR"

#: Machine-local private config (tracked under private/, stripped from the public
#: release). Holds the real DB path (and other machine paths); read directly here so a
#: lower layer doesn't import the services layer. Location overridable for tests.
_LOCAL_DEFAULTS_ENV = "CATALOGUE_LOCAL_DEFAULTS"
_CONFIG_DB_KEY = "catalogue_db"
#: Repo root, from this file's location (editable install / repo checkout).
_REPO_ROOT = Path(__file__).resolve().parents[5]


def _private_config() -> dict:
    """Parsed ``private/local_defaults.json`` (or ``$CATALOGUE_LOCAL_DEFAULTS``); ``{}``
    if absent/unreadable. Read as plain JSON — no dependency on the services layer."""
    override = os.environ.get(_LOCAL_DEFAULTS_ENV)
    path = Path(override) if override else _REPO_ROOT / "private" / "local_defaults.json"
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def config_db_path() -> "str | None":
    """The DB path from the private config (``catalogue_db`` in local_defaults.json),
    with env overrides IGNORED and ``~`` / ``$VARS`` expanded; ``None`` when unset.

    This is the machine-local source of truth. It exists separately from
    ``default_db_path()`` because the test suite deliberately overrides ``$CATALOGUE_DB``
    to a tmp DB for hermeticity — the live-DB guard/integrity tests use *this* to find
    the REAL live DB regardless of that override."""
    v = _private_config().get(_CONFIG_DB_KEY)
    if not v:
        return None
    return os.path.expanduser(os.path.expandvars(v))

#: Repo-relative fallback used only when neither env var is set (e.g. a fresh clone
#: with no serve.env). Kept for a zero-config default; the maintainer's real DB is
#: pointed at via $CATALOGUE_DB (private/serve.env). Nothing here is tracked anymore.
DEFAULT_DATA_DIR = "private/catalogue-db"
DB_FILENAME = "catalogue.db"


def data_dir() -> str:
    """Directory that holds the catalogue DB and its sidecar caches.

    Honors ``$CATALOGUE_DB`` (its parent dir), then ``$CATALOGUE_DATA_DIR``, then
    the repo-relative ``catalogue-db/`` fallback. Read at call time, never cached,
    so tests and deployments can set the env after import.
    """
    db = os.environ.get(DB_ENV)
    if db:
        return os.path.dirname(os.path.abspath(db))
    dd = os.environ.get(DATA_DIR_ENV)
    if dd:
        return dd
    cfg = config_db_path()
    if cfg:
        return os.path.dirname(os.path.abspath(cfg))
    return DEFAULT_DATA_DIR


def default_db_path() -> str:
    """Resolved path to the catalogue DB (see module docstring for the order).

    Use this everywhere a DB path default is needed — e.g.
    ``ap.add_argument("--db", default=default_db_path())`` — instead of hardcoding
    ``"catalogue-db/catalogue.db"``. Returns a path string; the file may not exist
    yet (callers that require it get a clear error from ``connect``).
    """
    db = os.environ.get(DB_ENV)
    if db:
        return db
    dd = os.environ.get(DATA_DIR_ENV)
    if dd:
        return os.path.join(dd, DB_FILENAME)
    cfg = config_db_path()
    if cfg:
        return cfg
    return os.path.join(DEFAULT_DATA_DIR, DB_FILENAME)


def require_db(db_path: str | os.PathLike) -> str:
    """Return ``db_path`` if the file exists, else raise a FileNotFoundError that
    tells the user how to supply one.

    The catalogue DB is user-supplied and not shipped in the repo, so a missing
    file is the expected first-run state — this turns SQLite's opaque "unable to
    open database file" into an actionable message. Use at entrypoints that need
    an *existing, populated* DB (read paths, the webui); do not use where a fresh
    DB may legitimately be created (``init_db``).
    """
    p = os.fspath(db_path)
    if not os.path.isfile(p):
        raise FileNotFoundError(
            f"Catalogue database not found at {p!r}.\n"
            f"The catalogue.db is user data and is not included in the repository. "
            f"Provide one by setting ${DB_ENV} to its path, ${DATA_DIR_ENV} to its "
            f"directory, or placing it at {os.path.join(DEFAULT_DATA_DIR, DB_FILENAME)!r}."
        )
    return p
