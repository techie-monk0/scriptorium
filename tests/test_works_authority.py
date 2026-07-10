"""Works authority search (84000/Toh by English/Sanskrit/Tibetan), the unified
work+authority picker, and clearing a chosen root/commentary work."""
import pytest

from catalogue.db_store import connect
from catalogue.services import work_detect as WD
from catalogue.services import work_canonical_resolver as R
from catalogue.webui.web import create_app


_FAKE = {"by_toh": {
    "3824": {"toh": "3824", "english": "The Root Stanzas on the Middle Way",
             "sanskrit": "Mūlamadhyamakakārikā", "tibetan": "dbu ma rtsa ba"},
    "3871": {"toh": "3871", "english": "Entering the Way of a Bodhisattva",
             "sanskrit": "Bodhicaryāvatāra", "tibetan": "byang chub sems dpa'i spyod pa la 'jug pa"},
    "9999": {"toh": "9999", "english": None,                      # Sanskrit-only (no English)
             "sanskrit": "Tārāmūlakalpa", "tibetan": None},
}}


def test_index_search_by_each_script():
    idx = R.EightyFourThousandIndex()
    idx._ensure_loaded = lambda: _FAKE                       # stub the on-disk index
    assert [m["toh"] for m in idx.search("root stanzas")] == ["3824"]      # English substring
    assert idx.search("Bodhicaryavatara")[0]["toh"] == "3871"             # Sanskrit, separator-insensitive
    assert idx.search("byang chub sems dpa")[0]["toh"] == "3871"          # Tibetan (Wylie)
    assert idx.search("3824")[0]["toh"] == "3824"                         # exact Toh#
    assert idx.search("nomatch") == []


def test_lang_prefix_scopes_search():
    assert R.parse_lang_prefix("skt: tārā") == ("sanskrit", "tārā")
    assert R.parse_lang_prefix("tib: dbu ma") == ("tibetan", "dbu ma")
    assert R.parse_lang_prefix("en: heart") == ("english", "heart")
    assert R.parse_lang_prefix("dbu ma") == (None, "dbu ma")              # no prefix
    idx = R.EightyFourThousandIndex(); idx._ensure_loaded = lambda: _FAKE
    # 'root stanzas' is an English title only → scoping to sanskrit finds nothing
    assert idx.search("root stanzas", lang="sanskrit") == []
    assert idx.search("Bodhicaryavatara", lang="sanskrit")[0]["toh"] == "3871"


class _FakeIdx:
    def __init__(self, *a, **k): pass
    def available(self): return True
    def by_toh(self, toh): return _FAKE["by_toh"].get(str(toh))
    def search(self, q, *, limit=20, lang=None):
        return [{"toh": "3824", "english": "Root Stanzas",
                 "sanskrit": "Mūlamadhyamakakārikā", "tibetan": "dbu ma rtsa ba"}]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "EightyFourThousandIndex", _FakeIdx)
    # shared_84000_index() is @lru_cache'd: an earlier test in the suite may have warmed
    # it with the REAL index, which would defeat the monkeypatch above (the cached
    # instance is returned without re-instantiating _FakeIdx). Clear it now so the
    # endpoints build the fake fresh, and again on teardown so our fake never leaks into
    # a later test that wants the real index.
    R.shared_84000_index.cache_clear()
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    yield app
    R.shared_84000_index.cache_clear()


def _edition(app):
    db = connect(app.config["DB_PATH"])
    eid = db.execute("INSERT INTO edition (title, structure) VALUES ('A', 'single_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/x.pdf')", (eid,))
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {"english": "A"}))
    db.commit()
    return eid


def test_authority_search_endpoint(app):
    with app.test_client() as c:
        j = c.get("/works/authority/search?q=root").get_json()
    assert j["matches"][0]["toh"] == "3824"


