"""Capture 'resolve' must never silently create a duplicate.

Regression for the Mind Seeing Mind case: a scan whose ISBN differs from an
already-catalogued printing (and whose contributors we haven't recorded yet)
still surfaces that edition as a pickable match, and confirming the match closes
the capture row WITHOUT adding a new edition/holding.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import connect
from catalogue.webui.web import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "web.db")
    app.testing = True
    with app.test_client() as c:
        yield c, app


def _seed_edition(app, title, *, isbn=None, publisher=None):
    conn = connect(app.config["DB_PATH"])
    cur = conn.execute(
        "INSERT INTO edition(title, isbn, publisher) VALUES (?,?,?)",
        (title, isbn, publisher))
    conn.commit()
    eid = cur.lastrowid
    conn.close()
    return eid


def _stage(app, raw_isbn, metadata, *, form="physical"):
    conn = connect(app.config["DB_PATH"])
    cur = conn.execute(
        "INSERT INTO capture_staging(form, raw_isbn, metadata_json, status) "
        "VALUES (?,?,?,'pending')",
        (form, raw_isbn, json.dumps(metadata)))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def _edition_count(app):
    conn = connect(app.config["DB_PATH"])
    n = conn.execute("SELECT count(*) FROM edition").fetchone()[0]
    conn.close()
    return n


def _staging_status(app, sid):
    conn = connect(app.config["DB_PATH"])
    s = conn.execute("SELECT status FROM capture_staging WHERE id=?", (sid,)).fetchone()[0]
    conn.close()
    return s


def test_resolve_surfaces_title_match_and_adds_nothing(client):
    c, app = client
    # Same book, DIFFERENT ISBN, no contributors recorded — the exact shape that
    # slipped past the old exact-ISBN-only suggestion.
    eid = _seed_edition(
        app, "Mind Seeing Mind: Mahamudra and the Geluk Tradition",
        isbn="9781614296010", publisher="Wisdom Publications")
    sid = _stage(app, "9781614295778", {
        "title": "Mind Seeing Mind", "authors": ["Roger R. Jackson"],
        "publishers": ["Wisdom Publications"]})

    # Detail page offers the existing edition as a pickable match.
    r = c.get(f"/staging/{sid}")
    assert r.status_code == 200
    assert f'value="match:{eid}"' in r.get_data(as_text=True)

    # Guard: resolving with no choice, while a match is on the table, creates nothing.
    before = _edition_count(app)
    r = c.post(f"/staging/{sid}/resolve", data={})
    assert r.status_code == 200                     # re-rendered with the notice
    assert _edition_count(app) == before
    assert _staging_status(app, sid) == "pending"

    # Confirm the match → capture row closed, NOTHING added to the catalogue.
    r = c.post(f"/staging/{sid}/resolve", data={"resolution": f"match:{eid}"})
    assert r.status_code in (301, 302)
    assert f"/edition/{eid}" in r.headers["Location"]
    assert _edition_count(app) == before            # no duplicate edition
    assert _staging_status(app, sid) == "resolved"


def test_bare_resolve_click_adds_nothing_even_with_no_match(client):
    # A stray Resolve with nothing selected must never write to the DB — no
    # accidental "Untitled"/Uncategorized edition — even when there are no matches.
    c, app = client
    sid = _stage(app, "9990000000024", {"title": "A Wholly Unique Title QZX"})
    before = _edition_count(app)
    r = c.post(f"/staging/{sid}/resolve", data={})
    assert r.status_code == 200                      # re-rendered with the prompt
    assert _edition_count(app) == before             # nothing created
    assert _staging_status(app, sid) == "pending"    # still open


def test_discard_deletes_capture_and_adds_nothing(client):
    c, app = client
    sid = _stage(app, "9990000000031", {"title": "A Mis-scan"})
    before = _edition_count(app)
    r = c.post(f"/staging/{sid}/discard")
    assert r.status_code in (301, 302)
    assert r.headers["Location"].rstrip("/").endswith("/capture")   # back to Capture, refreshed
    assert _edition_count(app) == before             # nothing catalogued
    conn = connect(app.config["DB_PATH"])
    gone = conn.execute("SELECT count(*) FROM capture_staging WHERE id=?", (sid,)).fetchone()[0]
    conn.close()
    assert gone == 0                                 # the capture row is removed


def test_capture_badge_excludes_already_matched(client):
    # The home Capture pill counts only scans that still NEED resolving — not ones
    # whose at-capture verdict already matched an existing edition (in_catalogue=1).
    c, app = client
    conn = connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO capture_staging(form, raw_isbn, status, in_catalogue) "
                 "VALUES ('physical','9990000000041','raw',NULL)")   # never checked → counts
    conn.execute("INSERT INTO capture_staging(form, raw_isbn, status, in_catalogue) "
                 "VALUES ('physical','9990000000042','raw',0)")      # not matched → counts
    conn.execute("INSERT INTO capture_staging(form, raw_isbn, status, in_catalogue) "
                 "VALUES ('physical','9990000000043','raw',1)")      # already matched → excluded
    conn.commit()
    conn.close()

    from catalogue.access_api import system_conn
    conn = connect(app.config["DB_PATH"])
    acc = system_conn(conn)
    assert acc.capture.raw_count() == 3            # all open captures
    assert acc.capture.unresolved_count() == 2     # the already-matched one is not work to do
    conn.close()


def test_capture_reconcile_clears_held_scans_only(client):
    # A scan whose book the catalogue now holds is auto-resolved out of the inbox;
    # a genuinely-missing scan is left alone.
    from catalogue.services import capture_reconcile
    c, app = client
    _seed_edition(app, "Owned Book", isbn="9780000000055")
    conn = connect(app.config["DB_PATH"])
    held = conn.execute("INSERT INTO capture_staging(form,raw_isbn,status) "
                        "VALUES ('physical','9780000000055','raw')").lastrowid
    miss = conn.execute("INSERT INTO capture_staging(form,raw_isbn,status) "
                        "VALUES ('physical','9780000000099','raw')").lastrowid
    conn.commit()
    conn.close()

    conn = connect(app.config["DB_PATH"])
    n = capture_reconcile.reconcile_captures(conn)
    conn.close()
    assert n == 1

    conn = connect(app.config["DB_PATH"])
    st = dict(conn.execute(
        "SELECT id, status FROM capture_staging WHERE id IN (?,?)", (held, miss)).fetchall())
    conn.close()
    assert st[held] == "resolved"     # already held → cleared from inbox
    assert st[miss] == "raw"          # not in catalogue → stays for resolution


def test_resolve_new_creates_edition_when_explicitly_chosen(client):
    c, app = client
    sid = _stage(app, "9990000000017", {"title": "A Wholly Unique Title QZX"})
    before = _edition_count(app)
    r = c.post(f"/staging/{sid}/resolve", data={
        "resolution": "new", "title": "A Wholly Unique Title QZX",
        "isbn": "9990000000017"})
    assert r.status_code in (301, 302)
    assert _edition_count(app) == before + 1
    assert _staging_status(app, sid) == "resolved"
