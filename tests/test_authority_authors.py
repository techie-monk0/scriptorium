"""Each authority source resolves the AUTHORS of a work it identifies (authors_for),
and adding a work from an authority match auto-populates those authors on the work."""
from catalogue.db_store import connect
from catalogue.services import work_authority as WA
from catalogue.webui.web import create_app


# ── Per-module authors_for ───────────────────────────────────────────────────
def test_84000_authors_for_reads_tei_author(tmp_path):
    tei = tmp_path / "t.xml"
    tei.write_text(
        '<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt>'
        '<title>Root Stanzas</title><author>Nāgārjuna</author>'
        '</titleStmt></fileDesc></teiHeader></TEI>', "utf-8")

    class StubIndex:
        snapshot_dir = tmp_path
        def available(self): return True
        def by_toh(self, toh): return {"file": "t.xml", "toh": toh} if toh == "3824" else None
        def by_english(self, t): return None

    src = WA.EightyFourThousandSource(index=StubIndex())
    assert src.authors_for("3824") == [{"name": "Nāgārjuna"}]
    assert src.authors_for("9999") == []                       # unknown toh → none


def test_84000_authors_for_none_when_snapshot_absent(tmp_path):
    class Unavail:
        def available(self): return False
        def by_toh(self, toh): return {"file": "x"}
        def by_english(self, t): return None
    assert WA.EightyFourThousandSource(index=Unavail()).authors_for("3824") == []


def test_bdrc_authors_for_picks_matching_work(monkeypatch):
    monkeypatch.setattr("catalogue.services.bdrc.live_work_matches", lambda title, **k: [
        {"number": "bdr:WA0", "authors": ["someone else"]},
        {"number": "bdr:WA1", "authors": ["klu sgrub", "rgyal tshab"]},
    ])
    src = WA.BdrcWorkSource(client=object())                   # client unused by authors_for
    assert src.authors_for("bdr:WA1", title="dbu ma rtsa ba") == [
        {"name": "klu sgrub"}, {"name": "rgyal tshab"}]
    assert src.authors_for("bdr:NOPE", title="x") == []        # no matching work → none


def test_wikidata_authors_for_resolves_p50_with_cross_ids():
    work = {"claims": {"P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q42"}}}}]}}
    author = {"labels": {"en": {"value": "Nāgārjuna"}},
              "claims": {"P2477": [{"mainsnak": {"datavalue": {"value": "P1583"}}}]}}  # P2477 = BDRC id

    class FakeClient:
        def __init__(self, ents): self.ents = ents
        def entity(self, qid): return self.ents.get(qid)

    src = WA.WikidataWorkSource(client=FakeClient({"Q1": work, "Q42": author}))
    out = src.authors_for("Q1")
    assert len(out) == 1
    assert out[0]["name"] == "Nāgārjuna"
    assert out[0]["external_id"] == "wikidata:Q42"
    assert out[0]["extra_ids"]["bdrc"] == "bdr:P1583"          # cross-link harvested
    # offline → never touches the client
    assert WA.WikidataWorkSource(client=FakeClient({}), offline=True).authors_for("Q1") == []


def test_authors_for_dispatch_maps_system_to_source():
    class StubSource:
        def authors_for(self, number, *, title=None, language=None):
            return [{"name": f"author-of-{number}"}]
    out = WA.authors_for("wikidata", "Q7", title="x", sources={"wikidata": StubSource()})
    assert out == [{"name": "author-of-Q7"}]
    assert WA.authors_for("toh", "1", sources={"84000": StubSource()}) == [{"name": "author-of-1"}]
    assert WA.authors_for("nonsense", "x") == []               # unknown system → none


# ── Wiring: add-work from an authority auto-populates the author ──────────────
def _edition(app, title="Host Edition"):
    db = connect(app.config["DB_PATH"])
    eid = db.execute("INSERT INTO edition (title, structure) VALUES (?, 'single_work')",
                     (title,)).lastrowid
    db.commit()
    return eid


def _add_authority_work(app, lookup, *, eid=None):
    app.config["INGEST_VERIFY"] = True                          # enable the authority fetch
    app.config["WORK_AUTHORS_LOOKUP"] = lookup
    eid = eid or _edition(app)
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/add-work", data={
            "canonical_system": "toh", "canonical_number": "3824",
            "english_title": "Root Stanzas"})
    db = connect(app.config["DB_PATH"])
    return db.execute("SELECT id FROM work WHERE canonical_number='3824'").fetchone()[0]


def _work_authors(db, w):
    return [n for (n,) in db.execute(
        "SELECT p.primary_name FROM work_author wa JOIN person p ON p.id = wa.person_id "
        "WHERE wa.work_id = ?", (w,))]


def test_authority_author_links_by_external_id_despite_spelling(tmp_path):
    """(1) external-id match: the authority's author id already sits on a catalogue person
    stored under a DIFFERENT spelling → that person is linked, no duplicate minted."""
    app = create_app(tmp_path / "c.db", ingest_verify=False); app.testing = True
    db = connect(app.config["DB_PATH"])
    from catalogue.db_store import add_alias
    pid = db.execute("INSERT INTO person (primary_name, external_id, verification_status) "
                     "VALUES ('Nāgārjuna', 'wikidata:Q42', 'verified')").lastrowid
    add_alias(db, "person", pid, "Nāgārjuna", "english")
    n_before = db.execute("SELECT COUNT(*) FROM person").fetchone()[0]
    db.commit()
    w = _add_authority_work(app, lambda s, n, title=None: [
        {"name": "Klu sgrub", "external_id": "wikidata:Q42",          # different spelling
         "extra_ids": {"wikidata": "wikidata:Q42"}}])
    db = connect(app.config["DB_PATH"])
    assert _work_authors(db, w) == ["Nāgārjuna"]                       # the existing person
    assert db.execute("SELECT COUNT(*) FROM person").fetchone()[0] == n_before   # none created


