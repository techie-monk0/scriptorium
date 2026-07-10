"""Unit tests — the wishlist resolver (`services/wishlist_resolve.py`).

Pure orchestration over isbn/cip/intake_match, exercised offline with injected fetchers. Pins the
three input forms (ISBN / CIP / title) and the statuses they produce — including the contract the
frontends + scanner depend on: a book the resolver CAN'T identify still resolves to a kept item
(`unresolved`/`ambiguous`), never an exception.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from catalogue.db_store import db as dbmod
from catalogue.services import wishlist_resolve as WR

_ISBN = "9780061575594"
_META = {"title": "Being and Time", "authors": ["Martin Heidegger"],
         "publishers": ["Harper"], "publish_date": "1962"}


@pytest.fixture
def conn():
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.close()
    c = dbmod.init_db(fd.name)
    yield c
    c.close()
    Path(fd.name).unlink()


def _lookup(i):
    return _META if i == _ISBN else None


def _wk(i):
    return "/works/OL12345W" if i == _ISBN else None


def test_isbn_resolves_to_snapshot(conn):
    r = WR.resolve_isbn(conn, "978-0-06-157559-4", isbn_lookup=_lookup, work_key_fetch=_wk)
    assert r.status == "resolved"
    assert r.snapshot["title"] == "Being and Time"
    assert r.snapshot["isbn"] == _ISBN
    assert r.snapshot["ol_work_key"] == "/works/OL12345W"
    assert r.snapshot["year"] == 1962
    assert r.snapshot["cover_url"].endswith(f"{_ISBN}-L.jpg")


def test_invalid_isbn_is_unresolved_not_error(conn):
    r = WR.resolve_isbn(conn, "12345", isbn_lookup=_lookup, work_key_fetch=_wk)
    assert r.status == "unresolved"
    assert r.verdict["in_catalogue"] is False


def test_isbn_valid_but_no_record_stays_unresolved(conn):
    # Checksum-valid ISBN OpenLibrary doesn't know → kept with the ISBN, status unresolved.
    r = WR.resolve_isbn(conn, "9783161484100", isbn_lookup=lambda i: None, work_key_fetch=lambda i: None)
    assert r.status == "unresolved"
    assert r.snapshot["isbn"] == "9783161484100"


def test_isbn_already_owned_flags_owned(conn):
    conn.execute("INSERT INTO edition (title, isbn) VALUES ('Being and Time', ?)", (_ISBN,))
    conn.commit()
    r = WR.resolve_isbn(conn, _ISBN, isbn_lookup=_lookup, work_key_fetch=_wk)
    assert r.status == "owned"
    assert r.snapshot["matched_edition_id"] is not None


def test_cip_resolves_title_and_lccn(conn):
    cip = ("## copyright page\nLibrary of Congress Cataloging-in-Publication Data\n"
           "Title: Being and time / Martin Heidegger.\nISBN 9780061575594\nLCCN 2008012345")
    r = WR.resolve_cip(conn, cip, isbn_lookup=_lookup, work_key_fetch=_wk)
    assert r.status in ("resolved", "owned")
    assert r.snapshot["title"]
    assert r.snapshot["lccn"] == "2008012345"
    assert r.snapshot["isbn"] == _ISBN


def test_cip_unparseable_is_unresolved(conn):
    r = WR.resolve_cip(conn, "just some random text with no CIP block",
                       isbn_lookup=_lookup, work_key_fetch=_wk)
    assert r.status == "unresolved"


def test_title_single_candidate_resolves(conn):
    search = lambda t, a=None: [{"title": "Zen Mind", "authors": ["Suzuki"], "publisher": "W",
                                 "year": 1970, "isbn_13": _ISBN, "ol_work_key": "/works/OLz"}]
    r = WR.resolve_title(conn, "Zen Mind", "Suzuki", title_search=search)
    assert r.status == "resolved"
    assert r.snapshot["title"] == "Zen Mind"
    assert r.snapshot["isbn"] == _ISBN


def test_title_many_candidates_is_ambiguous(conn):
    cands = [{"title": "A1", "isbn_13": None, "ol_work_key": "/works/a"},
             {"title": "A2", "isbn_13": None, "ol_work_key": "/works/b"}]
    r = WR.resolve_title(conn, "Ambiguous", title_search=lambda t, a=None: cands)
    assert r.status == "ambiguous"
    assert len(r.snapshot["candidates"]) == 2


def test_title_no_candidates_keeps_typed_title(conn):
    r = WR.resolve_title(conn, "Nonexistent Xyz", title_search=lambda t, a=None: [])
    assert r.status == "unresolved"
    assert r.snapshot["title"] == "Nonexistent Xyz"


def test_snapshot_from_candidate_helper():
    snap = WR.snapshot_from_candidate(
        {"title": "T", "authors": ["A"], "publisher": "P", "year": 2001,
         "isbn_13": _ISBN, "ol_work_key": "/works/OLx"})
    assert snap["title"] == "T" and snap["isbn"] == _ISBN
    assert snap["cover_url"].endswith(f"{_ISBN}-L.jpg")
