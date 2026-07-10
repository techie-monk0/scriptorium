"""Edition review verdict read/write (catalogue_review.set_review/get_review).

The edition_verify diff-categorizer + triage actions (and the /catalogue/verify,
/catalogue/review surface) were removed; see tests/system/DELETIONS.md.
"""
import pytest

from catalogue.services import catalogue_review as cr
from catalogue.db_store import connect, init_db


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "c.db")
    init_db(p).close()
    conn = connect(p)
    yield conn
    conn.close()


def _edition(db, title="A Book", publisher=None, year=None):
    return db.execute("INSERT INTO edition (title, publisher, year) VALUES (?, ?, ?)",
                      (title, publisher, year)).lastrowid


def test_set_and_get_review_merges_flags(db):
    eid = _edition(db)
    cr.set_review(db, eid, status="needs_fix", flags={"title": True}, note="check author")
    cr.set_review(db, eid, flags={"contributors": True})   # merge, don't clobber
    rv = cr.get_review(db, eid)
    assert rv["status"] == "needs_fix"
    assert rv["flags"] == {"title": True, "contributors": True}
    assert rv["note"] == "check author"
    assert rv["reviewed_at"] is not None
