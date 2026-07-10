"""Pytest plugin — shared fixtures, auto-loaded via the `pytest11` entry point.

Registered in test-kit's pyproject so `import`-ing nothing is required: any test in any package gets
`cat_db` / `cat_conn` / `cat_acc`. They build a throwaway initialized catalogue DB (never the live
one — see the suite's `_guard_live_db` tripwire) so a test seeds through a connection and reads
through a bound `Access` against the same file.
"""
from __future__ import annotations

import pytest

from catalogue.access_api import system_access
from catalogue.db_store import connect, init_db


@pytest.fixture
def cat_db(tmp_path):
    """Path to a freshly initialized (schema-current) throwaway catalogue DB."""
    p = tmp_path / "catalogue.db"
    init_db(p).close()
    return p


@pytest.fixture
def cat_conn(cat_db):
    """An open read-write connection to `cat_db` — for seeding (caller commits)."""
    conn = connect(cat_db)
    yield conn
    conn.close()


@pytest.fixture
def cat_acc(cat_db):
    """A SYSTEM-bound `Access` over `cat_db` (full access; closed on teardown). Read through this
    AFTER committing any seed written via `cat_conn`."""
    with system_access(cat_db) as acc:
        yield acc
