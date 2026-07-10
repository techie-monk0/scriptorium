"""Unit tests — the starred-editions access-API (`access_api/starred.py`).

Pins the toggle round-trip (star → list → unstar), idempotent re-star, that `list`/`count`/`is_starred`
filter to LIVE editions only (a tombstoned edition's star never resurfaces), the `NotFound` guard on a
missing edition, the FK CASCADE on a hard edition delete, and that the fingerprint moves on mutation.
Goes through the gateway (`system_access`) like the other access-API tests.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from catalogue.contracts import NotFound
from catalogue.db_store import db as dbmod
from catalogue.access_api.gateway import system_access


@pytest.fixture
def acc():
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.close()
    dbmod.init_db(fd.name).close()
    a = system_access(fd.name)
    # Two live editions to star.
    a.rw.execute("INSERT INTO edition (id, title) VALUES (1, 'One'), (2, 'Two')")
    a.commit()
    yield a
    a.close()
    Path(fd.name).unlink()


def test_star_list_unstar_roundtrip(acc):
    acc.starred.star(2); acc.commit()
    assert acc.starred.is_starred(2)
    assert acc.starred.list() == [2]
    assert acc.starred.count() == 1
    acc.starred.unstar(2); acc.commit()
    assert not acc.starred.is_starred(2)
    assert acc.starred.list() == []


def test_star_is_idempotent_and_orders_newest_first(acc):
    acc.starred.star(1); acc.commit()
    acc.starred.star(2); acc.commit()
    acc.starred.star(1); acc.commit()          # re-star — no duplicate row, bumps rev
    assert acc.starred.count() == 2
    # newest-starred first (2 starred after 1's first star); re-starring 1 does not reorder it.
    assert acc.starred.list() == [2, 1]


def test_star_missing_edition_raises_notfound(acc):
    with pytest.raises(NotFound):
        acc.starred.star(999)


def test_unstar_missing_is_noop(acc):
    acc.starred.unstar(999)          # never starred — forgiving toggle, no raise
    acc.commit()
    assert acc.starred.list() == []


def test_list_hides_tombstoned_edition(acc):
    acc.starred.star(1); acc.commit()
    acc.rw.execute("UPDATE edition SET deleted_at = datetime('now') WHERE id = 1"); acc.commit()
    # The star row still exists, but reads join to live editions only.
    assert acc.starred.list() == []
    assert acc.starred.count() == 0
    assert not acc.starred.is_starred(1)


def test_hard_delete_cascades_star(acc):
    acc.starred.star(1); acc.commit()
    acc.rw.execute("PRAGMA foreign_keys = ON")
    acc.rw.execute("DELETE FROM edition WHERE id = 1"); acc.commit()
    assert acc.rw.execute("SELECT count(*) FROM starred_edition WHERE edition_id = 1").fetchone()[0] == 0


def test_fingerprint_changes_on_mutation(acc):
    f0 = acc.starred.fingerprint()
    acc.starred.star(1); acc.commit()
    assert acc.starred.fingerprint() != f0