def test_live_bdrc_work_matches_parses(monkeypatch):
    from catalogue.services import bdrc
    # a fake ElasticSearch _msearch response (injected transport → no network)
    fake = {"responses": [{"hits": {"hits": [
        {"_id": "WA0RT1234", "_score": 9.1,
         "_source": {"prefLabel_bo_x_ewts": "dbu ma rtsa ba",
                     "altLabel_bo_x_ewts": ["dbu ma rtsa ba'i tshig le'ur byas pa"],
                     "authorName_bo_x_ewts": "klu sgrub"}}]}}]}
    out = bdrc.live_work_matches("dbu ma", transport=lambda body: fake)
    assert out[0]["system"] == "bdrc" and out[0]["number"] == "bdr:WA0RT1234"
    assert "dbu ma rtsa ba" in out[0]["title"]
    assert bdrc.live_work_matches("x", transport=lambda b: (_ for _ in ()).throw(OSError())) == []  # network fail → []


def test_bdrc_disambiguate_collapses_manifestations_to_work():
    """The real symptom: a name search returns many MW manifestations/parts of one text;
    disambiguate surfaces the abstract WORK (WA…) each belongs to, deduped."""
    from catalogue.services import bdrc
    matches = [
        {"system": "bdrc", "number": "bdr:MW1ALS19080E", "title": "grub mtha'",
         "titles": ["grub mtha'"], "authors": ["klu sgrub"],
         "associated_res": ["WA1KG25240", "T385", "P423"]},
        {"system": "bdrc", "number": "bdr:MW1ALS19027", "title": "grub mtha'",
         "titles": ["grub mtha'"], "authors": [], "associated_res": ["WA1KG25240", "T385"]},
        {"system": "bdrc", "number": "bdr:MW1NLM6369", "title": "other",        # no WA → kept as itself
         "titles": ["other"], "authors": [], "associated_res": ["PR1NLM00"]},
        {"system": "bdrc", "number": "bdr:MW1NLM1472_O1NLM1472_005",            # outline node → parent MW
         "title": "part", "titles": ["part"], "authors": [], "associated_res": []},
    ]
    out = bdrc.disambiguate(matches)
    assert [m["number"] for m in out] == ["bdr:WA1KG25240", "bdr:MW1NLM6369", "bdr:MW1NLM1472"]
    assert set(out[0]["members"]) == {"MW1ALS19080E", "MW1ALS19027"}
    assert out[0]["authors"] == ["klu sgrub"]                 # author carried from the scoring hit
    # stable: re-running keeps the same work numbers (the WA/MW keys don't drift)
    assert [m["number"] for m in bdrc.disambiguate(out)] == [m["number"] for m in out]


def test_live_bdrc_collapses_to_abstract_work():
    """End-to-end through the ES transport: two Instance hits sharing a WA → one work row."""
    from catalogue.services import bdrc
    fake = {"responses": [{"hits": {"hits": [
        {"_id": "MW1ALS19080E", "_score": 9.0, "_source": {
            "prefLabel_bo_x_ewts": "grub mtha'", "type": ["Instance"],
            "associated_res": ["WA1KG25240", "P423"], "authorName_bo_x_ewts": "klu sgrub"}},
        {"_id": "MW1ALS19027", "_score": 8.0, "_source": {
            "prefLabel_bo_x_ewts": "grub mtha'", "type": ["Instance"],
            "associated_res": ["WA1KG25240"]}},
    ]}}]}
    out = bdrc.live_work_matches("grub mtha'", transport=lambda b: fake)
    assert [m["number"] for m in out] == ["bdr:WA1KG25240"]
    assert set(out[0]["members"]) == {"MW1ALS19080E", "MW1ALS19027"}


def test_wikidata_disambiguate_collapses_editions_to_work(monkeypatch):
    from catalogue.services import wikidata as W

    def p629(qid):     # an entity that is an "edition or translation of" Q9
        return {"id": qid, "claims": {"P629": [
            {"mainsnak": {"datavalue": {"value": {"id": "Q9"}}}}]}}

    class C:
        ents = {"Q1": p629("Q1"), "Q2": p629("Q2"), "Q9": {"id": "Q9", "claims": {}}}
        def entity(self, qid): return self.ents.get(qid)

    monkeypatch.setattr(W, "labels_and_aliases", lambda e, **k: ("The Work", []))
    matches = [{"system": "wikidata", "number": "Q1", "title": "1822 print"},
               {"system": "wikidata", "number": "Q2", "title": "1905 print"}]
    out = W.disambiguate(matches, client=C())
    assert [m["number"] for m in out] == ["Q9"]              # both editions → the one work
    assert out[0]["title"] == "The Work"


