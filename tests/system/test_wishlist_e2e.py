"""End-to-end wishlist journeys — the frontends (web/PWA/iOS) and the scanner against a live server.

These mirror what each surface actually sends/consumes:
  • Frontend (web/PWA/iOS): POST add → GET the ETagged list → the payload is exactly the
    `{items:[…]}` shape the SHARED `LibraryCore.wishlistVM` parses → PATCH pick / DELETE.
  • Scanner: the §14.10 `intent:"wishlist"` branch of POST /capture and POST /capture/cip.
  • Acquisition loop: catalogue the book, then a normal catalogue-intent scan tells the scanner the
    wishlist item was `fulfilled_wishlist_item`.
All run offline (resolver fetchers stubbed). The HTTP surface IS the contract every client shares.
"""
from __future__ import annotations

import pytest

_ISBN = "9780061575594"
_META = {"title": "Being and Time", "authors": ["Martin Heidegger"],
         "publishers": ["Harper"], "publish_date": "1962"}
_CIP = ("## title page\nBeing and Time\n\n## copyright page\n"
        "Library of Congress Cataloging-in-Publication Data\n"
        "Title: Being and time / Martin Heidegger.\nISBN 9780061575594\nLCCN 2008012345")

# The keys the shared LibraryCore.wishlistVM (web/PWA) + Swift wishlistVM read off each item.
_VM_KEYS = {"id", "source", "status", "title", "authors", "isbn", "cover_url",
            "candidates", "matched_edition_id"}


@pytest.fixture
def client(app_env):
    c, app, _ = app_env
    app.config["ISBN_LOOKUP"] = lambda i: _META if i == _ISBN else None
    app.config["ISBN_WORK_KEY_LOOKUP"] = lambda i: "/works/OL12345W" if i == _ISBN else None
    return c


@pytest.fixture
def reconcile(app_env):
    """Run the WRITE-side wishlist reconcile against the app DB — the same call sweep/ingest and
    edition-delete make. Simulates "a book appeared/left in the catalogue" so the e2e can assert the
    read-only GET reflects it (reconciliation no longer happens on read)."""
    from catalogue.db_store import connect_rw
    from catalogue.services import wishlist_reconcile
    _, app, _ = app_env

    def _run():
        conn = connect_rw(app.config["DB_PATH"])
        try:
            return wishlist_reconcile.reconcile_acquisitions(conn)
        finally:
            conn.close()
    return _run


# ── Frontend (web / PWA / iOS) journey ────────────────────────────────────────────
def test_frontend_add_list_payload_is_wishlistvm_ready(client):
    # A frontend adds by ISBN, then GETs the list it will feed to wishlistVM.
    assert client.post("/api/v1/wishlist", json={"isbn": _ISBN}).status_code == 201
    r = client.get("/api/v1/wishlist", headers={"Accept": "application/json"})
    assert r.status_code == 200 and "ETag" in r.headers
    payload = r.get_json()
    assert "items" in payload and len(payload["items"]) == 1
    item = payload["items"][0]
    assert _VM_KEYS <= set(item), f"payload missing wishlistVM keys: {_VM_KEYS - set(item)}"
    assert item["status"] == "resolved" and item["title"] == "Being and Time"


def test_frontend_offline_cache_304(client):
    client.post("/api/v1/wishlist", json={"isbn": _ISBN})
    etag = client.get("/api/v1/wishlist").headers["ETag"]
    # A client re-validating with its cached ETag gets 304 → serves its cached copy offline.
    assert client.get("/api/v1/wishlist", headers={"If-None-Match": etag}).status_code == 304


def test_frontend_ambiguous_then_pick(client, monkeypatch):
    import catalogue.services.isbn as I
    monkeypatch.setattr(I, "search_by_title", lambda t, a=None: [
        {"title": "A1", "authors": ["X"], "isbn_13": _ISBN, "ol_work_key": "/works/a"},
        {"title": "A2", "authors": ["X"], "isbn_13": None, "ol_work_key": "/works/b"}])
    item = client.post("/api/v1/wishlist", json={"title": "Ambiguous"}).get_json()["item"]
    assert item["status"] == "ambiguous" and len(item["candidates"]) == 2
    picked = client.patch(f"/api/v1/wishlist/{item['id']}", json={"pick": 0}).get_json()["item"]
    assert picked["status"] == "resolved" and picked["title"] == "A1"
    client.delete(f"/api/v1/wishlist/{item['id']}")
    assert client.get("/api/v1/wishlist").get_json()["items"] == []


