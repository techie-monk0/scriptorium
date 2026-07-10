"""§14.9 capture contract v3 — copyright-page (CIP) intake.

Black-box over HTTP: the phone OCRs the copyright page on-device and POSTs the
recognized TEXT to /capture/cip. The server parses the CIP block and returns the
same cross-format verdict the barcode path uses. Resolvers are stubbed offline by
the `app_env` fixture; the CIP paths exercised here are purely local anyway.
"""
from __future__ import annotations

# A labelled LoC CIP block carrying a checksum-valid ISBN.
CIP_WITH_ISBN = (
    "Library of Congress Cataloging-in-Publication Data\n"
    "Title: Illuminating the intent / Tsongkhapa.\n"
    "Identifiers: LCCN 2020045678 | ISBN 9781614294412\n"
)

# A free-form CIP block with a title but NO ISBN (the no-barcode fallback case).
CIP_NO_ISBN = (
    "Library of Congress Cataloging-in-Publication Data\n"
    "Nagarjuna.\n"
    "The dispeller of disputes / Jan Westerhoff.\n"
)

CIP_UNKNOWN = (
    "Library of Congress Cataloging-in-Publication Data\n"
    "Smith, John.\n"
    "An utterly unknown treatise on nothing / John Smith.\n"
)

_KEYS = ("status", "staging_id", "isbn", "duplicate",
         "in_catalogue", "matched_by", "editions", "uncertain", "parsed")


def test_cip_response_shape(app_env):
    c, _, _ = app_env
    r = c.post("/capture/cip", json={"pages": [{"label": "copyright", "text": CIP_WITH_ISBN}]})
    assert r.status_code == 201
    body = r.get_json()
    for k in _KEYS:
        assert k in body, k
    # The `parsed` echo tells the phone what the CIP parser understood.
    assert body["parsed"]["title"] == "Illuminating the intent"
    assert "9781614294412" in body["parsed"]["isbns"]
    assert isinstance(body["staging_id"], int)


def test_cip_found_by_isbn(app_env, seed):
    c, _, _ = app_env
    seed("INSERT INTO edition (id, title, isbn) "
         "VALUES (30, 'Illuminating the Intent', '9781614294412')")
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (30, 'electronic', 'x.epub')")
    body = c.post("/capture/cip", json={"text": CIP_WITH_ISBN}).get_json()
    assert body["in_catalogue"] is True
    assert body["matched_by"] == "isbn"
    assert body["isbn"] == "9781614294412"
    assert body["editions"][0]["id"] == 30
    assert body["editions"][0]["forms"] == ["epub"]


def test_cip_no_isbn_title_match_is_uncertain(app_env, seed):
    # We hold a book with this title but no contributor data → a partial (title-only)
    # match must surface for the operator to judge, NEVER a silent not-found.
    c, _, _ = app_env
    seed("INSERT INTO edition (id, title) VALUES (40, 'The dispeller of disputes')")
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (40, 'electronic', 'd.pdf')")
    body = c.post("/capture/cip", json={"pages": [{"label": "title", "text": CIP_NO_ISBN}]}).get_json()
    assert body["in_catalogue"] is False
    assert body["matched_by"] == "title"
    assert any(u["id"] == 40 for u in body["uncertain"])


def test_cip_unknown_book_not_found_but_staged(app_env):
    c, app, _ = app_env
    body = c.post("/capture/cip", json={"text": CIP_UNKNOWN}).get_json()
    assert body["in_catalogue"] is False
    assert body["matched_by"] is None
    assert body["editions"] == [] and body["uncertain"] == []
    # A new no-ISBN book is still staged durably (raw text kept for desktop resolve).
    import sqlite3
    conn = sqlite3.connect(app.config["DB_PATH"])
    note = conn.execute("SELECT free_text_note FROM capture_staging WHERE id = ?",
                        (body["staging_id"],)).fetchone()[0]
    conn.close()
    assert "utterly unknown treatise" in note


def test_cip_staged_even_when_resolver_raises(app_env):
    c, app, _ = app_env
    def boom(_isbn):
        raise TimeoutError("openlibrary down")
    app.config["ISBN_WORK_KEY_LOOKUP"] = boom
    app.config["ISBN_LOOKUP"] = boom
    # CIP carries an ISBN we don't hold; the work-key/title layers raise but are
    # swallowed — the capture is never lost.
    r = c.post("/capture/cip", json={"text": CIP_WITH_ISBN})
    assert r.status_code == 201
    assert r.get_json()["in_catalogue"] is False


def test_cip_empty_and_malformed_are_rejected(app_env):
    c, _, _ = app_env
    assert c.post("/capture/cip", json={"pages": []}).status_code == 422
    assert c.post("/capture/cip", json={"text": "   "}).status_code == 422
    assert c.post("/capture/cip", json=[1, 2, 3]).status_code == 422
    # Non-JSON body.
    assert c.post("/capture/cip", data="not json",
                  content_type="text/plain").status_code == 422
