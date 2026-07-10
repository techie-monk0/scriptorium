"""Tests for the title re-derivation pass (catalogue/work_titles.py).

The LLM ladder is faked (no network/Ollama): a fake Rung whose client returns a
canned JSON string, optionally chosen by the title-page text it's shown. Everything
else runs against a real in-memory DB built from schema.sql.
"""
from __future__ import annotations

import json

from catalogue.services import work_titles as WT
from catalogue.services.classify import Rung
from catalogue.db_store import add_alias, fold_key, init_db


# ── fake LLM ladder ──────────────────────────────────────────────────────────────
class _Client:
    def __init__(self, fn):
        self._fn = fn

    def chat(self, messages, *, max_tokens=512, json_only=True):
        return {"content": self._fn(messages)}


def _ladder(resp):
    """resp: a JSON string, or a fn(messages)->JSON string."""
    fn = resp if callable(resp) else (lambda _m: resp)
    return [Rung("fake", _Client(fn))]


def _map_ladder(mapping, default='{"title": "", "confidence": 0.0}'):
    """Pick the response whose key appears in the title-page text shown to the LLM."""
    def fn(messages):
        user = messages[-1]["content"]
        for needle, resp in mapping.items():
            if needle in user:
                return resp
        return default
    return _ladder(fn)


def _resp(title, *, conf, native=None, script=None, ev="seen on page"):
    d = {"title": title, "confidence": conf, "evidence": ev}
    if native:
        d["native_title"] = native
        d["native_script"] = script
    return json.dumps(d)


# ── DB seed helpers ────────────────────────────────────────────────────────────────
def _edition(db, title):
    return db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


def _holding_with_text(db, eid, file_hash, text):
    db.execute("INSERT INTO holding (edition_id, file_hash) VALUES (?, ?)",
               (eid, file_hash))
    db.execute("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
               "VALUES (?, 1, ?)", (file_hash, text))


def _work(db, title):
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, title, "english")
    return wid


def _link(db, eid, wid):
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) "
               "VALUES (?, ?, 0)", (eid, wid))


def _full_book(db, *, edition_title, work_title, text, file_hash="h1"):
    """An edition with one contained work and cached title-page text."""
    eid = _edition(db, edition_title)
    wid = _work(db, work_title)
    _link(db, eid, wid)
    _holding_with_text(db, eid, file_hash, text)
    db.commit()
    return eid, wid


def _edition_title(db, eid):
    return db.execute("SELECT title FROM edition WHERE id = ?", (eid,)).fetchone()[0]


def _work_aliases(db, wid):
    return db.execute("SELECT text, scheme FROM work_alias WHERE work_id = ? ORDER BY id",
                      (wid,)).fetchall()


def _queue(db):
    return db.execute("SELECT id, payload_json FROM review_queue "
                      "WHERE item_type = 'title_proposal'").fetchall()


# ── suggest_title (parsing) ────────────────────────────────────────────────────────
def test_suggest_title_parses_llm_json():
    s = WT.suggest_title("title page text here",
                         ladder=_ladder(_resp("Real Title", conf=0.9,
                                              native="ལམ་རིམ", script="tibetan")))
    assert s.title == "Real Title"
    assert s.confidence == 0.9
    assert s.native_title == "ལམ་རིམ" and s.native_script == "tibetan"


def test_suggest_title_none_on_no_text_or_no_title():
    assert WT.suggest_title("", ladder=_ladder(_resp("T", conf=0.9))) is None
    assert WT.suggest_title("page",
                            ladder=_ladder('{"title": "", "confidence": 0.1}')) is None


