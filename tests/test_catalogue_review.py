"""Works-curation model: adapters + apply_draft round-trips."""
import pytest

from catalogue.services import catalogue_review as cr
from catalogue.db_store import add_alias, connect, init_db


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "c.db")
    init_db(p).close()
    conn = connect(p)
    yield conn
    conn.close()


def _edition(db, title="A Book"):
    return db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


def _work(db, eid, title, seq, *, authors=(), translators=()):
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", wid, title, "english")
    for a in authors:
        pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (a,)).lastrowid
        add_alias(db, "person", pid, a, "english")
        db.execute("INSERT INTO work_author (work_id, person_id, role) "
                   "VALUES (?, ?, 'author')", (wid, pid))
    for t in translators:
        pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (t,)).lastrowid
        add_alias(db, "person", pid, t, "english")
        db.execute("INSERT OR IGNORE INTO edition_translator (edition_id, person_id, seq) "
                   "VALUES (?, ?, 1)", (eid, pid))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) "
               "VALUES (?, ?, ?)", (eid, wid, seq))
    return wid


def _names(draft):
    return [(w["title"], sorted((c["name"], c["role"]) for c in w["contributors"]))
            for w in draft["works"]]


def test_draft_from_edition_shape(db):
    eid = _edition(db)
    _work(db, eid, "Root Text", 1, authors=["Nagarjuna"], translators=["Jay Garfield"])
    draft = cr.draft_from_edition(db, eid)
    assert draft["structure"] == "single_work"
    assert len(draft["works"]) == 1
    w = draft["works"][0]
    assert w["title"] == "Root Text"
    roles = {(c["name"], c["role"]) for c in w["contributors"]}
    assert ("Nagarjuna", "author") in roles
    assert ("Jay Garfield", "translator") in roles


def test_round_trip_identity(db):
    eid = _edition(db)
    _work(db, eid, "W1", 1, authors=["Atisha"])
    _work(db, eid, "W2", 2, authors=["Tsongkhapa"], translators=["Thurman"])
    before = cr.draft_from_edition(db, eid)
    cr.apply_draft(db, eid, before)
    after = cr.draft_from_edition(db, eid)
    assert _names(before) == _names(after)
    assert after["structure"] == "multi_work"


def test_exclude_collapses_to_single_work(db):
    eid = _edition(db)
    _work(db, eid, "Real Work", 1, authors=["Shantideva"])
    spurious = _work(db, eid, "Chapter 2 spurious", 2)
    draft = cr.draft_from_edition(db, eid)
    assert draft["structure"] == "multi_work"
    for w in draft["works"]:
        if w["work_id"] == spurious:
            w["included"] = False
    res = cr.apply_draft(db, eid, draft)
    assert res["structure"] == "single_work"
    after = cr.draft_from_edition(db, eid)
    assert [w["title"] for w in after["works"]] == ["Real Work"]
    # The excluded work was orphan-GC'd.
    assert db.execute("SELECT 1 FROM work WHERE id = ?", (spurious,)).fetchone() is None


def test_role_toggle_author_to_translator(db):
    eid = _edition(db)
    _work(db, eid, "W", 1, authors=["Person X"])
    draft = cr.draft_from_edition(db, eid)
    draft["works"][0]["contributors"][0]["role"] = "translator"
    cr.apply_draft(db, eid, draft)
    after = cr.draft_from_edition(db, eid)
    c = after["works"][0]["contributors"][0]
    assert c["role"] == "translator"
    # the edition's translator set now holds that person (book-level home).
    tpid = db.execute("SELECT person_id FROM edition_translator "
                      "WHERE edition_id = ?", (eid,)).fetchone()[0]
    assert tpid == c["person_id"]


def test_add_new_work_via_draft(db):
    eid = _edition(db)
    _work(db, eid, "Existing", 1, authors=["Author A"])
    draft = cr.draft_from_edition(db, eid)
    draft["works"].append({"included": True, "work_id": None, "title": "Brand New",
                           "kind": "work", "locator": "p. 50",
                           "contributors": [{"name": "Author B", "role": "author"}]})
    res = cr.apply_draft(db, eid, draft)
    assert len(res["works_created"]) == 1
    after = cr.draft_from_edition(db, eid)
    assert sorted(w["title"] for w in after["works"]) == ["Brand New", "Existing"]
    assert after["structure"] == "multi_work"


def test_draft_from_payload(db):
    payload = {"structure": "multi_work",
               "book_authors": ["Editor E"],
               "works": [{"title": "T1", "kind": "root", "authors": ["A1"],
                          "translators": ["Tr1"]},
                         {"title": "T2", "authors": ["A2"]}]}
    draft = cr.draft_from_payload(payload)
    assert draft["structure"] == "multi_work"
    assert len(draft["works"]) == 2
    assert {"name": "A1", "role": "author", "person_id": None} in draft["works"][0]["contributors"]
    assert {"name": "Tr1", "role": "translator", "person_id": None} in draft["works"][0]["contributors"]
    assert draft["book_contributors"][0]["name"] == "Editor E"


def test_parse_works_form_to_payload_roundtrip():
    # Simulates the submitted curation widget (a plain dict — no Flask).
    form = {
        "structure": "multi_work",
        "bc0_name": "Editor E", "bc0_role": "author",
        "w0_included": "on", "w0_work_id": "7", "w0_title": "First",
        "w0_kind": "root", "w0_c0_name": "A1", "w0_c0_role": "author",
        "w0_c1_name": "Tr1", "w0_c1_role": "translator", "w0_locator": "pp.1-10",
        "w1_included": "on", "w1_title": "Second", "w1_c0_name": "A2",
        "w1_c0_role": "author",
        # spare blank row → dropped
        "w2_title": "", "w2_c0_name": "",
    }
    draft = cr.parse_works_form(form)
    assert len(draft["works"]) == 2
    assert draft["works"][0]["work_id"] == 7
    assert draft["works"][0]["included"] is True
    payload = cr.draft_to_payload(draft)
    assert payload["structure"] == "multi_work"
    assert payload["book_authors"] == ["Editor E"]
    assert payload["works"][0]["title"] == "First"
    assert payload["works"][0]["authors"] == ["A1"]
    assert payload["works"][0]["translators"] == ["Tr1"]
    assert payload["works"][1]["title"] == "Second"


def test_parse_form_excluded_work_dropped_from_payload():
    form = {"w0_included": "on", "w0_title": "Keep", "w0_c0_name": "A",
            "w0_c0_role": "author",
            "w1_title": "Drop", "w1_c0_name": "B", "w1_c0_role": "author"}  # no w1_included
    draft = cr.parse_works_form(form)
    assert [w["included"] for w in draft["works"]] == [True, False]
    payload = cr.draft_to_payload(draft)
    assert [w["title"] for w in payload["works"]] == ["Keep"]


def test_plan_apply_draft_preview(db):
    eid = _edition(db)
    keep = _work(db, eid, "Keep", 1, authors=["A"])
    drop = _work(db, eid, "Drop", 2)
    draft = cr.draft_from_edition(db, eid)
    for w in draft["works"]:
        if w["work_id"] == drop:
            w["included"] = False
    plan = cr.plan_apply_draft(db, eid, draft)
    assert plan["works_removed"] == ["Drop"]
    assert plan["works_kept"] == ["Keep"]
    assert plan["structure_after"] == "single_work"
    # plan did not mutate
    assert db.execute("SELECT count(*) FROM edition_work WHERE edition_id = ?",
                      (eid,)).fetchone()[0] == 2
