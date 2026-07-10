"""System tests — title re-derivation from cached title-page text.

Black-box per the convention (tests/system/conftest.py): ARRANGE via the `seed` SQL
fixture, ACT through the top-level entry point `work_titles.derive_all_titles` (the
function the CLI's main() calls) with an INJECTED fake LLM ladder so the real code
path runs offline, then ASSERT through the HTTP UI (/work/<id>, /review, accept/reject).
"""
from __future__ import annotations

import json
import sqlite3

from catalogue.services import work_titles
from catalogue.services.classify import Rung


# ── fake LLM ladder: title-page text → canned JSON ──────────────────────────────────
class _Client:
    def __init__(self, mapping, default='{"title": "", "confidence": 0.0}'):
        self._mapping = mapping
        self._default = default

    def chat(self, messages, *, max_tokens=512, json_only=True):
        user = messages[-1]["content"]
        for needle, resp in self._mapping.items():
            if needle in user:
                return {"content": resp}
        return {"content": self._default}


def _ladder(mapping):
    return [Rung("fake", _Client(mapping))]


def _resp(title, *, conf, native=None, script=None):
    d = {"title": title, "confidence": conf, "evidence": "on the page"}
    if native:
        d.update(native_title=native, native_script=script)
    return json.dumps(d)


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


# ── THE headline test: a filename-junk title becomes the real one, end to end ───────
def test_high_confidence_retitle_visible_in_ui(app_env, seed):
    c, app, _ = app_env
    # The e16 case: filename codes + a flat-wrong work alias; the page has the title.
    eid, wid = _seed_book(
        seed,
        edition_title="LTK - Great Exposition of Secret Mantra vol 1",
        work_title="Reasons for Faith",
        text="The Great Exposition of Secret Mantra VOLUME I Tantra in Tibet",
        file_hash="hh1")
    new = "The Great Exposition of Secret Mantra, Volume I: Tantra in Tibet"
    ladder = _ladder({"Great Exposition": _resp(new, conf=0.95,
                                                native="གསང་སྔགས", script="tibetan")})

    tally = work_titles.derive_all_titles(_db(app), ladder=ladder)
    assert tally["applied"] == 1

    # ASSERT through HTTP: the work page shows the real title, not the filename junk.
    page = c.get(f"/work/{wid}").data
    assert new.encode() in page
    assert b"Reasons for Faith" not in page.split(b"</h1>")[0]   # not the heading


# ── low confidence → queued, NOT applied; visible in the review queue ───────────────
def test_low_confidence_is_queued_not_applied(app_env, seed):
    c, app, _ = app_env
    eid, wid = _seed_book(seed, edition_title="Murky", work_title="Murky",
                          text="garbled scan, no clear title", file_hash="hh2")
    ladder = _ladder({"garbled scan": _resp("A Tentative Title", conf=0.3)})
    tally = work_titles.derive_all_titles(_db(app), ladder=ladder)
    assert tally["queued"] == 1

    # stored title untouched…
    conn = _db(app)
    assert conn.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] == "Murky"
    iid = conn.execute("SELECT id FROM review_queue WHERE item_type='title_proposal'").fetchone()[0]
    conn.close()
    # …but it shows up in the review queue for confirmation
    assert b"title_proposal" in c.get("/review-queue?type=title_proposal").data
    # accept via HTTP applies it
    r = c.post(f"/review-queue/{iid}/authority/accept")
    assert r.status_code in (200, 302)
    assert b"A Tentative Title" in c.get(f"/work/{wid}").data


# ── reject an auto-applied retitle → reverts, end to end ────────────────────────────
def test_reject_reverts_via_http(app_env, seed):
    c, app, _ = app_env
    eid, wid = _seed_book(seed, edition_title="OLD codes", work_title="Old Work Title",
                          text="The Correct Title here", file_hash="hh3")
    ladder = _ladder({"Correct Title": _resp("The Correct Title", conf=0.95)})
    work_titles.derive_all_titles(_db(app), ladder=ladder)

    conn = _db(app)
    assert conn.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] == "The Correct Title"
    iid = conn.execute("SELECT id FROM review_queue WHERE item_type='title_proposal'").fetchone()[0]
    conn.close()

    r = c.post(f"/review-queue/{iid}/authority/reject")
    assert r.status_code in (200, 302)
    conn = _db(app)
    assert conn.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] == "OLD codes"
    # the filename alias the apply added was removed on revert
    aliases = [r[0] for r in conn.execute(
        "SELECT text FROM work_alias WHERE work_id=? ORDER BY id", (wid,))]
    conn.close()
    assert aliases == ["Old Work Title"]
