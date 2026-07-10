"""§14.6/§14.7 capture contract v2 — cross-format verdict + no-ISBN search.

Black-box over HTTP: a scan response tells the phone whether the book is already
in the catalogue (in any format), and /capture/find answers the same for a book
with no scannable ISBN. Resolvers are injected so the suite stays offline.
"""
from __future__ import annotations


def test_scan_response_is_backcompatible_plus_verdict(app_env):
    c, app, _ = app_env
    r = c.post("/capture", json={"isbn": "9780861711765", "source": "ios"})
    assert r.status_code == 201
    body = r.get_json()
    # v1 keys still present (old apps keep working)...
    for k in ("status", "staging_id", "isbn", "duplicate"):
        assert k in body
    # ...plus the v2 verdict keys.
    for k in ("in_catalogue", "matched_by", "editions"):
        assert k in body
    assert body["in_catalogue"] is False   # empty catalogue + offline resolvers


def test_scan_stages_even_when_work_key_lookup_raises(app_env):
    c, app, _ = app_env
    def boom(_isbn):
        raise TimeoutError("openlibrary down")
    app.config["ISBN_WORK_KEY_LOOKUP"] = boom
    app.config["ISBN_LOOKUP"] = boom
    r = c.post("/capture", json={"isbn": "9780861711765", "source": "ios"})
    assert r.status_code == 201            # the scan is never lost to a lookup failure
    assert r.get_json()["in_catalogue"] is False


def test_cross_format_work_key_verdict(app_env, seed):
    c, app, _ = app_env
    # We already hold the epub (different ISBN) under work key /works/OL1W.
    seed("INSERT INTO edition (id, title, isbn, ol_work_key) "
         "VALUES (10, 'The Way of the Bodhisattva', '9782222222227', '/works/OL1W')")
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (10, 'electronic', 'x.epub')")
    app.config["ISBN_WORK_KEY_LOOKUP"] = lambda _i: "/works/OL1W"
    # Scan the PRINT ISBN we don't hold; its work key matches the epub.
    r = c.post("/capture", json={"isbn": "9780861711765", "source": "ios"})
    body = r.get_json()
    assert body["in_catalogue"] is True
    assert body["matched_by"] == "work_key"
    assert body["editions"][0]["forms"] == ["epub"]


def test_verdict_matches_isbn_on_a_holding(app_env, seed):
    # The schema's canonical home for an ISBN is the holding (a format/printing),
    # not the edition. A scan of an ISBN held only on a holding must still match.
    c, _, _ = app_env
    seed("INSERT INTO edition (id, title) VALUES (40, 'Four Hundred Stanzas')")
    seed("INSERT INTO holding (edition_id, form, file_path, isbn, holding_type) "
         "VALUES (40, 'electronic', 'x.pdf', '9781559393027', 'pdf')")
    body = c.post("/capture", json={"isbn": "9781559393027", "source": "ios"}).get_json()
    assert body["in_catalogue"] is True
    assert body["matched_by"] == "isbn"
    assert body["editions"][0]["id"] == 40


def test_verdict_matches_alternate_isbn_link(app_env, seed):
    # edition_isbn records "this edition is also known under this ISBN" (a variant
    # printing) without implying a copy. A scan of that ISBN resolves to the edition.
    c, _, _ = app_env
    seed("INSERT INTO edition (id, title, isbn) VALUES (41, 'Four Hundred Stanzas', '9781559390194')")
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (41, 'electronic', 'y.pdf')")
    seed("INSERT INTO edition_isbn (edition_id, isbn, note) "
         "VALUES (41, '9781559393027', 'variant printing')")
    body = c.post("/capture", json={"isbn": "9781559393027", "source": "ios"}).get_json()
    assert body["in_catalogue"] is True
    assert body["matched_by"] == "isbn"
    assert body["editions"][0]["id"] == 41
    # And the primary edition ISBN still matches, as before.
    assert c.post("/capture", json={"isbn": "9781559390194"}).get_json()["in_catalogue"] is True


def test_capture_version_is_current(app_env):
    c, _, _ = app_env
    assert c.get("/capture/version").get_json() == {"contract_version": "4"}


def test_find_by_title_for_no_isbn_book(app_env, seed):
    c, _, _ = app_env
    seed("INSERT INTO edition (id, title) VALUES (20, 'Bodhicaryavatara')")
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (20, 'electronic', 'b.pdf')")
    matches = c.get("/capture/find?q=Bodhicaryavatara").get_json()["matches"]
    assert any(m["id"] == 20 for m in matches)
    assert all("forms" in m for m in matches)


def test_find_miss_returns_empty(app_env):
    c, _, _ = app_env
    assert c.get("/capture/find?q=zzznotathing").get_json()["matches"] == []
    assert c.get("/capture/find").get_json()["matches"] == []   # blank query
