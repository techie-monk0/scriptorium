"""Tests for catalogue/services/classify_tradition.py — Phase 2 LLM adjudication.

The ladder is injected (a fake Rung whose transport returns canned JSON), so no
network / Ollama is needed — same pattern as tests/system/test_llm_ladder.py.
"""
from __future__ import annotations

import json

from catalogue.db_store import add_alias, init_db
from catalogue.db_store import migrate_tradition as R
from catalogue.services import classify_tradition as CT
from catalogue.services.classify import Rung
from catalogue.services.llm import LLMClient


def _fake_ladder(payload: dict):
    """A one-rung ladder whose LLM always returns `payload` as its JSON content."""
    def transport(url, body, timeout):
        return {"choices": [{"message": {"content": json.dumps(payload)}}], "usage": {}}
    return [Rung("fake", LLMClient(model="fake", base_url="http://x/v1", transport=transport))]


def _work(db, title, subject="Buddhism/Meditation"):
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, title, "english")
    eid = db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (eid, wid))
    sid = db.execute("INSERT OR IGNORE INTO subject (name) VALUES (?)", (subject,)) \
        and db.execute("SELECT id FROM subject WHERE name=?", (subject,)).fetchone()[0]
    db.execute("INSERT INTO edition_subject (edition_id, subject_id) VALUES (?,?)", (eid, sid))
    return wid


def _labels(db, wid):
    return {r[0]: (round(r[1], 3), r[2]) for r in db.execute(
        "SELECT t.name, wt.confidence, wt.source FROM work_tradition wt "
        "JOIN tradition t ON t.id=wt.tradition_id WHERE wt.work_id=?", (wid,)).fetchall()}


def _seed_default(db):
    """A work carrying only the presumed-Gelug rule row (the Phase-2 candidate shape)."""
    wid = _work(db, "A general Buddhist book")
    db.commit()
    R.migrate(db)                    # tags it Gelug @0.5 via rule-default
    assert _labels(db, wid) == {"Gelug": (0.5, "rule-default")}
    return wid


def test_candidate_selection(tmp_path):
    db = init_db(tmp_path / "t.db")
    wid = _seed_default(db)
    assert CT.candidates(db) == [wid]


def test_confident_verdict_replaces_rule_default(tmp_path):
    db = init_db(tmp_path / "t.db")
    wid = _seed_default(db)
    ladder = _fake_ladder({"traditions": ["Nyingma"], "scope": "school",
                           "confidence": 0.9, "evidence": "Dzogchen author"})
    res = CT.run(db, ladder=ladder)
    assert res["confident_written"] == 1 and res["rows_written"] == 1
    lab = _labels(db, wid)
    assert set(lab) == {"Nyingma"}
    conf, source = lab["Nyingma"]
    assert conf == 0.9 and source == "llm"


def test_multilabel_verdict(tmp_path):
    db = init_db(tmp_path / "t.db")
    wid = _seed_default(db)
    ladder = _fake_ladder({"traditions": ["Gelug", "Kagyu"], "scope": "school",
                           "confidence": 0.8, "evidence": "synthesis"})
    CT.run(db, ladder=ladder)
    assert set(_labels(db, wid)) == {"Gelug", "Kagyu"}


def test_low_confidence_left_for_review(tmp_path):
    db = init_db(tmp_path / "t.db")
    wid = _seed_default(db)
    ladder = _fake_ladder({"traditions": ["Sakya"], "scope": "school",
                           "confidence": 0.3, "evidence": "weak"})
    res = CT.run(db, ladder=ladder)
    assert res["confident_written"] == 0 and res["left_for_review"] == 1
    # rule-default row is preserved (not overwritten by a low-confidence guess)
    assert _labels(db, wid) == {"Gelug": (0.5, "rule-default")}


def test_unknown_tradition_is_dropped(tmp_path):
    db = init_db(tmp_path / "t.db")
    wid = _seed_default(db)
    ladder = _fake_ladder({"traditions": ["Bön", "Theravada"], "scope": "school",
                           "confidence": 0.9, "evidence": "off-vocab"})
    res = CT.run(db, ladder=ladder)
    # every tradition was off-vocab → no usable answer, nothing written
    assert res["no_answer"] == 1 and res["rows_written"] == 0
    assert _labels(db, wid) == {"Gelug": (0.5, "rule-default")}


def test_human_row_never_touched(tmp_path):
    db = init_db(tmp_path / "t.db")
    wid = _work(db, "Human-set work")
    kagyu = db.execute("SELECT id FROM tradition WHERE name='Kagyu'").fetchone()[0]
    db.execute("INSERT INTO work_tradition (work_id, tradition_id, confidence, source) "
               "VALUES (?,?,1.0,'human')", (wid, kagyu))
    db.commit()
    # a settled (human) work is not a candidate
    assert wid not in CT.candidates(db)


def test_cache_hit_skips_second_call(tmp_path):
    db = init_db(tmp_path / "t.db")
    wid = _seed_default(db)
    calls = {"n": 0}

    def transport(url, body, timeout):
        calls["n"] += 1
        return {"choices": [{"message": {"content": json.dumps(
            {"traditions": ["Kagyu"], "scope": "school", "confidence": 0.9, "evidence": "x"})}}],
            "usage": {}}
    ladder = [Rung("fake", LLMClient(model="fake", base_url="http://x/v1", transport=transport))]
    CT.classify_work(db, wid, ladder=ladder)
    CT.classify_work(db, wid, ladder=ladder)     # second call must hit the cache
    assert calls["n"] == 1
