"""Part B: single-work detection (dry-run). Hermetic — the classical resolver is
injected, so no network/Toh-snapshot is touched; we test the wiring + the
classical-vs-modern determination + the cache."""
import pytest

from catalogue.db_store import init_db
from catalogue.services import work_detect as WD


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "c.db")
    yield conn
    conn.close()


def _single_edition(db, title, author):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (author,)).lastrowid
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (wid, pid))
    eid = db.execute("INSERT INTO edition (title, structure) VALUES (?, 'single_work')",
                     (title,)).lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)",
               (eid, wid))
    db.execute("INSERT INTO holding (edition_id, form, file_path) "
               "VALUES (?, 'electronic', '/lib/x.pdf')", (eid,))
    return eid


def test_recorded_contributors(db):
    eid = _single_edition(db, "The Way of the Bodhisattva", "Śāntideva")
    # add a book-level translator + edition_author
    tp = db.execute("INSERT INTO person (primary_name) VALUES ('Padmakara')").lastrowid
    db.execute("INSERT INTO edition_translator (edition_id, person_id, seq) VALUES (?, ?, 1)",
               (eid, tp))
    authors, translators = WD.recorded_contributors(db, eid)
    assert [a["name"] for a in authors] == ["Śāntideva"]
    assert [t["name"] for t in translators] == ["Padmakara"]


def test_classical_match_links_work(db):
    eid = _single_edition(db, "Fundamental Wisdom of the Middle Way", "Nāgārjuna")

    def fake(ctx):
        return {"english": ctx["title"], "authority_en": "The Root Stanzas on the Middle Way",
                "sanskrit": "Mūlamadhyamakakārikā", "tibetan": "dbu ma rtsa ba",
                "system": "toh", "number": "3824", "confidence": 0.9, "source": "84000-by-english"}

    res = WD.detect_single(db, eid, classical=fake)
    assert res["determination"] == "classical"
    c = res["canonical"]
    assert c["system"] == "toh" and c["number"] == "3824"
    assert c["title_en"] == "The Root Stanzas on the Middle Way"      # authority English to verify against
    assert c["url_84000"] == "https://read.84000.co/translation/toh3824.html"
    assert c["url_bdrc"]                                              # a BDRC link is provided too
    assert res["title"]["english"] == "Fundamental Wisdom of the Middle Way"   # the BOOK's title
    assert res["title"]["sanskrit"] == "Mūlamadhyamakakārikā"
    assert [a["name"] for a in res["authors_recorded"]] == ["Nāgārjuna"]


def test_native_title_only_is_classical(db):
    eid = _single_edition(db, "Bodhicaryāvatāra study", "Someone")

    def fake(ctx):
        return {"english": ctx["title"], "sanskrit": "Bodhicaryāvatāra", "tibetan": None,
                "system": None, "number": None, "confidence": 0.4, "source": None}

    res = WD.detect_single(db, eid, classical=fake)
    assert res["determination"] == "classical"          # native title present, no canonical#
    assert res["canonical"]["number"] is None


def test_no_signal_is_modern_edition(db):
    eid = _single_edition(db, "Insight Into Emptiness", "Jampa Tegchok")

    def fake(ctx):
        return {"english": ctx["title"], "sanskrit": None, "tibetan": None,
                "system": None, "number": None, "confidence": 0.0, "source": None}

    res = WD.detect_single(db, eid, classical=fake)
    assert res["determination"] == "modern"             # → author on the edition, no work
    assert [a["name"] for a in res["authors_recorded"]] == ["Jampa Tegchok"]


def test_detected_contributors_side_by_side(db):
    import json
    eid = _single_edition(db, "Some Book", "Saved Author")
    hid = db.execute("SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]
    # the ingest proposal read a DIFFERENT author off the title page
    db.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('book_toc_pattern', ?)",
               (json.dumps({"holding_id": hid, "book_authors": ["Title-Page Author"],
                            "book_translators": ["A Translator"]}),))
    idx = WD.build_proposal_index(db)
    res = WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]},
                           proposal_index=idx)
    assert [a["name"] for a in res["authors_recorded"]] == ["Saved Author"]
    assert res["authors_detected"] == ["Title-Page Author"]    # drift is visible
    assert res["translators_detected"] == ["A Translator"]


