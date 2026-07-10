"""Step-1.5 regression tests for the web skeleton.

Skeleton contract: every stubbed route renders 200, /search routes through
the SearchService, /health reports the init gate result, and /capture
writes through to capture_staging.
"""
from __future__ import annotations

import pytest

from catalogue.webui.web import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "web.db")
    app.testing = True
    with app.test_client() as c:
        yield c, app


def test_all_stub_routes_return_200(client):
    c, _ = client
    for path in ("/", "/review/subjects", "/review", "/review-queue",
                 "/capture", "/search", "/text"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"


def test_health_reports_sqlite_source(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["sqlite_source"] in ("stdlib", "pysqlite3")


def test_capture_post_writes_to_staging(client):
    c, app = client
    r = c.post("/capture", data={"isbn": "9780000000001", "note": "shelf 3"})
    assert r.status_code == 200
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    row = conn.execute(
        "SELECT form, raw_isbn, free_text_note FROM capture_staging"
    ).fetchone()
    conn.close()
    assert row == ("physical", "9780000000001", "shelf 3")


def test_work_detail_shows_contributors_and_holdings(client):
    c, app = client
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO work (id) VALUES (1)")
    conn.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
                 "VALUES (1, 'Bodhicaryavatara', 'english', 'bodicaryavatara')")
    conn.execute("INSERT INTO person (id, primary_name) VALUES (10, 'Śāntideva')")
    conn.execute("INSERT INTO person (id, primary_name) VALUES (11, 'A Translator')")
    conn.execute("INSERT INTO work_author (work_id, person_id, role) "
                 "VALUES (1, 10, 'author')")
    conn.execute("INSERT INTO edition (id, title) VALUES (5, 'Way of the Bodhisattva')")
    conn.execute("INSERT INTO holding (id, edition_id, form, file_path) "
                 "VALUES (50, 5, 'electronic', '/tmp/x.pdf')")
    conn.execute("INSERT INTO edition_work (edition_id, work_id, sequence, "
                 "translator_person_id) VALUES (5, 1, 1, 11)")
    conn.commit()
    conn.close()
    r = c.get("/work/1")
    assert r.status_code == 200
    body = r.data
    # authors/translators link to their person pages
    assert b"/person/10" in body and b"/person/11" in body
    assert "Śāntideva".encode() in body
    # edition + open-in-viewer control for the holding
    assert b"/edition/5" in body and b"Way of the Bodhisattva" in body
    assert b"openHolding(50" in body
    # header shows the work's NAME, not a bare 'Work #1'
    assert b"Bodhicaryavatara" in body
    assert b"Work #1" not in body
    # the inline card fragment (browse/review) carries the same name+author header
    card = c.get("/work/1/card").data
    assert b"Bodhicaryavatara" in card and b"/person/10" in card and b"Work #1" not in card


def test_people_and_person_detail_render(client):
    c, app = client
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO person (id, primary_name, verification_status) "
                 "VALUES (7, 'Nagarjuna', 'verified')")
    conn.execute("INSERT INTO person_external_id (person_id, scheme, value) "
                 "VALUES (7, 'bdrc', 'bdr:P4954')")
    conn.commit()
    conn.close()
    r = c.get("/people")
    assert r.status_code == 200 and b"Nagarjuna" in r.data
    r = c.get("/person/7")
    assert r.status_code == 200
    # cross-link rendered as a click-through to the BDRC page
    assert b"bdr:P4954" in r.data and b"purl.bdrc.io/resource/P4954" in r.data


def test_person_detail_lists_editions_with_roles(client):
    c, app = client
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO person (id, primary_name) VALUES (20, 'Nagarjuna')")
    # An anthology whose contained work Nagarjuna authored.
    conn.execute("INSERT INTO work (id) VALUES (1)")
    conn.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (1, 20, 'author')")
    conn.execute("INSERT INTO edition (id, title, year) VALUES (5, 'Collected Treatises', 1995)")
    conn.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (5, 1, 1)")
    # A second book he translated.
    conn.execute("INSERT INTO edition (id, title) VALUES (6, 'A Translation')")
    conn.execute("INSERT INTO edition_translator (edition_id, person_id) VALUES (6, 20)")
    # A third book he is the book-level author of.
    conn.execute("INSERT INTO edition (id, title) VALUES (7, 'His Own Book')")
    conn.execute("INSERT INTO edition_author (edition_id, person_id, role) VALUES (7, 20, 'author')")
    conn.commit()
    conn.close()
    r = c.get("/person/20")
    assert r.status_code == 200
    body = r.data
    assert b"<h3>Editions</h3>" in body
    for eid, title in ((5, b"Collected Treatises"), (6, b"A Translation"), (7, b"His Own Book")):
        assert f"/edition/{eid}".encode() in body and title in body
    # book-level author vs. author of a work merely contained in the edition
    assert b"(translator)" in body and b"(author)" in body
    assert b"(author (contained work))" in body


def test_person_authority_review_renders_and_accepts(client):
    import json
    from catalogue.db_store import connect
    c, app = client
    conn = connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO person (id, primary_name) VALUES (3, 'Nagarjuna')")
    conn.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
                 "VALUES (3, 'Nagarjuna', 'english', 'nagarjuna')")
    conn.execute("INSERT INTO review_queue (id, item_type, payload_json) VALUES "
                 "(5, 'person_authority', ?)",
                 (json.dumps({"person_id": 3, "candidate_id": "bdr:P4954",
                              "canonical_name": "Nāgārjuna", "aliases": [],
                              "verifier": "bdrc", "reason": "bdrc_blmp_fuzzy"}),))
    conn.commit()
    conn.close()
    # the rich accept/reject card renders (not the generic JSON view)
    r = c.get("/review-queue/5")
    assert r.status_code == 200 and b"Accept" in r.data and b"bdr:P4954" in r.data
    # accept binds the candidate end-to-end over HTTP
    r = c.post("/review-queue/5/authority/accept")
    assert r.status_code in (200, 302)
    conn = connect(app.config["DB_PATH"])
    ext = conn.execute("SELECT external_id FROM person WHERE id=3").fetchone()[0]
    st = conn.execute("SELECT status FROM review_queue WHERE id=5").fetchone()[0]
    conn.close()
    assert ext == "bdr:P4954" and st == "resolved"


def test_search_route_uses_service_and_keeps_diacritics(client):
    c, app = client
    # Seed text directly via the app's DB path.
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO edition (id, title) VALUES (1, 'e')")
    conn.execute(
        "INSERT INTO edition_text (edition_id, page, content) VALUES (1, 1, ?)",
        ("Śāntideva, the author.",),
    )
    conn.commit()
    conn.close()

    # Content search is client-rendered now; the data lives in the JSON contract.
    doc = c.get("/api/v1/content?q=santideva").get_json()
    snippets = " ".join(s for b in doc["books"] for s in b["snippets"])
    # Diacritics survive into the snippet (folding is index-only, §4.5).
    assert "Śāntideva" in snippets


# `test_search_service_is_swappable_on_the_app` deleted — it reached into
# `app.config["SEARCH"]` (implementation, not plan). Behavior coverage:
# tests/system/test_search.py.
