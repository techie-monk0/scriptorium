"""Review queue for works with incomplete data (the safety net for frictionless adds)."""
import pytest

from catalogue.db_store import init_db, connect, add_alias
from catalogue.db_store import contributor_store as cs
from catalogue.services import work_review as WR, subjects as S
from catalogue.webui.web import create_app


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "c.db")
    yield conn
    conn.close()


def _bare_work(db, title="Typed Root"):
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", wid, title, "english")
    return wid


def test_bare_work_is_incomplete(db):
    w = _bare_work(db)
    assert set(WR.work_reasons(db, w)) == {
        "no subject", "no author", "no canonical# / native title"}
    assert any(x["id"] == w for x in WR.incomplete_works(db))


def test_complete_work_is_not_listed(db):
    w = _bare_work(db, "Mūlamadhyamakakārikā")
    db.execute("UPDATE work SET canonical_system='toh', canonical_number='3824', "
               "work_type='root' WHERE id=?", (w,))
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Nāgārjuna')").lastrowid
    cs.add_work_author(db, w, pid)
    S.add_subject(db, "work", w, "Madhyamaka")
    db.commit()
    assert WR.work_reasons(db, w) == []
    assert WR.incomplete_works(db) == []


def test_mark_reviewed_clears_from_queue(db):
    w = _bare_work(db)                                   # incomplete but operator vouches
    assert WR.incomplete_works(db)
    WR.set_review(db, w, "ok")
    assert WR.incomplete_works(db) == []                 # 'ok' clears it despite missing data
    WR.set_review(db, w, None)
    assert any(x["id"] == w for x in WR.incomplete_works(db))   # un-reviewing brings it back


def test_web_queue_and_mark(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    w = _bare_work(db); db.commit()
    with app.test_client() as c:
        page = c.get("/works/incomplete").data.decode()
        assert "Works needing review" in page and "Typed Root" in page
        assert "no subject" in page and "markWorkReviewed(" in page
        r = c.post(f"/work/{w}/review", json={"status": "ok"}).get_json()
        assert r["ok"]
    assert connect(app.config["DB_PATH"]).execute(
        "SELECT review_status FROM work WHERE id=?", (w,)).fetchone()[0] == "ok"


def test_dashboard_surfaces_review_backlogs(tmp_path):
    from catalogue.services import work_detect as WD
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    _bare_work(db)                                       # one incomplete work
    # one single-work edition with an unapplied detection
    eid = db.execute("INSERT INTO edition (title, structure) VALUES ('Bk', 'single_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/x.pdf')", (eid,))
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {"english": "Bk"}))
    db.commit()
    with app.test_client() as c:
        page = c.get("/").data.decode()
        # The splash surfaces a pending backlog as a status strip (a Review chip with
        # the count, linking to the editable Review module).
        assert 'href="/review"' in page
        assert 'class="status-strip"' in page and "Review" in page
        # And the Review hub itself links the individual queues via its tab strip.
        hub = c.get("/works/detect/single").data.decode()
    assert "/works/incomplete" in hub and "/works/detect/single" in hub and "/picker/person" in hub