def test_bdrc_work_meta_classifies_serial_and_author():
    from catalogue.services import bdrc
    work_ttl = "bdr:WA1NLM1136  a  bdo:Work ;\n  bdo:creator   bdr:CR2EB8 .\n"
    serial_ttl = "bdr:WAS2KG235404 a bdo:SerialWork , bdo:Work .\n"
    assert bdrc._parse_work_meta("WA1NLM1136", work_ttl) == {"kind": "work", "has_author": True}
    assert bdrc._parse_work_meta("WAS2KG235404", serial_ttl) == {"kind": "serial", "has_author": False}
    assert bdrc._parse_work_meta("WA1NLM2490", "bdr:WA1NLM2490 a bdo:Work .") == {"kind": "work", "has_author": False}
    bdrc._WORK_META_CACHE.clear()
    assert bdrc.work_meta("bdr:WA1NLM1136", fetch=lambda l: work_ttl)["has_author"] is True


def test_bdrc_select_authority_picks_authored_work():
    """The user's case: same text under WA1NLM1136 (Work+author), WA1NLM2490 (Work, no
    author), WAS2KG235404 (SerialWork container), and an authorless manifestation → ONE row,
    the author-bearing Work, the rest folded into same_as."""
    from catalogue.services import bdrc
    T = "grub mtha'i rnam par bzhag pa 'khrul spong"
    works = [
        {"system": "bdrc", "number": "bdr:WA1NLM2490", "title": T, "titles": [T], "authors": [], "author_ids": []},
        {"system": "bdrc", "number": "bdr:WA1NLM1136", "title": T, "titles": [T], "authors": [], "author_ids": []},
        {"system": "bdrc", "number": "bdr:WAS2KG235404", "title": T, "titles": [T], "authors": [], "author_ids": []},
        {"system": "bdrc", "number": "bdr:MW1NLM6369", "title": T, "titles": [T], "authors": [],
         "author_ids": [], "members": ["MW1NLM6369"]},
    ]
    meta = lambda n: {"bdr:WA1NLM1136": {"kind": "work", "has_author": True},
                      "bdr:WA1NLM2490": {"kind": "work", "has_author": False},
                      "bdr:WAS2KG235404": {"kind": "serial", "has_author": False},
                      }.get(n, {"kind": None, "has_author": False})
    out = bdrc.select_authority(works, meta=meta)
    assert len(out) == 1 and out[0]["number"] == "bdr:WA1NLM1136"          # author-bearing Work wins
    assert set(out[0]["same_as"]) == {"bdr:WA1NLM2490", "bdr:WAS2KG235404", "bdr:MW1NLM6369"}
    assert "author_ids" not in out[0]                                       # internal field stripped


def test_bdrc_select_authority_demotes_lone_serial_and_manifestation():
    """When two DIFFERENT texts come back, a SerialWork is flagged + sinks below a real
    Work, and a WA-less hit is flagged provisional — none are dropped."""
    from catalogue.services import bdrc
    works = [
        {"system": "bdrc", "number": "bdr:WAS9", "title": "A Catalogue Series", "titles": [], "authors": [], "author_ids": []},
        {"system": "bdrc", "number": "bdr:WA7", "title": "A Real Treatise", "titles": [], "authors": [], "author_ids": []},
        {"system": "bdrc", "number": "bdr:MW5", "title": "Only A Scan", "titles": [], "authors": [], "author_ids": [], "members": ["MW5"]},
    ]
    meta = lambda n: {"bdr:WA7": {"kind": "work", "has_author": True},
                      "bdr:WAS9": {"kind": "serial", "has_author": False},
                      }.get(n, {"kind": None, "has_author": False})
    out = bdrc.select_authority(works, meta=meta)
    assert [w["number"] for w in out] == ["bdr:WA7", "bdr:MW5", "bdr:WAS9"]  # work, then provisional, then serial
    assert next(w for w in out if w["number"] == "bdr:MW5")["provisional"] is True
    assert next(w for w in out if w["number"] == "bdr:WAS9")["serial"] is True


