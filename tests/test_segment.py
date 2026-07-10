"""Part C: multi-work segmentation (dry-run). Hermetic — LLM clients are faked
(no Ollama/Haiku), sections injected (no file I/O). Tests the grouping-pass
parsing + the three-method orchestration."""
import json

import pytest

from catalogue.db_store import init_db
from catalogue.services.locator import Section
from catalogue.services import segment as SEG


class FakeClient:
    def __init__(self, payload, model="fake"):
        self._payload, self.model = payload, model

    def chat(self, messages, *, max_tokens=512, json_only=True):
        return {"content": json.dumps(self._payload), "model": self.model, "tokens_out": 7}


class BadClient:
    model = "bad"

    def chat(self, *a, **k):
        return {"content": "not json at all", "model": "bad"}


def test_llm_segment_parses_works_and_authors():
    c = FakeClient({"works": [{"title": "Root Stanzas", "author": "Nāgārjuna"},
                              {"title": "Commentary", "author": None},
                              {"title": "", "author": "x"}]})        # blank title dropped
    r = SEG.llm_segment(["Root Stanzas", "Commentary", "Index"], book_title="MMK", client=c)
    assert r["ok"]
    assert [w["title"] for w in r["works"]] == ["Root Stanzas", "Commentary"]
    assert r["works"][0]["authors"] == ["Nāgārjuna"]
    assert r["works"][1]["authors"] == []


def test_llm_segment_tolerates_bad_json():
    r = SEG.llm_segment(["A"], client=BadClient())
    assert not r["ok"] and r["works"] == []


def test_llm_segment_no_client_or_titles():
    assert SEG.llm_segment(["A"], client=None)["works"] == []
    assert SEG.llm_segment([], client=FakeClient({"works": [{"title": "x"}]}))["works"] == []


def test_clean_titles_drops_frontback_and_apparatus():
    secs = [Section(title="Preface", text=""),
            Section(title="Mūlamadhyamakakārikā", text=""),
            Section(title="Glossary", text=""),
            Section(title="Bodhicaryāvatāra", text="")]
    assert SEG.clean_titles(secs) == ["Mūlamadhyamakakārikā", "Bodhicaryāvatāra"]


def test_segment_edition_runs_three_methods(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = db.execute(
        "INSERT INTO edition (title, structure) VALUES ('Anthology', 'multi_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) "
               "VALUES (?, 'electronic', '/x.pdf')", (eid,))
    secs = [Section(title="Song by Saraha", text=""), Section(title="Song by Tilopa", text="")]
    gemma = FakeClient({"works": [{"title": "Song", "author": "Saraha"}]}, model="gemma3:12b")
    haiku = FakeClient({"works": [{"title": "Song of Saraha", "author": "Saraha"},
                                  {"title": "Song of Tilopa", "author": "Tilopa"}]}, model="haiku")

    def classical(ctx):                                  # no canonical for these
        return {"system": None, "number": None, "sanskrit": None, "tibetan": None}

    res = SEG.segment_edition(db, eid, sections=secs,
                              clients={"gemma": gemma, "haiku": haiku}, classical=classical)
    assert set(res["methods"]) == {"deterministic", "gemma", "haiku"}
    assert res["n_sections"] == 2
    assert [w["title"] for w in res["methods"]["haiku"]["works"]] == ["Song of Saraha", "Song of Tilopa"]
    assert res["methods"]["gemma"]["model"] == "gemma3:12b"
    assert "canonical" in res["methods"]["haiku"]["works"][0]   # per-work canonical annotated


def test_segment_detect_cli_stores_multi(tmp_path):
    from catalogue.cli import segment_detect
    db = init_db(tmp_path / "c.db")
    eid = db.execute(
        "INSERT INTO edition (title, structure) VALUES ('A', 'multi_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) "
               "VALUES (?, 'electronic', '/nope.pdf')", (eid,))
    db.commit()
    n, used = segment_detect.run(db, clients={}, offline=True)   # no LLM, no network
    assert n == 1
    assert db.execute("SELECT kind FROM work_detection WHERE edition_id=?",
                      (eid,)).fetchone()[0] == "multi"


def test_multi_report_renders_methods(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")   # gated feature on
    from catalogue.webui.web import create_app
    from catalogue.db_store import connect
    from catalogue.services import work_detect as WD
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    eid = db.execute(
        "INSERT INTO edition (title, structure) VALUES ('Anthology', 'multi_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/x.pdf')",
               (eid,))
    WD.store_detection(db, eid, "multi", {
        "stored_title": "Anthology", "n_sections": 3, "n_titles": 2,
        "file": {"holding_id": 1, "path": "/x.pdf"},
        "methods": {
            "deterministic": {"works": [{"title": "Det Work", "authors": []}]},
            "haiku": {"works": [{"title": "Haiku Work", "authors": ["Saraha"]}],
                      "model": "haiku", "ok": True}}})
    with app.test_client() as c:
        page = c.get("/works/detect/multi").data
    assert b"Det Work" in page and b"Haiku Work" in page
    assert b"deterministic" in page and b"haiku" in page


def test_multi_detection_gated_when_disabled(tmp_path, monkeypatch):
    """With multi_work_detection OFF, the multi review route redirects to single and the
    apply-multi / per-segment routes 404. (The flag now ships ON; this forces it off to
    pin the gate itself.)"""
    monkeypatch.setattr("catalogue.services.features.feature_enabled",
                        lambda name, default=False: False)
    from catalogue.webui.web import create_app
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    with app.test_client() as c:
        r = c.get("/works/detect/multi")
        assert r.status_code == 302 and "/works/detect/single" in r.headers["Location"]
        assert c.post("/works/detect/1/apply-multi", data={"method": "claude"}).status_code == 404
        assert c.post("/works/detect/1/segment/link?method=claude&idx=0",
                      data={"from_detection": "1"}).status_code == 404


def test_segment_detect_cli_gated_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr("catalogue.services.features.feature_enabled",
                        lambda name, default=False: False)
    from catalogue.cli import segment_detect
    assert segment_detect.main([str(tmp_path / "c.db")]) == 2          # refused, flag off


def test_segment_edition_canonical_annotation(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = db.execute("INSERT INTO edition (title) VALUES ('Book')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/x.pdf')",
               (eid,))
    haiku = FakeClient({"works": [{"title": "Mulamadhyamakakarika", "author": "Nagarjuna"}]})

    def classical(ctx):
        return {"system": "toh", "number": "3824", "authority_en": "Root Stanzas",
                "sanskrit": "Mūlamadhyamakakārikā", "tibetan": "dbu ma rtsa ba"}

    res = SEG.segment_edition(db, eid, sections=[Section(title="MMK", text="")],
                              clients={"haiku": haiku}, classical=classical)
    w = res["methods"]["haiku"]["works"][0]
    assert w["canonical"]["number"] == "3824"
    assert w["canonical"]["url_84000"] == "https://read.84000.co/translation/toh3824.html"
    assert w["canonical"]["title_en"] == "Root Stanzas"
    assert w["title_sanskrit"] == "Mūlamadhyamakakārikā"