# ── Suspected match — ask the operator (similar title+author / different ISBN) ─────
_EBOOK = "9781614294412"   # a different (checksum-valid) ISBN from the catalogue's hardcover


def _seed_partial_match(seed):
    """Catalogue holds the HARDCOVER (different ISBN), authored by ONE of the two names the ebook
    lists → a partial (uncertain) match, the 'is this the same book?' case."""
    pid = seed("INSERT INTO person (primary_name) VALUES ('Martin Heidegger')").lastrowid
    eid = seed("INSERT INTO edition (title, isbn) VALUES ('Being and Time', '9780000000019')").lastrowid
    seed("INSERT INTO edition_author (edition_id, person_id) VALUES (?, ?)", (eid, pid))
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'physical', NULL)", (eid,))
    return eid


def _ebook_app(app_env):
    c, app, _ = app_env
    app.config["ISBN_LOOKUP"] = lambda i: (
        {"title": "Being and Time", "authors": ["Martin Heidegger", "Joan Stambaugh"],
         "publishers": ["Harper"]} if i == _EBOOK else None)
    app.config["ISBN_WORK_KEY_LOOKUP"] = lambda i: None
    return c


def test_suspected_match_is_flagged_then_confirmed(app_env, seed):
    c = _ebook_app(app_env)
    eid = _seed_partial_match(seed)
    j = c.post("/api/v1/wishlist", json={"isbn": _EBOOK}).get_json()
    item = j["item"]
    # Added (not silently merged) but flagged 'suspected' with the candidate to confirm.
    assert j["added"] is True and item["status"] == "suspected"
    assert any(cand["id"] == eid for cand in item["candidates"])
    # The operator confirms it's the same book they own → acquired, off the wanted list.
    confirmed = c.patch(f"/api/v1/wishlist/{item['id']}", json={"confirm_owned": eid}).get_json()["item"]
    assert confirmed["status"] == "acquired" and confirmed["matched_edition_id"] == eid


def test_suspected_match_declined_stays_wanted(app_env, seed):
    c = _ebook_app(app_env)
    _seed_partial_match(seed)
    item = c.post("/api/v1/wishlist", json={"isbn": _EBOOK}).get_json()["item"]
    assert item["status"] == "suspected"
    # "No, different book" → it stays on the active wishlist, suspicion cleared.
    declined = c.patch(f"/api/v1/wishlist/{item['id']}",
                       json={"decline_suspected": True}).get_json()["item"]
    assert declined["status"] == "resolved" and declined["candidates"] == []


# ── Scanner journey (§14.10 intent) ───────────────────────────────────────────────
def test_scanner_isbn_intent_lands_in_wishlist(client):
    r = client.post("/capture", json={"isbn": _ISBN, "intent": "wishlist", "source": "ios"})
    assert r.status_code == 201
    j = r.get_json()
    assert j["intent"] == "wishlist" and j["wishlist_item"]["isbn"] == _ISBN
    # It went to the wishlist, NOT capture_staging — and the frontend list now shows it.
    assert len(client.get("/api/v1/wishlist").get_json()["items"]) == 1


def test_scanner_cip_intent_lands_in_wishlist(client):
    r = client.post("/capture/cip", json={"cip_text": _CIP, "intent": "wishlist", "source": "ios"})
    assert r.status_code == 201 and r.get_json()["intent"] == "wishlist"
    items = client.get("/api/v1/wishlist").get_json()["items"]
    assert len(items) == 1 and items[0]["lccn"] == "2008012345"


def test_scanner_default_intent_unchanged_goes_to_capture(client):
    # No intent → the normal catalogue capture path (regression guard for v3 clients).
    j = client.post("/capture", json={"isbn": _ISBN, "source": "ios"}).get_json()
    assert "wishlist_item" not in j and j["status"] == "ok" and "staging_id" in j
    assert client.get("/api/v1/wishlist").get_json()["items"] == []