def test_toh_disambiguate_rejects_range_container():
    """A Toh RANGE/collection entry ('98-100') loses to the ATOMIC text ('98') it shares a
    title with — the Toh analogue of demoting a BDRC SerialWork."""
    from catalogue.services.work_canonical_resolver import EightyFourThousandIndex as IDX
    rows = [
        {"toh": "98-100", "english": "Collected Dhāraṇīs", "sanskrit": "Saṃgraha", "tibetan": "gzungs bsdus"},
        {"toh": "98", "english": "Collected Dhāraṇīs", "sanskrit": "Saṃgraha", "tibetan": "gzungs bsdus"},
    ]
    out = IDX.disambiguate(rows)
    assert [r["toh"] for r in out] == ["98"] and out[0]["variants"] == ["98-100"]


def test_toh_disambiguate_collapses_same_text():
    """Same sūtra catalogued under two Toh numbers (identical titles) → the lower Toh#."""
    from catalogue.services.work_canonical_resolver import EightyFourThousandIndex as IDX
    rows = [
        {"toh": "184", "english": "The Heart Sūtra", "sanskrit": "Prajñāpāramitāhṛdaya", "tibetan": "shes snying"},
        {"toh": "21",  "english": "The Heart Sūtra", "sanskrit": "Prajñāpāramitāhṛdaya", "tibetan": "shes snying"},
        {"toh": "99",  "english": "A Different Text", "sanskrit": "X", "tibetan": "y"},
    ]
    out = IDX.disambiguate(rows)
    assert [r["toh"] for r in out] == ["21", "99"]           # 184 folded into 21 (lower)
    assert out[0]["variants"] == ["184"]
    assert IDX.disambiguate(out) == out                      # idempotent


def test_wikidata_live_work_matches(monkeypatch):
    from catalogue.services import wikidata as W

    class FakeClient:
        def search(self, t, *, language="en"):
            return [("Q42", "The Heart Sūtra", "Buddhist text"), ("Q99", "Not a work", "person")]

        def entity(self, qid):
            return {"id": qid}

    monkeypatch.setattr(W, "is_work", lambda e, **k: e["id"] == "Q42")        # only Q42 is a work
    monkeypatch.setattr(W, "labels_and_aliases", lambda e, **k: ("The Heart Sūtra", []))
    out = W.live_work_matches("heart sutra", client=FakeClient())
    assert [m["number"] for m in out] == ["Q42"] and out[0]["system"] == "wikidata"

    class Boom:
        def search(self, *a, **k): raise OSError()
    assert W.live_work_matches("x", client=Boom()) == []                      # failure → []


def test_work_search_includes_live_bdrc_and_wikidata(app, monkeypatch):
    from catalogue.services import bdrc, wikidata
    monkeypatch.setattr(bdrc, "live_work_matches", lambda q, **k: [
        {"system": "bdrc", "number": "bdr:WA999", "title": "Live BDRC Hit", "titles": [], "authors": []}])
    monkeypatch.setattr(wikidata, "live_work_matches", lambda q, **k: [
        {"system": "wikidata", "number": "Q42", "title": "Live WD Hit", "desc": "text"}])
    with app.test_client() as c:
        j = c.get("/works/search?q=root&authority=1&live=1").get_json()
    syss = {m.get("system") for m in j["matches"] if m.get("authority")}
    assert "bdrc" in syss and "wikidata" in syss                              # both live sources wired