def test_authority_author_name_match_binds_id_and_alias(tmp_path):
    """(2) name fold-key match to an UNBOUND person → link, then bind the authority id +
    cross-links + the authority spelling as an alias (spelling-proof next time)."""
    app = create_app(tmp_path / "c.db", ingest_verify=False); app.testing = True
    db = connect(app.config["DB_PATH"])
    from catalogue.db_store import add_alias
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('Nāgārjuna', 'provisional')").lastrowid
    add_alias(db, "person", pid, "Nāgārjuna", "english")             # fold-key 'nagarjuna'
    db.commit()
    w = _add_authority_work(app, lambda s, n, title=None: [
        {"name": "Nagarjuna", "external_id": "wikidata:Q42",          # diacritic-only variant
         "extra_ids": {"wikidata": "wikidata:Q42", "bdrc": "bdr:P1583"}}])
    db = connect(app.config["DB_PATH"])
    assert _work_authors(db, w) == ["Nāgārjuna"]                       # linked the existing person
    p = db.execute("SELECT external_id, verification_status FROM person WHERE id=?", (pid,)).fetchone()
    assert p == ("wikidata:Q42", "verified")                          # bound on confident match
    assert db.execute("SELECT 1 FROM person_external_id WHERE person_id=? AND value='bdr:P1583'",
                      (pid,)).fetchone()                              # cross-link stored
    keys = {k for (k,) in db.execute(
        "SELECT normalized_key FROM person_alias WHERE person_id=?", (pid,))}
    assert "nagarjuna" in keys                                        # authority spelling now an alias


def test_authority_author_unmatched_is_skipped_and_noted(tmp_path):
    """(3) no external-id and no alias match → DO NOT create a person; leave a note on the
    work so the operator resolves it by hand (the user's 'must resolve' requirement)."""
    app = create_app(tmp_path / "c.db", ingest_verify=False); app.testing = True
    db = connect(app.config["DB_PATH"])
    n_before = db.execute("SELECT COUNT(*) FROM person").fetchone()[0]
    w = _add_authority_work(app, lambda s, n, title=None: [{"name": "Sumpa Khenpo"}])
    db = connect(app.config["DB_PATH"])
    assert _work_authors(db, w) == []                                 # nothing linked
    assert db.execute("SELECT COUNT(*) FROM person").fetchone()[0] == n_before   # none created
    notes = db.execute("SELECT notes FROM work WHERE id=?", (w,)).fetchone()[0] or ""
    assert "Sumpa Khenpo" in notes and "resolve by hand" in notes     # flagged for the operator


def test_add_work_authority_skips_author_fetch_when_hermetic(tmp_path):
    """Default (INGEST_VERIFY off, as in the whole suite) never calls the resolver — the
    work is created with the canonical id but no network author fetch happens."""
    app = create_app(tmp_path / "c.db", ingest_verify=False)
    app.testing = True
    called = []
    app.config["WORK_AUTHORS_LOOKUP"] = lambda *a, **k: called.append(a) or [{"name": "X"}]
    eid = _edition(app)
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/add-work", data={
            "canonical_system": "toh", "canonical_number": "3824", "english_title": "Root"})
    db = connect(app.config["DB_PATH"])
    w = db.execute("SELECT id FROM work WHERE canonical_number='3824'").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM work_author WHERE work_id=?", (w,)).fetchone()[0] == 0
    assert called == []                                         # resolver never invoked


def test_add_work_authority_respects_hand_picked_authors(tmp_path):
    """If the operator hand-picked a person, the authority author fetch is skipped (their
    choice wins, no surprise extra authors)."""
    app = create_app(tmp_path / "c.db", ingest_verify=False)
    app.testing = True
    app.config["INGEST_VERIFY"] = True
    app.config["WORK_AUTHORS_LOOKUP"] = lambda *a, **k: [{"name": "Authority Author"}]
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Chosen Person')").lastrowid
    db.commit()
    eid = _edition(app)
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/add-work", data={
            "canonical_system": "toh", "canonical_number": "3824",
            "english_title": "Root", "author_pids": str(pid)})
    db = connect(app.config["DB_PATH"])
    w = db.execute("SELECT id FROM work WHERE canonical_number='3824'").fetchone()[0]
    names = [n for (n,) in db.execute(
        "SELECT p.primary_name FROM work_author wa JOIN person p ON p.id = wa.person_id "
        "WHERE wa.work_id = ?", (w,))]
    assert names == ["Chosen Person"]                           # only the hand-picked author


def test_84000_index_is_process_cached():
    """Perf regression: the 84000 authority index is parsed ONCE and reused — the
    works-search autocomplete builds it per request, so a fresh parse per keystroke
    (≈170ms) made the type-ahead laggy. shared_84000_index() returns one instance."""
    from catalogue.services.work_canonical_resolver import shared_84000_index
    assert shared_84000_index() is shared_84000_index()
