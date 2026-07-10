"""Delete/merge an edition or work from the review pane (reversible, persons-style),
plus the action-bar layout + edition search. Hermetic Flask client."""
import pytest

from catalogue.db_store import connect
from catalogue.services import work_detect as WD
from catalogue.webui.web import create_app


def _single(db, title, author="A", isbn=None):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (author,)).lastrowid
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (wid, pid))
    eid = db.execute("INSERT INTO edition (title, structure, isbn) VALUES (?, 'single_work', ?)",
                     (title, isbn)).lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, wid))
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/x.pdf')", (eid,))
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))
    return eid, wid


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    return app


def test_detail_has_persons_style_action_bar(app):
    db = connect(app.config["DB_PATH"])
    _single(db, "A Book"); db.commit()
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
    assert 'picker-actions' in page and 'pa-btn' in page          # same layout as persons
    assert 'data-key="m"' in page and 'data-key="x"' in page      # merge / delete keys
    assert 'mergeEdition(' in page and 'deleteEdition(' in page    # edition-level ops
    assert 'deleteWork(' not in page and 'mergeWork(' not in page  # work delete/merge are work-level only


def test_reclassify_single_to_multi_and_back(app):
    """The 'multi-work edition' checkbox flips edition.structure both ways; a marked book
    stays in the single pane (own 'Multi-work' group, single-apply off) so it's reachable
    to flip back — it does NOT vanish."""
    db = connect(app.config["DB_PATH"])
    eid, _ = _single(db, "An Anthology In Disguise"); db.commit()
    with app.test_client() as c:
        # default single → the checkbox is present + UNCHECKED, Apply is offered
        page = c.get("/works/detect/single").data.decode()
        assert 'toggleMultiWork(' in page and 'multi-work edition' in page
        assert 'applyEdition(' in page

        # check it → multi_work
        r = c.post(f"/works/detect/{eid}/structure", json={"multi": "1"}).get_json()
        assert r["structure"] == "multi_work"
        assert connect(app.config["DB_PATH"]).execute(
            "SELECT structure FROM edition WHERE id=?", (eid,)).fetchone()[0] == "multi_work"
        # still listed (didn't disappear), now in the Multi-work group + single-apply note
        page = c.get("/works/detect/single").data.decode()
        assert f'id="i{eid}"' in page
        assert 'Multi-work' in page and 'single-apply is off' in page

        # uncheck → single_work again
        r = c.post(f"/works/detect/{eid}/structure", json={"multi": ""}).get_json()
        assert r["structure"] == "single_work"
    assert connect(app.config["DB_PATH"]).execute(
        "SELECT structure FROM edition WHERE id=?", (eid,)).fetchone()[0] == "single_work"


def test_delete_edition_and_undo(app):
    db = connect(app.config["DB_PATH"])
    eid, wid = _single(db, "Junk"); db.commit()
    with app.test_client() as c:
        r = c.post(f"/works/detect/{eid}/delete-edition").get_json()
        assert r["status"] == "deleted" and r["undo_token"]
        assert connect(app.config["DB_PATH"]).execute(
            "SELECT COUNT(*) FROM edition WHERE id=?", (eid,)).fetchone()[0] == 0
        c.post("/works/detect/undo", json={"token": r["undo_token"]})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM edition WHERE id=?", (eid,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM work_detection WHERE edition_id=?", (eid,)).fetchone()[0] == 1


def test_bulk_subject_assigns_and_lifts_uncategorized(app):
    from catalogue.services import subjects as S
    db = connect(app.config["DB_PATH"])
    e1, _ = _single(db, "One"); e2, _ = _single(db, "Two")
    S.add_subject(db, "edition", e1, "Uncategorized"); db.commit()   # placeholder to lift
    with app.test_client() as c:
        r = c.post("/works/detect/bulk-subject",
                   json={"ids": [e1, e2], "name": "Madhyamaka"}).get_json()
    assert sorted(r["assigned"]) == sorted([e1, e2]) and not r["errors"]
    db = connect(app.config["DB_PATH"])
    for eid in (e1, e2):
        assert "Madhyamaka" in [n for _, n in S.subjects_for(db, "edition", eid)]
    assert "Uncategorized" not in [n for _, n in S.subjects_for(db, "edition", e1)]


