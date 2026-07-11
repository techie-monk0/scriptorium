"""Where the catalogue data lives — the single source of truth for DB location.

The database is **personal data**, kept out of the public release: the live
``catalogue.db`` sits under the git-ignored-at-publish ``private/`` tree and is the
only DB file version-controlled (as a raw blob on the private ``main`` branch); its
sidecars — WAL, backups, snapshots, caches — stay local (see the top-level
`.gitignore`). Every entrypoint — the webui, the CLI, the batch pipelines —
resolves the DB path through this module so there is exactly one policy to reason
about.

Resolution order (`default_db_path()`):
  1. ``$CATALOGUE_DB``        — full path to the ``.db`` file (highest precedence)
  2. ``$CATALOGUE_DATA_DIR``  — a directory; the DB is ``<dir>/catalogue.db``
  3. ``./private/catalogue-db/catalogue.db`` — repo-relative fallback

The DB's sidecar artifacts (``.cover-cache``, ``.webdav-cache``, ``covers-pinned``)
live alongside it and are derived from ``data_dir()`` / the DB's parent directory,
so pointing ``$CATALOGUE_DB`` elsewhere moves the whole data set as a unit.
"""
from __future__ import annotations

import os

#: Env var naming the DB file directly (wins over everything).
DB_ENV = "CATALOGUE_DB"
#: Env var naming the directory that holds the DB + its sidecar caches.
DATA_DIR_ENV = "CATALOGUE_DATA_DIR"

#: Repo-relative fallback used only when neither env var is set. Lives under the
#: private/ tree (stripped from the public release); only the live catalogue.db in
#: here is tracked. Keeps the zero-config local workflow.
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
    return os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR


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
    return os.path.join(data_dir(), DB_FILENAME)


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
