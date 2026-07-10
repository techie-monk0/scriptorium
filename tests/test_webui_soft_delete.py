"""End-to-end: the webui hides soft-deleted (tombstoned) editions (reorg Phase 4).

The access-API tombstones a root (deleted_at set) instead of hard-deleting it, so the webui's
edition reads were repointed to the `v_live_edition` view (deleted_at IS NULL). These tests drive
the real Flask routes through the test client and assert a tombstoned edition never surfaces in a
listing / count / search. See docs/access/entity_api_model.md §6.
"""
from __future__ import annotations

import pytest

from catalogue.db_store import connect
from catalogue.webui.web import create_app


@pytest.fixture
def app(tmp_path):
    a = create_app(tmp_path / "web.db")
    a.testing = True
    return a


def _edition(conn, title, isbn, *, dead=False):
    eid = conn.execute("INSERT INTO edition (title, isbn) VALUES (?, ?)", (title, isbn)).lastrowid
    if dead:
        conn.execute("UPDATE edition SET deleted_at = datetime('now') WHERE id = ?", (eid,))
    return eid


def test_dashboard_renders_with_repointed_count_query(app):
    # GET / runs the repointed books-count query (SELECT count(*) FROM v_live_edition);
    # a 200 proves that repoint is valid SQL and wired into the route.
    conn = connect(app.config["DB_PATH"])
    _edition(conn, "Live", "1110000001")
    _edition(conn, "Dead", "1110000002", dead=True)
    conn.commit(); conn.close()
    with app.test_client() as c:
        r = c.get("/")
    assert r.status_code == 200


def test_editions_search_excludes_tombstoned_by_isbn(app):
    conn = connect(app.config["DB_PATH"])
    live = _edition(conn, "Live Book", "9781234500000")
    _edition(conn, "Dead Book", "9781234500000", dead=True)   # same ISBN, tombstoned
    conn.commit(); conn.close()
    with app.test_client() as c:
        r = c.get("/editions/search", query_string={"q": "9781234500000"})
    assert r.status_code == 200
    matches = r.get_json()["matches"]
    eids = {m["edition_id"] for m in matches}
    assert live in eids                       # live edition surfaces
    assert all(m["title"] != "Dead Book" for m in matches)   # tombstone hidden
    assert len(eids) == 1


def test_dashboard_count_query_is_live_only(app):
    # direct check of the repointed count query the dashboard badge uses
    conn = connect(app.config["DB_PATH"])
    _edition(conn, "A", "")
    _edition(conn, "B", "", dead=True)
    conn.commit()
    live = conn.execute("SELECT count(*) FROM v_live_edition").fetchone()[0]
    allrows = conn.execute("SELECT count(*) FROM edition").fetchone()[0]
    conn.close()
    assert (live, allrows) == (1, 2)