def test_work_search_saved_listed_before_authority(app):
    # Already-saved DB matches (works → editions) come FIRST so an existing record is the
    # obvious choice; the authority candidates (Toh/BDRC) follow. The picker prefixes the
    # saved rows "Saved Work/Edition:".
    from catalogue.db_store import connect, add_alias
    db = connect(app.config["DB_PATH"])
    w = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", w, "Root Stanzas (local)", "english")
    db.execute("INSERT INTO edition (title) VALUES ('Root book')")
    db.commit()
    with app.test_client() as c:
        j = c.get("/works/search?q=root&authority=1&editions=1").get_json()
    kinds = [("authority" if m.get("authority") else m["kind"]) for m in j["matches"]]
    assert kinds[0] == "work"                                     # saved work first
    assert kinds.index("work") < kinds.index("edition") < kinds.index("authority")


def test_picking_authority_creates_and_links_work(app):
    eid = _edition(app)
    with app.test_client() as c:
        # the picker submits the authority canonical#/titles (no work_id) → work is created
        c.post(f"/works/detect/{eid}/work/link", data={
            "canonical_system": "toh", "canonical_number": "3824",
            "english_title": "Root Stanzas", "sanskrit_title": "Mūlamadhyamakakārikā"})
    db = connect(app.config["DB_PATH"])
    w = db.execute("SELECT id FROM work WHERE canonical_number='3824'").fetchone()
    assert w and w[0] in [r[0] for r in db.execute(
        "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]


def test_work_search_lists_works_first_then_editions(app):
    from catalogue.db_store import add_alias
    db = connect(app.config["DB_PATH"])
    w = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", w, "Root Verses of the Middle Way", "english")
    db.execute("INSERT INTO edition (title) VALUES ('Root Verses — A Translation')")
    db.commit()
    with app.test_client() as c:
        j = c.get("/works/search?q=root verses&editions=1").get_json()
    kinds = [m["kind"] for m in j["matches"]]
    assert "work" in kinds and "edition" in kinds
    assert kinds.index("work") < kinds.index("edition")           # works listed first
    assert any(m["kind"] == "work" and "Root Verses of the Middle Way" in m["title"]
               for m in j["matches"])


def test_picking_edition_links_its_work(app):
    db = connect(app.config["DB_PATH"])
    src = db.execute("INSERT INTO edition (title) VALUES ('Source book')").lastrowid
    w = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (src, w))
    db.commit()
    eid = _edition(app)
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/work/link", data={"edition_id": src})   # pick an EDITION
    db = connect(app.config["DB_PATH"])
    assert w in [r[0] for r in db.execute(
        "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]       # its work linked


def test_typed_new_root_work_goes_into_works_table(app):
    """Adding a root/commentary text not in the catalogue creates a row in `work`
    (the typed-new-work path: english_title only, no work_id/edition_id)."""
    eid = _edition(app)
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/work/set-root",
               data={"english_title": "An Uncatalogued Root Text"})
    db = connect(app.config["DB_PATH"])
    w = db.execute("SELECT work_id FROM work_alias WHERE normalized_key LIKE '%uncatalogued%'").fetchone()
    assert w                                                       # the work row exists
    assert db.execute("SELECT work_type FROM work WHERE id=?", (w[0],)).fetchone()[0] == "root"
    assert w[0] in [r[0] for r in db.execute(
        "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]   # linked to the edition


def test_bdrc_instance_id_follows_to_its_work(monkeypatch):
    """REGRESSION: a BDRC scan INSTANCE (bdo:Instance, e.g. W…) has no title of its own;
    work_by_id follows `instanceOf` to the abstract work and resolves the title there."""
    from catalogue.services import bdrc
    ttls = {
        "W1": "bdr:W1  a  bdo:Instance ;\n   bdo:instanceOf  bdr:WA1 ;\n   bdo:instanceReproductionOf  bdr:MW1 .\n",
        "WA1": "@prefix skos: <#> .\n   skos:prefLabel  \"dbu ma rtsa ba\"@bo-x-ewts ;\n   .\n",
    }
    monkeypatch.setattr(bdrc, "_fetch_bdrc_ttl", lambda local, timeout: ttls.get(local))
    m = bdrc.work_by_id("bdr:W1", transport=lambda body: {"responses": [{"hits": {"hits": []}}]})
    assert m["number"] == "bdr:WA1" and m["tibetan"] == "dbu ma rtsa ba" and m["english"] is None


def test_bdrc_describe_unresolved_explains_type(monkeypatch):
    """A failed paste says WHAT it was (person / instance / missing), not just 'no resolve'."""
    from catalogue.services import bdrc
    bodies = {
        "P1": "bdr:P1  a  bdo:Person .\n",
        "W2": "bdr:W2  a  bdo:Instance , bdo:ImageInstance .\n",   # instance, no instanceOf
        "G3": "bdr:G3  a  bdo:Place .\n",
    }
    monkeypatch.setattr(bdrc, "_fetch_bdrc_ttl", lambda local, timeout: bodies.get(local))
    assert "PERSON" in bdrc.describe_unresolved("bdr:P1")
    assert "INSTANCE" in bdrc.describe_unresolved("bdr:W2")
    assert "PLACE" in bdrc.describe_unresolved("bdr:G3")
    assert "no BDRC record" in bdrc.describe_unresolved("bdr:NOPE")        # not found


def test_resolve_endpoint_returns_reason(app, monkeypatch):
    from catalogue.services import bdrc
    monkeypatch.setattr(bdrc, "work_by_id", lambda wid, **k: None)
    monkeypatch.setattr(bdrc, "describe_unresolved", lambda wid, **k: "bdr:P9 is a BDRC PERSON record, not a work")
    with app.test_client() as c:
        j = c.get("/works/authority/resolve?id=bdr:P9").get_json()
    assert "PERSON" in j["error"]


def test_bdrc_ttl_title_parse():
    from catalogue.services import bdrc
    ttl = ('@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n'
           '   skos:prefLabel  "rgyud bla ma\'i \'grel bshad"@bo-x-ewts , "Ratnagotra"@sa-x-iast ;\n'
           '   skos:altLabel  "alt title"@bo-x-ewts ;\n')
    t = bdrc.parse_bdrc_ttl_titles(ttl)
    assert "rgyud bla ma'i 'grel bshad" in t["bo-x-ewts"]
    assert "Ratnagotra" in t["sa-x-iast"]
    assert "alt title" in t["all"]


def test_resolve_pasted_id_toh_and_bdrc(app, monkeypatch):
    from catalogue.services import bdrc
    monkeypatch.setattr(bdrc, "work_by_id", lambda wid, **k: {
        "system": "bdrc", "number": "bdr:WA20455", "title": "dbu ma rtsa ba shes rab",
        "titles": ["dbu ma rtsa ba shes rab"]} if "20455" in wid else None)
    with app.test_client() as c:
        toh = c.get("/works/authority/resolve?id=Toh 3824").get_json()
        assert toh["system"] == "toh" and toh["number"] == "3824" and "Stanzas" in toh["english"]
        bd = c.get("/works/authority/resolve?id=bdr:WA20455").get_json()
        assert bd["system"] == "bdrc" and bd["number"] == "bdr:WA20455"
        assert "error" in c.get("/works/authority/resolve?id=bdr:WNOPE").get_json()


def test_pasted_bdrc_id_creates_work(app, monkeypatch):
    """The id-paste flow posts the resolved canonical#/title → a work in the works table."""
    eid = _edition(app)
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/work/set-root", data={
            "canonical_system": "bdrc", "canonical_number": "bdr:WA20455",
            "english_title": "dbu ma rtsa ba shes rab"})
    db = connect(app.config["DB_PATH"])
    w = db.execute("SELECT id FROM work WHERE canonical_system='bdrc' AND canonical_number='bdr:WA20455'").fetchone()
    assert w and w[0] in [r[0] for r in db.execute(
        "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]


def test_authority_titles_map_to_their_script_not_english(app, monkeypatch):
    """REGRESSION: a non-English authority title (BDRC Wylie, Sanskrit-only Toh) must go
    to its own script field, NEVER into english_title."""
    from catalogue.services import bdrc
    monkeypatch.setattr(bdrc, "work_by_id", lambda wid, **k: {
        "system": "bdrc", "number": "bdr:WA1", "title": "dbu ma rtsa ba",
        "titles": ["dbu ma rtsa ba"], "english": None, "tibetan": "dbu ma rtsa ba",
        "sanskrit": None})
    with app.test_client() as c:
        bd = c.get("/works/authority/resolve?id=bdr:WA1").get_json()
        assert bd["english"] is None and bd["tibetan"] == "dbu ma rtsa ba"   # Wylie → tibetan
        toh = c.get("/works/authority/resolve?id=Toh 9999").get_json()
        assert toh["english"] is None and toh["sanskrit"] == "Tārāmūlakalpa"  # Skt → sanskrit


def test_native_only_authority_creates_correct_alias_schemes(app):
    """REGRESSION: creating a work from a Tibetan/Sanskrit-only authority stores the
    title under its scheme (wylie/iast), adds NO english alias, and lands in review."""
    from catalogue.services import work_review as WR
    eid = _edition(app)
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/work/link", data={
            "canonical_system": "bdrc", "canonical_number": "bdr:WA1",
            "tibetan_title": "dbu ma rtsa ba"})           # no english_title
    db = connect(app.config["DB_PATH"])
    w = db.execute("SELECT id FROM work WHERE canonical_number='bdr:WA1'").fetchone()[0]
    schemes = {s for (s,) in db.execute("SELECT scheme FROM work_alias WHERE work_id=?", (w,))}
    assert "wylie" in schemes and "english" not in schemes
    assert db.execute("SELECT tibetan_title FROM work WHERE id=?", (w,)).fetchone()[0] == "dbu ma rtsa ba"
    assert any(x["id"] == w for x in WR.incomplete_works(db))   # flagged for an English title


def test_create_work_native_only_alias_invariant(tmp_path):
    """REGRESSION (data model): create_work files native titles under iast/wylie and
    never invents an english alias."""
    from catalogue.db_store import init_db
    from catalogue.services import work_identity as WI
    db = init_db(tmp_path / "c.db")
    w1, _, _ = WI.create_work(db, tibetan_title="dbu ma rtsa ba",
                              canonical_system="bdrc", canonical_number="bdr:WA1")
    a1 = {sc: t for t, sc in db.execute("SELECT text, scheme FROM work_alias WHERE work_id=?", (w1,))}
    assert a1.get("wylie") == "dbu ma rtsa ba" and "english" not in a1
    w2, _, _ = WI.create_work(db, sanskrit_title="Tārāmūlakalpa",
                              canonical_system="toh", canonical_number="9999")
    a2 = {sc: t for t, sc in db.execute("SELECT text, scheme FROM work_alias WHERE work_id=?", (w2,))}
    assert a2.get("iast") == "Tārāmūlakalpa" and "english" not in a2


def test_clear_commentary_and_root(app):
    eid = _edition(app)
    with app.test_client() as c:
        # The work↔work (Layer 1) set/clear routes are kept though the old degenerate-work
        # commentary UI was replaced by the edition-level Layer-2 "Commentary on" row.
        c.post(f"/works/detect/{eid}/work/set-commentary", data={"english_title": "A Guide"})
        c.post(f"/works/detect/{eid}/work/set-root", data={"english_title": "A Root"})
        c.post(f"/works/detect/{eid}/work/clear-commentary")
    db = connect(app.config["DB_PATH"])
    # commentary work unlinked + the commentary_on relationship gone
    types = [r[0] for r in db.execute(
        "SELECT w.work_type FROM edition_work ew JOIN work w ON w.id=ew.work_id WHERE ew.edition_id=?", (eid,))]
    assert "commentary" not in types
    assert db.execute("SELECT COUNT(*) FROM relationship WHERE relation='commentary_on'").fetchone()[0] == 0
