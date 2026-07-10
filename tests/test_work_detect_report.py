"""Part B end-to-end: the dry-run CLI fills the cache, the /works/detect report
renders it. Hermetic — no Toh snapshot / network (live_classical degrades to
title-only), so every edition resolves to 'modern' here."""
import pytest

from catalogue.db_store import connect
from catalogue.cli import work_detect as CLI
from catalogue.webui.web import create_app


def _single(db, title, author):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (author,)).lastrowid
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (wid, pid))
    eid = db.execute("INSERT INTO edition (title, structure, isbn) "
                     "VALUES (?, 'single_work', '9780861711765')", (title,)).lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)",
               (eid, wid))
    db.execute("INSERT INTO holding (edition_id, form, file_path) "
               "VALUES (?, 'electronic', '/lib/x.pdf')", (eid,))
    return eid


@pytest.fixture
def app_db(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    e1 = _single(db, "Insight Into Emptiness", "Jampa Tegchok")
    # a multi_work edition is skipped by the single-work CLI pass
    db.execute("INSERT INTO edition (title, structure) VALUES ('Anthology', 'multi_work')")
    db.commit()
    return app, db, e1


def test_cli_fills_cache_skipping_multi(app_db):
    app, db, e1 = app_db
    n, counts = CLI.run(db, limit=None, offline=True)
    assert n == 1                                   # only the single_work edition
    assert counts["modern"] == 1                    # no snapshot/network → modern
    assert db.execute("SELECT kind FROM work_detection WHERE edition_id=?", (e1,)).fetchone()[0] == "single"


def test_report_renders(app_db):
    app, db, e1 = app_db
    CLI.run(db, offline=True)
    with app.test_client() as c:
        page = c.get("/works/detect/single").data
        card = c.get(f"/works/detect/{e1}/edit").data    # author lives in the unified card now
    assert b"Single-work review" in page             # the page <title> (the h1 was dropped)
    assert b"Insight Into Emptiness" in page
    assert b"modern" in page
    assert b"Jampa Tegchok" in card                 # recorded author shown (as a quick-add)


def test_report_groups_classical_modern_alphabetical(tmp_path):
    from catalogue.services import work_detect as WD
    app = create_app(tmp_path / "c.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])

    def ed(title, det, conf=0.0):
        eid = db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
        WD.store_detection(db, eid, "single", {
            "determination": det, "confidence": conf, "title": {"english": title},
            "stored_title": title, "authors_recorded": [], "translators_recorded": [],
            "authors_detected": [], "translators_detected": [], "canonical": {},
            "file": {}})
        return eid

    ed("Zebra Sutra", "classical", 0.9)
    ed("Apple Tantra", "classical", 0.9)
    ed("Mango Manual", "modern")
    ed("Banana Book", "modern")
    db.commit()
    with app.test_client() as c:
        body = c.get("/works/detect/single").data.decode()
    # section headers present
    assert "Classical — 2 editions" in body and "Modern — 2 editions" in body
    # classical section before modern; alphabetical within each
    assert body.index("Classical —") < body.index("Apple Tantra") < body.index("Zebra Sutra")
    assert body.index("Zebra Sutra") < body.index("Modern —")
    assert body.index("Banana Book") < body.index("Mango Manual")


def test_empty_report_ok(tmp_path):
    app = create_app(tmp_path / "c.db", ingest_verify=False)
    app.testing = True
    with app.test_client() as c:
        assert c.get("/works/detect/single").status_code == 200
