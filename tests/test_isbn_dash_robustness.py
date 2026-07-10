"""ISBNs must match whether the caller supplies them with or without dashes/spaces.

Stored ISBNs are canonical (digits-only) — see the "Canonicalize ISBNs on entry"
fix. These tests pin the QUERY/LOOKUP boundaries so a dashed or spaced ISBN coming
from an external caller (a scan, a paste, an API arg) still finds the canonical row:

  - reconcile.find_candidate_editions(isbn=...)   (the exact-ISBN candidate)
  - intake_match.catalogue_verdict(db, isbn)      (Layer-1 local ISBN match across
                                                   edition / holding / edition_isbn)

normalize_isbn itself (digits-only) is also asserted directly as the contract these
boundaries rely on.
"""
from __future__ import annotations

import pytest

from catalogue.db_store import init_db
from catalogue.services import reconcile, intake_match
from catalogue.services.isbn import normalize_isbn


CANON = "9781614296362"            # stored form
DASHED = "978-1-61429-636-2"       # same ISBN, hyphenated
SPACED = "978 1 61429 636 2"       # same ISBN, spaced


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "isbn.db")
    yield conn
    conn.close()


def test_normalize_strips_dashes_and_spaces():
    assert normalize_isbn(DASHED) == CANON
    assert normalize_isbn(SPACED) == CANON
    assert normalize_isbn(CANON) == CANON
    assert normalize_isbn("") == ""
    assert normalize_isbn(None) == ""


@pytest.mark.parametrize("query_isbn", [CANON, DASHED, SPACED])
def test_find_candidate_editions_matches_regardless_of_dashes(db, query_isbn):
    eid = db.execute(
        "INSERT INTO edition (title, isbn) VALUES (?, ?)", ("Some Book", CANON)
    ).lastrowid
    hits = reconcile.find_candidate_editions(db, isbn=query_isbn)
    assert any(c["edition_id"] == eid and "isbn" in c["why"] for c in hits), \
        f"dashed/spaced query {query_isbn!r} should match canonical stored {CANON!r}"


@pytest.mark.parametrize("query_isbn", [CANON, DASHED, SPACED])
def test_catalogue_verdict_layer1_matches_regardless_of_dashes(db, query_isbn):
    # ISBN on the EDITION (one of the three Layer-1 homes).
    eid = db.execute(
        "INSERT INTO edition (title, isbn) VALUES (?, ?)", ("Edition ISBN Book", CANON)
    ).lastrowid
    v = intake_match.catalogue_verdict(db, query_isbn)
    assert v["in_catalogue"] is True and v["matched_by"] == "isbn"
    assert any(e["id"] == eid for e in v["editions"])


@pytest.mark.parametrize("query_isbn", [DASHED, SPACED])
def test_catalogue_verdict_matches_holding_isbn_with_dashes(db, query_isbn):
    # ISBN on a HOLDING (print/epub/pdf carry different ISBNs).
    eid = db.execute(
        "INSERT INTO edition (title, isbn) VALUES (?, ?)", ("Holding ISBN Book", "")
    ).lastrowid
    db.execute(
        "INSERT INTO holding (edition_id, form, isbn) VALUES (?, 'electronic', ?)",
        (eid, CANON))
    v = intake_match.catalogue_verdict(db, query_isbn)
    assert v["in_catalogue"] is True
    assert any(e["id"] == eid for e in v["editions"])
