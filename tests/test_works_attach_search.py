"""The review pane's declutter + 'Add another work' search-to-attach: the
/works/search endpoint and the /edition/<id>/works attach card."""
import pytest

from catalogue.db_store import add_alias, connect
from catalogue.webui.web import create_app


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    # Two works, both titled like "Wisdom"; one edition that contains work A.
    pa = db.execute("INSERT INTO person (primary_name) VALUES ('Nāgārjuna')").lastrowid
    wa = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", wa, "Wisdom Verses", "english")
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (wa, pa))
    wb = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", wb, "Wisdom Commentary", "english")
    eid = db.execute("INSERT INTO edition (title) VALUES ('A Book')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)",
               (eid, wa))
    db.commit()
    with app.test_client() as c:
        yield c, app, eid, wa, wb


def test_works_search_matches_and_flags_attached(env):
    c, app, eid, wa, wb = env
    r = c.get(f"/works/search?q=wisdom&exclude_edition={eid}")
    assert r.status_code == 200
    by_id = {m["work_id"]: m for m in r.get_json()["matches"]}
    assert wa in by_id and wb in by_id
    assert by_id[wa]["attached"] is True          # already on the edition
    assert by_id[wb]["attached"] is False
    assert by_id[wa]["authors"] == ["Nāgārjuna"]   # display only


def test_works_search_empty_query(env):
    c, *_ = env
    assert c.get("/works/search?q=").get_json() == {"matches": []}


def test_works_search_authority_works_listed_first(env):
    # A saved work carrying an authority id (Toh/BDRC/…) sorts ahead of works without one, and
    # surfaces canonical_system/number so the picker can append it at the end of the row.
    c, app, eid, wa, wb = env
    db = connect(app.config["DB_PATH"])
    wr = db.execute(
        "INSERT INTO work (canonical_system, canonical_number) VALUES ('toh', '3824')").lastrowid
    add_alias(db, "work", wr, "Wisdom Authority", "english")
    db.commit()
    matches = c.get(f"/works/search?q=wisdom&exclude_edition={eid}").get_json()["matches"]
    works = [m for m in matches if m.get("kind") == "work"]
    assert works[0]["work_id"] == wr                                        # authority-bearing first
    assert works[0]["canonical_system"] == "toh" and works[0]["canonical_number"] == "3824"
    assert all(not m["canonical_number"] for m in works[1:])                # the rest have none


def test_works_card_has_attach_search(env):
    c, app, eid, *_ = env
    card = c.get(f"/edition/{eid}/works").data
    assert b"Add another work" in card
    assert b"bbMountWorkSearch" in card            # inline trigger
    assert b"/edition/%d/work/add" % eid in card or b"work/add" in card


def test_attach_existing_work_then_search_marks_it(env):
    c, app, eid, wa, wb = env
    # attach work B via the same route the typeahead submits to
    c.post(f"/edition/{eid}/work/add", data={"work_id": wb})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE edition_id=?",
                      (eid,)).fetchone()[0] == 2
    # B's author edge is untouched by the attach (search shows it, none lost)
    r = c.get(f"/works/search?q=wisdom&exclude_edition={eid}")
    assert {m["work_id"]: m["attached"] for m in r.get_json()["matches"]}[wb] is True
