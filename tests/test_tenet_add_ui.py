"""The add-person and add-work forms carry a `tenet_system` textbox, and submitting it
persists to person.tenet_system / work.tenet_system (a base column mirroring `tradition`).
"""
from __future__ import annotations

import pytest

from catalogue.db_store import connect
from catalogue.webui.web import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "web.db")
    app.testing = True
    with app.test_client() as c:
        yield c, app


def test_new_work_and_person_forms_show_tenet_textbox(client):
    c, _ = client
    assert b'name="tenet_system"' in c.get("/works").data
    assert b'name="tenet_system"' in c.get("/people").data


def test_creating_a_work_saves_tenet_system(client):
    c, app = client
    r = c.post("/works/new", data={"seed_alias": "Madhyamakāvatāra",
                                   "tenet_system": "Prāsaṅgika-Madhyamaka"})
    assert r.status_code == 302
    conn = connect(app.config["DB_PATH"])
    row = conn.execute("SELECT tenet_system FROM work ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert row[0] == "Prāsaṅgika-Madhyamaka"


def test_creating_a_person_saves_tenet_system(client):
    c, app = client
    r = c.post("/people/new", data={"primary_name": "Candrakīrti",
                                    "tenet_system": "Prāsaṅgika-Madhyamaka"})
    assert r.status_code == 302
    conn = connect(app.config["DB_PATH"])
    row = conn.execute("SELECT tenet_system FROM person WHERE primary_name = ?",
                       ("Candrakīrti",)).fetchone()
    conn.close()
    assert row[0] == "Prāsaṅgika-Madhyamaka"