def test_bulk_subject_requires_name_and_ids(app):
    db = connect(app.config["DB_PATH"])
    e1, _ = _single(db, "One"); db.commit()
    with app.test_client() as c:
        assert c.post("/works/detect/bulk-subject", json={"ids": [e1], "name": ""}).status_code == 400
        assert c.post("/works/detect/bulk-subject", json={"ids": [], "name": "X"}).status_code == 400


def test_bulk_delete_editions_snapshots_and_is_reversible(app, tmp_path):
    db = connect(app.config["DB_PATH"])
    e1, _ = _single(db, "Junk1"); e2, _ = _single(db, "Junk2"); db.commit()
    with app.test_client() as c:
        r = c.post("/works/detect/bulk-delete-editions", json={"ids": [e1, e2]}).get_json()
    assert {d["edition_id"] for d in r["deleted"]} == {e1, e2}
    assert all(d["undo_token"] for d in r["deleted"]) and not r["errors"]
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM edition WHERE id IN (?,?)", (e1, e2)).fetchone()[0] == 0
    assert list(tmp_path.glob("cat-backup-*.db")), "expected a pre-bulk-delete DB snapshot"
    # each delete is still individually reversible via the per-row undo token
    tok = next(d["undo_token"] for d in r["deleted"] if d["edition_id"] == e1)
    with app.test_client() as c:
        c.post("/works/detect/undo", json={"token": tok})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM edition WHERE id=?", (e1,)).fetchone()[0] == 1


def test_merge_edition_route(app):
    db = connect(app.config["DB_PATH"])
    dup, _ = _single(db, "Dup", isbn="9"); into, _ = _single(db, "Keep", isbn="9"); db.commit()
    with app.test_client() as c:
        r = c.post(f"/works/detect/{dup}/merge-edition", json={"into": into}).get_json()
        assert r["status"] == "merged"
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM edition WHERE id=?", (dup,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM holding WHERE edition_id=?", (into,)).fetchone()[0] == 2


def test_work_delete_and_merge_routes(app):
    db = connect(app.config["DB_PATH"])
    eid, wid = _single(db, "Book")
    w2 = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'Other', 'english', 'other')", (w2,))
    db.commit()
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'Unlinked', 'english', 'unlinked')",
               (db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid,))
    db.commit()
    unlinked = connect(app.config["DB_PATH"]).execute(
        "SELECT work_id FROM work_alias WHERE normalized_key='unlinked'").fetchone()[0]
    with app.test_client() as c:
        r = c.post(f"/work/{wid}/merge", json={"into": w2}).get_json()
        assert r["status"] == "merged"                            # wid folded into w2, eid re-pointed
        assert connect(app.config["DB_PATH"]).execute(
            "SELECT COUNT(*) FROM work WHERE id=?", (wid,)).fetchone()[0] == 0
        assert connect(app.config["DB_PATH"]).execute(
            "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,)).fetchone()[0] == w2
        # w2 is now eid's ONLY work → deleting it is refused with the blocking edition
        blocked = c.post(f"/work/{w2}/delete").get_json()
        assert "blocking_editions" in blocked and blocked["blocking_editions"][0]["id"] == eid
        # an unlinked work deletes fine
        d = c.post(f"/work/{unlinked}/delete").get_json()
        assert d["status"] == "deleted" and d["undo_token"]


def test_editions_search(app):
    db = connect(app.config["DB_PATH"])
    _single(db, "Ocean of Reasoning", isbn="978111"); db.commit()
    with app.test_client() as c:
        j = c.get("/editions/search?q=ocean").get_json()
        assert any(m["title"] == "Ocean of Reasoning" for m in j["matches"])
        j2 = c.get("/editions/search?q=978111").get_json()
        assert j2["matches"]                                       # ISBN hit
