"""Per-edition single/multi-work classification: domain helpers, the CLI, and
the /editions/structure checkbox tool."""
import json

import pytest

from catalogue.db_store import connect, init_db
from catalogue.services import edition_structure as ES
from catalogue.cli import edition_structure as ES_CLI
from catalogue.webui.web import create_app


def _edition(db, title="Book"):
    return db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


def _holding(db, eid):
    return db.execute(
        "INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/x/a.pdf')",
        (eid,)).lastrowid


def _proposal(db, holding_id, structure):
    db.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('book_toc_pattern', ?)",
               (json.dumps({"holding_id": holding_id, "structure": structure}),))


def test_proposal_guess_maps_to_binary(tmp_path):
    db = init_db(tmp_path / "c.db")
    e1 = _edition(db); h1 = _holding(db, e1); _proposal(db, h1, "multi_work")
    e2 = _edition(db); h2 = _holding(db, e2); _proposal(db, h2, "single_work")
    e3 = _edition(db); h3 = _holding(db, e3); _proposal(db, h3, "collection_unsegmented")
    g = ES.proposal_guess(db)
    assert g[e1] == "multi_work"
    assert g[e2] == "single_work"
    assert g[e3] == "single_work"          # unsegmented → single (audit finding)


def test_set_and_seed(tmp_path):
    db = init_db(tmp_path / "c.db")
    e1 = _edition(db); h1 = _holding(db, e1); _proposal(db, h1, "multi_work")
    e2 = _edition(db); h2 = _holding(db, e2); _proposal(db, h2, "single_work")
    ES.set_structure(db, e2, "multi_work")          # operator override before seeding
    n = ES.seed_from_proposals(db, only_unset=True)
    assert n == 1                                   # only e1 was unset
    assert db.execute("SELECT structure FROM edition WHERE id=?", (e1,)).fetchone()[0] == "multi_work"
    assert db.execute("SELECT structure FROM edition WHERE id=?", (e2,)).fetchone()[0] == "multi_work"  # not clobbered


def test_set_structure_rejects_bad_value(tmp_path):
    db = init_db(tmp_path / "c.db")
    e = _edition(db)
    with pytest.raises(ValueError):
        ES.set_structure(db, e, "anthology")


def test_cli_seed_and_explicit(tmp_path, capsys):
    dbp = tmp_path / "c.db"
    db = init_db(dbp)
    e1 = _edition(db, "A"); h1 = _holding(db, e1); _proposal(db, h1, "multi_work")
    e2 = _edition(db, "B"); _holding(db, e2)
    db.commit()
    ES_CLI.main([str(dbp), "--seed"])
    db = connect(dbp)
    assert db.execute("SELECT structure FROM edition WHERE id=?", (e1,)).fetchone()[0] == "multi_work"
    db.close()
    ES_CLI.main([str(dbp), "--single", str(e1), "--multi", str(e2)])
    db = connect(dbp)
    assert db.execute("SELECT structure FROM edition WHERE id=?", (e1,)).fetchone()[0] == "single_work"
    assert db.execute("SELECT structure FROM edition WHERE id=?", (e2,)).fetchone()[0] == "multi_work"


@pytest.fixture
def web(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    e1 = _edition(db, "Anthology"); h1 = _holding(db, e1); _proposal(db, h1, "multi_work")
    e2 = _edition(db, "Single"); _holding(db, e2)
    db.commit()
    with app.test_client() as c:
        yield c, app, e1, e2


def test_structure_page_precheck_and_save(web):
    c, app, e1, e2 = web
    page = c.get("/editions/structure").data
    assert b"Mark multi-work editions" in page
    assert b"Anthology" in page and b"Single" in page

    # ticking e2 (and leaving e1) → e2 multi, e1 single
    c.post("/editions/structure", data={"multi": [str(e2)]})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT structure FROM edition WHERE id=?", (e1,)).fetchone()[0] == "single_work"
    assert db.execute("SELECT structure FROM edition WHERE id=?", (e2,)).fetchone()[0] == "multi_work"


def test_structure_seed_route(web):
    c, app, e1, e2 = web
    c.post("/editions/structure/seed")
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT structure FROM edition WHERE id=?", (e1,)).fetchone()[0] == "multi_work"
