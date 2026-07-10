"""Black-box tests for the shared-frontend JSON contract (`/api/v1/{library,content}`).

These endpoints back the converged frontend layer (web client-render + PWA + native):
they must return stable shapes regardless of surface.
(The `/api/v1/find` "Browse" endpoint was removed with the `/find` surface — the
Search page covers it; the diacritic-fold behaviour is pinned in test_catalogue_web.)
"""
from __future__ import annotations


def test_api_library_browse_and_search(app_env, seed):
    c, _, _ = app_env
    eid = seed("INSERT INTO edition (title) VALUES ('Stages of Meditation')").lastrowid
    # No query → newest-first browse list.
    rows = c.get("/api/v1/library").get_json()["rows"]
    assert any(r["id"] == eid for r in rows)
    row = next(r for r in rows if r["id"] == eid)
    assert set(row) >= {"id", "title", "subtitle", "done", "holding_id", "has_file", "file_ext"}
    # With a title query → filtered.
    hit_rows = c.get("/api/v1/library?q=Meditation").get_json()["rows"]
    assert any(r["id"] == eid for r in hit_rows)
    miss_rows = c.get("/api/v1/library?q=nonexistent-zzz").get_json()["rows"]
    assert all(r["id"] != eid for r in miss_rows)


def test_api_content_groups_by_edition_with_snippets(app_env, seed):
    c, _, _ = app_env
    eid = seed("INSERT INTO edition (title) VALUES ('Lamp for the Path')").lastrowid
    # edition_text rows feed the FTS5 index via its triggers.
    seed("INSERT INTO edition_text (edition_id, page, content) VALUES (?, 1, ?)",
         (eid, "The bodhisattva cultivates patience and wisdom across many lifetimes."))
    doc = c.get("/api/v1/content?q=patience").get_json()
    assert doc["available"] is True
    assert doc["q"] == "patience"
    book = next(b for b in doc["books"] if b["eid"] == eid)
    assert set(book) == {"eid", "title", "authors", "snippets"}
    assert book["title"] == "Lamp for the Path"
    assert book["snippets"] and "[" in book["snippets"][0]    # FTS snippet() highlight markers


def test_api_content_blank_is_empty(app_env):
    c, _, _ = app_env
    assert c.get("/api/v1/content?q=").get_json() == {"q": "", "books": [], "available": True}


def test_api_edition_returns_full_detail(app_env, seed):
    c, _, _ = app_env
    eid = seed("INSERT INTO edition (title, volume, publisher, year) "
               "VALUES ('Steps on the Path', '4', 'Wisdom', 2014)").lastrowid
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/lib/s.pdf')", (eid,))
    row = c.get(f"/api/v1/edition/{eid}").get_json()
    assert row["edition_id"] == eid
    assert row["display_title"] == "Steps on the Path · vol. 4"   # volume-aware, the one shared rule
    assert row["publisher"] == "Wisdom" and row["year"] == 2014
    # same per-edition shape as a replica row (the read-only Book-detail contract)
    assert {"authors", "translators", "subjects", "isbns", "holdings",
            "cover_url", "spine_url"} <= set(row)
    assert row["cover_url"] == f"/edition/{eid}/cover.jpg"
    assert row["holdings"] and row["holdings"][0]["has_file"] is True


def test_api_edition_404_for_missing(app_env):
    c, _, _ = app_env
    assert c.get("/api/v1/edition/999999").status_code == 404
