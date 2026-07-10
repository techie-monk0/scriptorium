"""Unit tests — the wishlist access-API (`access_api/wishlist.py`).

Pins the typed repo round-trip (add → list → get → resolve → soft-delete), optimistic concurrency
(StaleWrite on a wrong rev), the acquisition `match` lookup, and that soft-deleted items leave the
live view. Goes through the gateway (`system_access`) like the other access-API tests.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from catalogue.contracts import NotFound, StaleWrite
from catalogue.db_store import db as dbmod
from catalogue.access_api.gateway import system_access


@pytest.fixture
def acc():
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.close()
    dbmod.init_db(fd.name).close()
    a = system_access(fd.name)
    yield a
    a.close()
    Path(fd.name).unlink()


def test_add_list_get_roundtrip(acc):
    i = acc.wishlist.add(source="isbn", raw_isbn="9780061575594", status="resolved",
                         snapshot={"title": "Being and Time", "authors": ["Heidegger"],
                                   "isbn": "9780061575594", "ol_work_key": "/works/OL1W"})
    acc.commit()
    item = acc.wishlist.get(i)
    assert item.title == "Being and Time"
    assert item.authors == ("Heidegger",)
    assert item.isbn == "9780061575594"
    assert acc.wishlist.count() == 1
    assert [w.id for w in acc.wishlist.list()] == [i]


def test_resolve_writes_snapshot_and_bumps_rev(acc):
    i = acc.wishlist.add(source="manual", raw_title="x", status="unresolved")
    acc.commit()
    assert acc.wishlist.get(i).rev == 0
    acc.wishlist.resolve(i, {"title": "Resolved", "isbn": "9780000000019"}, "resolved")
    acc.commit()
    w = acc.wishlist.get(i)
    assert w.status == "resolved" and w.title == "Resolved" and w.rev == 1


def test_optimistic_concurrency(acc):
    i = acc.wishlist.add(source="manual", raw_title="x")
    acc.commit()
    with pytest.raises(StaleWrite):
        acc.wishlist.update(i, notes="n", expected_rev=99)
    # Correct rev succeeds.
    acc.wishlist.update(i, notes="ok", expected_rev=0)
    acc.commit()
    assert acc.wishlist.get(i).notes == "ok"


def test_soft_delete_leaves_live_view(acc):
    i = acc.wishlist.add(source="manual", raw_title="x")
    acc.commit()
    acc.wishlist.remove(i)
    acc.commit()
    assert acc.wishlist.get(i) is None
    assert acc.wishlist.count() == 0
    # The row still exists (tombstoned) — id frozen, never reused.
    assert acc.rw.execute("SELECT deleted_at FROM wishlist_item WHERE id=?", (i,)).fetchone()[0]


def test_update_missing_raises_notfound(acc):
    with pytest.raises(NotFound):
        acc.wishlist.update(999, notes="x")


def test_match_by_isbn_workkey_title_and_skips_acquired(acc):
    i = acc.wishlist.add(source="isbn", raw_isbn="9780061575594", status="resolved",
                         snapshot={"title": "Being and Time", "isbn": "9780061575594",
                                   "ol_work_key": "/works/OL1W"})
    acc.commit()
    assert acc.wishlist.match(isbn="9780061575594").id == i
    assert acc.wishlist.match(ol_work_key="/works/OL1W").id == i
    assert acc.wishlist.match(title="  being   and TIME ").id == i      # folded
    assert acc.wishlist.match(isbn="9999999999999") is None
    # Once acquired, match no longer returns it (the acquisition loop won't re-fire).
    acc.rw.execute("INSERT INTO edition (id, title) VALUES (7, 'Being and Time')"); acc.commit()
    acc.wishlist.mark_acquired(i, 7)
    acc.commit()
    assert acc.wishlist.match(isbn="9780061575594") is None
    w = acc.wishlist.get(i)
    assert w.status == "acquired" and w.matched_edition_id == 7 and w.acquired_at


def test_fingerprint_changes_on_mutation(acc):
    f0 = acc.wishlist.fingerprint()
    acc.wishlist.add(source="manual", raw_title="x"); acc.commit()
    assert acc.wishlist.fingerprint() != f0