# ── high-confidence: replace now + queue, old kept as alias, native added ───────────
def test_high_confidence_applies_and_queues(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, wid = _full_book(
        db, edition_title="LTK - Great Exposition of Secret Mantra vol 1",
        work_title="Reasons for Faith",            # the known-wrong alias
        text="The Great Exposition of Secret Mantra VOLUME I Tantra in Tibet Tsongkhapa")
    new = "The Great Exposition of Secret Mantra, Volume I: Tantra in Tibet"
    status = WT.derive_title_for_edition(
        db, eid, ladder=_ladder(_resp(new, conf=0.95, native="གསང་སྔགས",
                                      script="tibetan")))
    assert status == "applied"
    # edition + work primary alias both replaced
    assert _edition_title(db, eid) == new
    aliases = _work_aliases(db, wid)
    assert aliases[0][0] == new and aliases[0][1] == "english"
    texts = {t for t, _ in aliases}
    schemes = {s for _, s in aliases}
    assert "Reasons for Faith" in texts          # old title preserved
    assert "filename" in schemes
    assert "གསང་སྔགས" in texts                    # native-script alias added
    # primary alias's normalized_key was refreshed
    nk = db.execute("SELECT normalized_key FROM work_alias WHERE work_id=? ORDER BY id LIMIT 1",
                    (wid,)).fetchone()[0]
    assert nk == fold_key(new)
    # queued, marked applied
    q = _queue(db)
    assert len(q) == 1
    p = json.loads(q[0][1])
    assert p["applied"] is True and p["new_title"] == new


# ── low-confidence: queue only, nothing replaced ────────────────────────────────────
def test_low_confidence_queues_without_replacing(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, wid = _full_book(db, edition_title="Murky Scan",
                          work_title="Murky Scan",
                          text="garbled o c r with no clear title line")
    status = WT.derive_title_for_edition(
        db, eid, ladder=_ladder(_resp("A Guess", conf=0.3)))
    assert status == "queued"
    assert _edition_title(db, eid) == "Murky Scan"          # untouched
    assert _work_aliases(db, wid) == [("Murky Scan", "english")]
    p = json.loads(_queue(db)[0][1])
    assert p["applied"] is False and p["new_title"] == "A Guess"


# ── accept a low-confidence (queue-only) item → applies it ──────────────────────────
def test_accept_applies_queue_only_item(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, wid = _full_book(db, edition_title="Murky", work_title="Murky",
                          text="some text")
    WT.derive_title_for_edition(db, eid, ladder=_ladder(_resp("Confirmed Title", conf=0.3)))
    iid = _queue(db)[0][0]
    assert WT.accept_title_proposal(db, iid) is True
    assert _edition_title(db, eid) == "Confirmed Title"
    assert _work_aliases(db, wid)[0][0] == "Confirmed Title"
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (iid,)).fetchone()[0] == "resolved"
    # accepting again is a no-op (not pending)
    assert WT.accept_title_proposal(db, iid) is False


# ── reject an auto-applied item → reverts ───────────────────────────────────────────
def test_reject_reverts_applied_title(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, wid = _full_book(db, edition_title="OLD - codes here",
                          work_title="Old Work Title",
                          text="The Real Title on the page")
    WT.derive_title_for_edition(db, eid, ladder=_ladder(_resp("The Real Title", conf=0.95)))
    assert _edition_title(db, eid) == "The Real Title"      # applied
    iid = _queue(db)[0][0]
    assert WT.reject_title_proposal(db, iid) is True
    # reverted on both edition and work
    assert _edition_title(db, eid) == "OLD - codes here"
    assert _work_aliases(db, wid) == [("Old Work Title", "english")]   # filename alias removed
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (iid,)).fetchone()[0] == "rejected"


def test_reject_queue_only_item_changes_nothing(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, wid = _full_book(db, edition_title="Keep Me", work_title="Keep Me",
                          text="ambiguous")
    WT.derive_title_for_edition(db, eid, ladder=_ladder(_resp("Rejected Guess", conf=0.2)))
    iid = _queue(db)[0][0]
    assert WT.reject_title_proposal(db, iid) is True
    assert _edition_title(db, eid) == "Keep Me"
    assert _work_aliases(db, wid) == [("Keep Me", "english")]


# ── echo / no-op / no-text / no-title / idempotence ─────────────────────────────────
def test_echo_of_current_title_is_unchanged(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _full_book(db, edition_title="Already Right", work_title="Already Right",
                        text="Already Right on the title page")
    status = WT.derive_title_for_edition(
        db, eid, ladder=_ladder(_resp("Already Right", conf=0.99)))
    assert status == "unchanged"
    assert _queue(db) == []


def test_clean_work_alias_still_fixes_dirty_edition_title(tmp_path):
    """The e2 regression: the work alias was already clean, but edition.title was
    still full filename junk. A match to the alias must NOT suppress cleaning the
    edition title — otherwise the displayed book title stays junk."""
    db = init_db(tmp_path / "t.db")
    eid, wid = _full_book(
        db,
        edition_title="Clean Title -- Some Author -- 2009 -- hash -- Anna’s Archive",
        work_title="Clean Title",                    # work alias already correct
        text="Clean Title front matter")
    status = WT.derive_title_for_edition(
        db, eid, ladder=_ladder(_resp("Clean Title", conf=0.95)))
    assert status == "applied"                        # was wrongly 'unchanged' before
    assert _edition_title(db, eid) == "Clean Title"   # the junk edition title is fixed
    # work alias unchanged (already equal) → no spurious 'filename' duplicate added
    assert _work_aliases(db, wid) == [("Clean Title", "english")]


# ── REQUIREMENT: the title LLM is fed the page ONLY, never the filename ─────────────
class _RecordingClient:
    """Captures every message the title pass sends to the LLM."""
    def __init__(self, resp):
        self._resp = resp
        self.seen = []

    def chat(self, messages, *, max_tokens=512, json_only=True):
        self.seen.append(messages)
        return {"content": self._resp}


def test_title_llm_never_receives_the_filename(tmp_path):
    db = init_db(tmp_path / "t.db")
    token = "ZZZ_UNIQUE_FILENAME_TOKEN_9999"
    eid, wid = _full_book(db, edition_title=token, work_title=token,
                          text="The Actual Page Title shown here")
    rec = _RecordingClient(_resp("The Actual Page Title", conf=0.95))
    WT.derive_title_for_edition(db, eid, ladder=[Rung("fake", rec)])
    # the filename/baseline title must appear in NOTHING sent to the model
    blob = json.dumps(rec.seen)
    assert token not in blob
    assert rec.seen, "LLM should have been called"


def test_mojibake_page_is_not_titled(tmp_path):
    """A corrupt custom-font text layer ('TILOPA' -> 'TIδτPƖ') must NOT be written
    as a title — the pass returns 'mojibake' and leaves the title alone."""
    db = init_db(tmp_path / "t.db")
    eid, wid = _full_book(db, edition_title="2018_Tilopa_A_Buddhist_Yogin",
                          work_title="2018_Tilopa_A_Buddhist_Yogin",
                          text="TIδτPƖ \nA BUDDHIST YOGIN OF THE TENTH CENTURY")
    # ladder would echo the garbled title, but we never reach it — source is mojibake
    status = WT.derive_title_for_edition(
        db, eid, ladder=_ladder(_resp("TIδτPƖ A Buddhist Yogin", conf=0.9)))
    assert status == "mojibake"
    assert _queue(db) == []
    assert _edition_title(db, eid) == "2018_Tilopa_A_Buddhist_Yogin"   # untouched


def test_looks_mojibake_helper():
    assert WT.looks_mojibake("TIδτPƖ A BUDDHIST YOGIN")
    assert not WT.looks_mojibake("Bodhicaryāvatāra: A Guide")        # IAST is fine
    assert not WT.looks_mojibake("ལམ་རིམ་ཆེན་མོ")                      # Tibetan is fine


def test_no_cached_text_is_no_text(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid = _edition(db, "No Text Book")           # no holding / raw_extract_cache
    db.commit()
    assert WT.derive_title_for_edition(db, eid, ladder=_ladder(_resp("X", conf=0.9))) == "no_text"


def test_no_title_from_model(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _full_book(db, edition_title="Book", work_title="Book", text="page text")
    assert WT.derive_title_for_edition(
        db, eid, ladder=_ladder('{"title": "", "confidence": 0.0}')) == "no_title"


def test_rerun_is_idempotent(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _full_book(db, edition_title="Codes 123", work_title="Codes 123",
                        text="The Clean Title here")
    WT.derive_title_for_edition(db, eid, ladder=_ladder(_resp("The Clean Title", conf=0.95)))
    second = WT.derive_title_for_edition(db, eid, ladder=_ladder(_resp("Other", conf=0.95)))
    assert second == "already"
    assert len(_queue(db)) == 1                  # not requeued


# ── work-less edition: only edition.title changes ───────────────────────────────────
def test_workless_edition_updates_edition_title_only(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid = _edition(db, "LZR - some book -- author -- hash")
    _holding_with_text(db, eid, "hZ", "The Actual Book Title front matter")
    db.commit()
    status = WT.derive_title_for_edition(
        db, eid, ladder=_ladder(_resp("The Actual Book Title", conf=0.9)))
    assert status == "applied"
    assert _edition_title(db, eid) == "The Actual Book Title"
    p = json.loads(_queue(db)[0][1])
    assert p["work_id"] is None
    # reject restores the edition title even with no work
    WT.reject_title_proposal(db, _queue(db)[0][0])
    assert _edition_title(db, eid) == "LZR - some book -- author -- hash"


# ── the walk over all editions ──────────────────────────────────────────────────────
def test_derive_all_titles_tally(tmp_path):
    db = init_db(tmp_path / "t.db")
    e1, _ = _full_book(db, edition_title="A-codes", work_title="A-codes",
                       text="Alpha Title page", file_hash="ha")
    e2, _ = _full_book(db, edition_title="B-codes", work_title="B-codes",
                       text="Beta Title page", file_hash="hb")
    e3 = _edition(db, "C no text"); db.commit()
    em, _ = _full_book(db, edition_title="Moji", work_title="Moji",
                       text="TIδτPƖ A BUDDHIST YOGIN", file_hash="hm")   # mojibake page
    ladder = _map_ladder({
        "Alpha Title": _resp("Alpha Title", conf=0.95),     # applied
        "Beta Title":  _resp("Beta Guess", conf=0.3),       # queued
    })
    tally = WT.derive_all_titles(db, ladder=ladder)
    assert tally["applied"] == 1
    assert tally["queued"] == 1
    assert tally["no_text"] == 1
    assert tally["mojibake"] == 1                            # the corrupt page, counted
    assert _edition_title(db, em) == "Moji"                  # and left untitled
