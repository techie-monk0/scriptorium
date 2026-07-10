"""Part A: edition_author (book-level authorship) + work.review_status, and the
contributor_store helpers / GC guard that go with them."""
import pytest

from catalogue.db_store import init_db
from catalogue.db_store import contributor_store as cs


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "c.db")
    yield conn
    conn.close()


def _person(db, name):
    return db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid


def _edition(db, title="Book"):
    return db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


def test_schema_objects_exist(db):
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='edition_author'").fetchone()
    cols = {r[1] for r in db.execute("PRAGMA table_info(work)")}
    assert {"review_status", "review_note", "reviewed_at"} <= cols


def test_add_and_read_edition_authors(db):
    e = _edition(db)
    p1, p2 = _person(db, "Jampa Tegchok"), _person(db, "Thubten Chodron")
    cs.add_edition_author(db, e, p1)
    cs.add_edition_author(db, e, p2, role="editor")
    cs.add_edition_author(db, e, p1)               # idempotent (same role)
    assert cs.edition_author_ids(db, e) == [p1, p2]
    assert db.execute("SELECT COUNT(*) FROM edition_author WHERE edition_id=?", (e,)).fetchone()[0] == 2


def test_set_edition_authors_reconciles(db):
    e = _edition(db)
    a, b, c2 = _person(db, "A"), _person(db, "B"), _person(db, "C")
    cs.set_edition_authors(db, e, [a, b])
    removed = cs.set_edition_authors(db, e, [b, c2])   # drop a, add c
    assert removed == {a}
    assert cs.edition_author_ids(db, e) == [b, c2]


def test_person_referenced_includes_edition_author(db):
    e = _edition(db)
    p = _person(db, "Solo Author")
    assert not cs.person_referenced(db, p)
    cs.add_edition_author(db, e, p)
    assert cs.person_referenced(db, p)              # now reachable via the edition
    assert cs.person_edition_ids_as_author(db, p) == [e]


def test_edition_author_cascades_on_edition_delete(db):
    e = _edition(db)
    cs.add_edition_author(db, e, _person(db, "X"))
    db.execute("DELETE FROM edition WHERE id=?", (e,))
    assert db.execute("SELECT COUNT(*) FROM edition_author").fetchone()[0] == 0


def test_work_review_status_roundtrip(db):
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    assert db.execute("SELECT review_status FROM work WHERE id=?", (wid,)).fetchone()[0] is None
    db.execute("UPDATE work SET review_status='ok', review_note='checked' WHERE id=?", (wid,))
    row = db.execute("SELECT review_status, review_note FROM work WHERE id=?", (wid,)).fetchone()
    assert row == ("ok", "checked")