def test_glosses_both_models_when_no_authority_english(db):
    eid = _single_edition(db, "Ocean of Reasoning", "Tsongkhapa")
    glossers = {"gemma3:12b": lambda t, l: f"gemma:{t}",
                "claude": lambda t, l: f"claude:{t}"}

    # BDRC Tibetan match, no authority English → BOTH models gloss the Tibetan
    bdrc = lambda ctx: {"english": ctx["title"], "authority_en": None,
                        "tibetan": "rigs pa'i rgya mtsho", "sanskrit": None,
                        "system": "bdrc", "number": "bdr:MW1", "confidence": 0.8}
    res = WD.detect_single(db, eid, classical=bdrc, glossers=glossers)
    assert res["canonical"]["title_en"] is None
    assert res["canonical"]["glosses"] == {"gemma3:12b": "gemma:rigs pa'i rgya mtsho",
                                           "claude": "claude:rigs pa'i rgya mtsho"}

    # Authority English present → no glosses (don't waste the calls)
    toh = lambda ctx: {"english": ctx["title"], "authority_en": "Ocean of Reasoning",
                       "tibetan": "rigs pa'i rgya mtsho", "system": "toh", "number": "1"}
    res = WD.detect_single(db, eid, classical=toh, glossers=glossers)
    assert res["canonical"]["glosses"] is None


def test_cached_gloss_calls_llm_once_per_title_model(db):
    calls = []

    class FakeLLM:
        model = "gemma3:12b"
        def chat(self, messages, *, max_tokens=512, json_only=True):
            calls.append(1)
            return {"content": "Ocean of Reasoning"}

    c = FakeLLM()
    g1 = WD.cached_gloss(db, "rigs pa'i rgya mtsho", lang="Tibetan Wylie",
                         model_label="gemma3:12b", client=c)
    g2 = WD.cached_gloss(db, "rigs pa'i rgya mtsho", lang="Tibetan Wylie",   # same title → cache hit
                         model_label="gemma3:12b", client=c)
    assert g1 == g2 == "Ocean of Reasoning"
    assert len(calls) == 1                                   # LLM called only once
    # a different model re-glosses (separate cache entry)
    WD.cached_gloss(db, "rigs pa'i rgya mtsho", lang="Tibetan Wylie",
                    model_label="claude", client=c)
    assert len(calls) == 2
    assert db.execute("SELECT COUNT(*) FROM gloss_cache").fetchone()[0] == 2


def test_external_llm_config_and_google_auth():
    from catalogue.services.llm import external_llm_config, _auth_headers
    cfg = external_llm_config()                       # vocab.json _external_llm
    assert cfg["provider"] in ("claude", "gemini")
    assert cfg["model"] and cfg["base_url"]
    # Gemini's OpenAI-compat endpoint authenticates with the Google key (env)
    assert _auth_headers("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                         env={"GEMINI_API_KEY": "k"}) == {"Authorization": "Bearer k"}
    assert _auth_headers("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                         env={"GOOGLE_API_KEY": "g"}) == {"Authorization": "Bearer g"}
    # Claude still works; unknown host gets nothing
    assert _auth_headers("https://api.anthropic.com/v1/chat/completions",
                         env={"ANTHROPIC_API_KEY": "a"}) == {"Authorization": "Bearer a"}
    assert _auth_headers("https://example.com/v1", env={}) == {}


def test_gloss_title_strips_quotes_and_tolerates_no_client():
    class FakeLLM:
        model = "gemma3:12b"
        def chat(self, messages, *, max_tokens=512, json_only=True):
            return {"content": '  "Ocean of Reasoning"  '}
    assert WD.gloss_title("rgya mtsho", lang="Tibetan Wylie", client=FakeLLM()) == "Ocean of Reasoning"
    assert WD.gloss_title("x", lang="t", client=None) is None


def test_store_and_get(db):
    eid = _single_edition(db, "X", "Y")
    res = WD.detect_single(db, eid, classical=lambda ctx: {"english": "X"})
    WD.store_detection(db, eid, "single", res)
    got = WD.get_detection(db, eid)
    assert got["kind"] == "single" and got["determination"] == "modern"
    # upsert replaces, not duplicates
    WD.store_detection(db, eid, "single", res)
    assert db.execute("SELECT COUNT(*) FROM work_detection WHERE edition_id=?", (eid,)).fetchone()[0] == 1


def test_live_classical_degrades_without_index_or_network(db):
    # No Toh snapshot, no BDRC search → returns title-only signals, never raises.
    eid = _single_edition(db, "Some English Title", "Author")
    res = WD.detect_single(db, eid, classical=WD.live_classical(bdrc_work_search=None))
    assert res["determination"] in ("modern", "classical")
    assert res["title"]["english"] == "Some English Title"
