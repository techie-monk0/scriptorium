"""System tests — identifier-first edition metadata resolution.

Black-box (tests/system/conftest.py convention): ARRANGE via the `seed` SQL fixture,
ACT through the top-level entry point `edition_resolve.resolve_all_editions` with
INJECTED fake authority sources (+ fake LLM ladder) so the real pass runs offline,
ASSERT through the HTTP UI (/work/<id>, /review, accept/reject).
"""
from __future__ import annotations

import json
import sqlite3

from catalogue.services import edition_resolve as ER
from catalogue.services.classify import Rung
from catalogue.services.edition_verify import EditionRecord


class _FakeSource:
    name = "fakeauth"

    def __init__(self, by_isbn=None):
        self._isbn = by_isbn or {}

    def by_isbn(self, isbn):
        return list(self._isbn.get(isbn, []))

    def by_lccn(self, lccn):
        return []


class _LLMClient:
    def __init__(self, resp):
        self._resp = resp

    def chat(self, messages, *, max_tokens=512, json_only=True):
        return {"content": self._resp}


_NO_PAGE = [Rung("fake", _LLMClient('{"title": "", "confidence": 0.0}'))]


def _db(app):
    return sqlite3.connect(app.config["DB_PATH"])


def _seed_book(seed, *, edition_title, work_title, text, file_hash):
    eid = seed("INSERT INTO edition (title) VALUES (?)", (edition_title,)).lastrowid
    wid = seed("INSERT INTO work DEFAULT VALUES").lastrowid
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (?, ?, 'english', ?)", (wid, work_title, work_title.lower()))
    seed("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 0)",
         (eid, wid))
    seed("INSERT INTO holding (edition_id, file_hash) VALUES (?, ?)", (eid, file_hash))
    seed("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
         "VALUES (?, 1, ?)", (file_hash, text))
    return eid, wid


def test_isbn_record_applied_and_visible(app_env, seed):
    c, app, _ = app_env
    eid, wid = _seed_book(
        seed, edition_title="JUNK -- Author -- 9781614298939 -- Anna’s Archive",
        work_title="Wrong Alias",
        text="copyright page ISBN 978-1-61429-893-9", file_hash="hi1")
    src = _FakeSource(by_isbn={"9781614298939": [EditionRecord(
        source="fakeauth", title="How to Meditate on the Stages of the Path",
        authors=("Kathleen McDonald",), publisher="Wisdom Publications", year=2024,
        isbn="9781614298939")]})

    tally = ER.resolve_all_editions(_db(app), sources=[src], ladder=_NO_PAGE, only="identifier")
    assert tally["id_applied"] == 1

    # the authoritative title is visible on the work page…
    assert b"How to Meditate on the Stages of the Path" in c.get(f"/work/{wid}").data
    # …and the authoritative author shows in the review item (NOT linked as a
    # contributor — role-aware passes own that).
    conn = _db(app)
    iid = conn.execute("SELECT id FROM review_queue WHERE item_type='edition_metadata'").fetchone()[0]
    conn.close()
    assert b"Kathleen McDonald" in c.get(f"/review-queue/{iid}").data


def test_reject_reverts_via_http(app_env, seed):
    c, app, _ = app_env
    eid, wid = _seed_book(seed, edition_title="OLD -- 9781614298939",
                          work_title="Old Work",
                          text="ISBN 9781614298939", file_hash="hi2")
    src = _FakeSource(by_isbn={"9781614298939": [EditionRecord(
        source="fakeauth", title="New Authoritative Title", authors=("A Author",),
        isbn="9781614298939")]})
    ER.resolve_all_editions(_db(app), sources=[src], ladder=_NO_PAGE, only="identifier")

    conn = _db(app)
    assert conn.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] == "New Authoritative Title"
    iid = conn.execute("SELECT id FROM review_queue WHERE item_type='edition_metadata'").fetchone()[0]
    conn.close()

    r = c.post(f"/review-queue/{iid}/authority/reject")
    assert r.status_code in (200, 302)
    conn = _db(app)
    assert conn.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] == "OLD -- 9781614298939"
    assert conn.execute("SELECT COUNT(*) FROM work_author WHERE work_id=?", (wid,)).fetchone()[0] == 0
    conn.close()


def test_no_isbn_falls_back_to_llm(app_env, seed):
    c, app, _ = app_env
    eid, wid = _seed_book(seed, edition_title="B junk filename", work_title="B junk",
                          text="The Real Title On The Page", file_hash="hi3")
    ladder = [Rung("fake", _LLMClient('{"title": "The Real Title", "confidence": 0.95}'))]
    tally = ER.resolve_all_editions(_db(app), ladder=ladder, only="llm")
    assert tally["llm_applied"] == 1
    assert b"The Real Title" in c.get(f"/work/{wid}").data


def test_mojibake_page_not_titled_end_to_end(app_env, seed):
    """A born-digital book whose text layer is corrupt ('TILOPA' -> 'TIδτPƖ') must
    NOT get a garbled title — even if the (fake) LLM would echo one. The book is
    counted 'mojibake', left at baseline, and the garbage never reaches the UI."""
    c, app, _ = app_env
    eid, wid = _seed_book(seed, edition_title="2018_Tilopa_A_Buddhist_Yogin",
                          work_title="2018_Tilopa_A_Buddhist_Yogin",
                          text="TIδτPƖ \nA BUDDHIST YOGIN OF THE TENTH CENTURY",
                          file_hash="hmoji")
    ladder = [Rung("fake", _LLMClient('{"title": "TIδτPƖ A Buddhist Yogin", "confidence": 0.95}'))]
    tally = ER.resolve_all_editions(_db(app), ladder=ladder, only="llm")
    assert tally["mojibake"] == 1 and tally["llm_applied"] == 0

    conn = _db(app)
    assert conn.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] \
        == "2018_Tilopa_A_Buddhist_Yogin"            # untouched, not garbled
    conn.close()
    assert "TIδτPƖ".encode() not in c.get(f"/work/{wid}").data   # garbage never in the UI
