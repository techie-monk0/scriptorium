"""Wishlist API (§14.10) — server side.

Exercises POST/GET/PATCH/DELETE /api/v1/wishlist across the three input forms
(ISBN, title/author, CIP text), the ETag/304 cache, the scanner `intent:"wishlist"`
branch of /capture + /capture/cip, and the acquisition loop. All assertions go through
the HTTP surface so every client shares one test target. Resolution fetchers are stubbed
per-test so the suite stays offline.
"""
from __future__ import annotations

import pytest

_ISBN = "9780061575594"
_META = {"title": "Being and Time", "authors": ["Martin Heidegger"],
         "publishers": ["Harper"], "publish_date": "1962"}
_CIP = ("## copyright page\nLibrary of Congress Cataloging-in-Publication Data\n"
        "Title: Being and time / Martin Heidegger.\nISBN 9780061575594\nLCCN 2008012345")


@pytest.fixture
def online(app_env):
    """app_env with the ISBN resolvers wired to recognize `_ISBN`."""
    c, app, tmp = app_env
    app.config["ISBN_LOOKUP"] = lambda i: _META if i == _ISBN else None
    app.config["ISBN_WORK_KEY_LOOKUP"] = lambda i: "/works/OL12345W" if i == _ISBN else None
    return c, app, tmp


def test_add_by_isbn_resolves(online):
    c, _, _ = online
    r = c.post("/api/v1/wishlist", json={"isbn": "978-0-06-157559-4"})
    assert r.status_code == 201
    item = r.get_json()["item"]
    assert item["status"] == "resolved"
    assert item["title"] == "Being and Time"
    assert item["isbn"] == _ISBN
    assert item["source"] == "isbn"


def test_add_by_cip_resolves(online):
    c, _, _ = online
    item = c.post("/api/v1/wishlist", json={"cip_text": _CIP}).get_json()["item"]
    assert item["status"] == "resolved"
    assert item["lccn"] == "2008012345"
    assert item["source"] == "cip"


def test_add_by_title_single_candidate_resolves(online, monkeypatch):
    c, _, _ = online
    import catalogue.services.isbn as I
    monkeypatch.setattr(I, "search_by_title", lambda t, a=None: [
        {"title": "Zen Mind", "authors": ["Suzuki"], "publisher": "Weatherhill",
         "year": 1970, "isbn_13": None, "ol_work_key": "/works/OLz", "source": "openlibrary"}])
    item = c.post("/api/v1/wishlist", json={"title": "Zen Mind", "author": "Suzuki"}).get_json()["item"]
    assert item["status"] == "resolved"
    assert item["title"] == "Zen Mind"


def test_add_by_title_many_candidates_is_ambiguous_then_pick(online, monkeypatch):
    c, _, _ = online
    cands = [
        {"title": "A1", "authors": ["X"], "publisher": "P1", "year": 2001,
         "isbn_13": _ISBN, "ol_work_key": "/works/OLa", "source": "openlibrary"},
        {"title": "A2", "authors": ["X"], "publisher": "P2", "year": 2002,
         "isbn_13": None, "ol_work_key": "/works/OLb", "source": "openlibrary"}]
    import catalogue.services.isbn as I
    monkeypatch.setattr(I, "search_by_title", lambda t, a=None: cands)
    item = c.post("/api/v1/wishlist", json={"title": "Ambiguous"}).get_json()["item"]
    assert item["status"] == "ambiguous"
    assert len(item["candidates"]) == 2
    # Pick the first candidate → resolved from it.
    picked = c.patch(f"/api/v1/wishlist/{item['id']}", json={"pick": 0}).get_json()["item"]
    assert picked["status"] == "resolved"
    assert picked["title"] == "A1"
    assert picked["isbn"] == _ISBN


def test_add_requires_an_input(online):
    c, _, _ = online
    assert c.post("/api/v1/wishlist", json={}).status_code == 422


def test_dedupe_same_isbn_not_added_twice(online):
    c, _, _ = online
    a = c.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()
    assert a["added"] is True
    b = c.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()
    assert b["added"] is False and b["duplicate"] is True
    assert b["item"]["id"] == a["item"]["id"]              # the EXISTING item, not a copy
    assert len(c.get("/api/v1/wishlist").get_json()["items"]) == 1


def test_owned_book_is_not_added(online, seed):
    c, _, _ = online
    seed("INSERT INTO edition (title, isbn) VALUES ('Being and Time', ?)", (_ISBN,))
    j = c.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()
    assert j["added"] is False and j["owned"] is True and j["item"] is None
    assert c.get("/api/v1/wishlist").get_json()["items"] == []   # never wishlisted


def test_list_etag_304(online):
    c, _, _ = online
    c.post("/api/v1/wishlist", json={"isbn": _ISBN})
    r = c.get("/api/v1/wishlist")
    assert r.status_code == 200 and len(r.get_json()["items"]) == 1
    etag = r.headers["ETag"]
    assert c.get("/api/v1/wishlist", headers={"If-None-Match": etag}).status_code == 304


def test_patch_notes_and_priority(online):
    c, _, _ = online
    iid = c.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()["item"]["id"]
    item = c.patch(f"/api/v1/wishlist/{iid}", json={"notes": "gift", "priority": 1}).get_json()["item"]
    assert item["notes"] == "gift" and item["priority"] == 1


def test_stale_rev_is_conflict(online):
    c, _, _ = online
    item = c.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()["item"]
    # A rev that doesn't match the row's current rev is a lost-update conflict.
    r = c.patch(f"/api/v1/wishlist/{item['id']}", json={"notes": "x", "rev": item["rev"] + 99})
    assert r.status_code == 409
    # The matching rev succeeds.
    assert c.patch(f"/api/v1/wishlist/{item['id']}",
                   json={"notes": "x", "rev": item["rev"]}).status_code == 200


def test_delete_soft_removes(online):
    c, _, _ = online
    iid = c.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()["item"]["id"]
    assert c.delete(f"/api/v1/wishlist/{iid}").status_code == 200
    assert c.get("/api/v1/wishlist").get_json()["items"] == []


def test_scanner_intent_routes_to_wishlist(online):
    c, _, _ = online
    r = c.post("/capture", json={"isbn": _ISBN, "intent": "wishlist", "source": "ios"})
    assert r.status_code == 201
    j = r.get_json()
    assert j["intent"] == "wishlist" and j["wishlist_item"]["isbn"] == _ISBN
    # It landed in the wishlist, not capture_staging.
    assert len(c.get("/api/v1/wishlist").get_json()["items"]) == 1


def test_scanner_cip_intent_routes_to_wishlist(online):
    c, _, _ = online
    r = c.post("/capture/cip", json={"cip_text": _CIP, "intent": "wishlist"})
    assert r.status_code == 201 and r.get_json()["intent"] == "wishlist"
    assert len(c.get("/api/v1/wishlist").get_json()["items"]) == 1


def test_acquisition_loop_marks_item_acquired(online, seed):
    c, _, _ = online
    wid = c.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()["item"]["id"]
    # Catalogue the book, then a catalogue-intent capture should fulfil the wishlist item.
    seed("INSERT INTO edition (title, isbn) VALUES ('Being and Time', ?)", (_ISBN,))
    j = c.post("/capture", json={"isbn": _ISBN, "source": "ios"}).get_json()
    assert j["in_catalogue"] is True
    assert j["fulfilled_wishlist_item"] == wid
    item = [x for x in c.get("/api/v1/wishlist").get_json()["items"] if x["id"] == wid][0]
    assert item["status"] == "acquired" and item["matched_edition_id"] is not None