# ── Acquisition loop (scanner is told) ────────────────────────────────────────────
def test_get_is_read_only_no_reconcile(client, seed):
    # The book appears in the catalogue but NOTHING write-side ran — a GET must NOT change state.
    wid = client.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()["item"]["id"]
    eid = seed("INSERT INTO edition (title, isbn) VALUES ('Being and Time', ?)", (_ISBN,)).lastrowid
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', 'x.pdf')", (eid,))
    item = [x for x in client.get("/api/v1/wishlist").get_json()["items"] if x["id"] == wid][0]
    assert item["status"] == "resolved"        # read-only: still wanted until a write-side reconcile


def test_ingest_reconcile_flips_item(client, seed, reconcile):
    # Wishlisted while NOT in the catalogue, then the book appears via the filesystem (a new
    # edition+holding). The sweep's write-side reconcile (here `reconcile()`) flips it to 'acquired'.
    wid = client.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()["item"]["id"]
    eid = seed("INSERT INTO edition (title, isbn) VALUES ('Being and Time', ?)", (_ISBN,)).lastrowid
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', 'x.pdf')", (eid,))
    reconcile()
    item = [x for x in client.get("/api/v1/wishlist").get_json()["items"] if x["id"] == wid][0]
    assert item["status"] == "acquired" and item["matched_edition_id"] == eid


def test_ingest_reconcile_matches_across_isbn_by_title(client, seed, reconcile):
    # The catalogue holds the HARDCOVER (a DIFFERENT ISBN); the wishlist item carries the EBOOK ISBN.
    # No work-key link, but same title + FULL author → reconcile flips it (cross-format by title).
    wid = client.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()["item"]["id"]
    pid = seed("INSERT INTO person (primary_name) VALUES ('Martin Heidegger')").lastrowid
    eid = seed("INSERT INTO edition (title, isbn) VALUES ('Being and Time', '9780000000019')").lastrowid
    seed("INSERT INTO edition_author (edition_id, person_id) VALUES (?, ?)", (eid, pid))
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'physical', NULL)", (eid,))
    reconcile()
    item = [x for x in client.get("/api/v1/wishlist").get_json()["items"] if x["id"] == wid][0]
    assert item["status"] == "acquired"


def test_deleting_edition_reverts_acquired_item_no_orphan(client, seed, reconcile):
    # Wishlist → ingest fulfils it (acquired) → the edition is deleted. The delete-side reconcile
    # must NOT leave it acquired pointing at a dead edition — it reverts to the active wishlist.
    wid = client.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()["item"]["id"]
    eid = seed("INSERT INTO edition (title, isbn) VALUES ('Being and Time', ?)", (_ISBN,)).lastrowid
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', 'x.pdf')", (eid,))
    reconcile()
    acquired = [x for x in client.get("/api/v1/wishlist").get_json()["items"] if x["id"] == wid][0]
    assert acquired["status"] == "acquired" and acquired["matched_edition_id"] == eid
    seed("UPDATE edition SET deleted_at = datetime('now') WHERE id = ?", (eid,))   # edition deleted
    reconcile()
    reverted = [x for x in client.get("/api/v1/wishlist").get_json()["items"] if x["id"] == wid][0]
    assert reverted["status"] == "resolved" and reverted["matched_edition_id"] is None


def test_acquisition_loop_tells_scanner(client, seed):
    wid = client.post("/api/v1/wishlist", json={"isbn": _ISBN}).get_json()["item"]["id"]
    seed("INSERT INTO edition (title, isbn) VALUES ('Being and Time', ?)", (_ISBN,))
    # A normal catalogue-intent scan of the now-catalogued book tells the scanner it was fulfilled.
    j = client.post("/capture", json={"isbn": _ISBN, "source": "ios"}).get_json()
    assert j["fulfilled_wishlist_item"] == wid
    item = [x for x in client.get("/api/v1/wishlist").get_json()["items"] if x["id"] == wid][0]
    assert item["status"] == "acquired"
