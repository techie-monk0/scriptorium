"""Tests for the modular candidate picker core (catalogue/picker.py) and its web UI."""
from __future__ import annotations

import pytest

from catalogue.services import picker, verify
from catalogue.db_store import init_db
from catalogue.services.http_util import AuthorityUnavailable


@pytest.fixture(autouse=True)
def _offline_wikidata(monkeypatch):
    """Keep these tests OFFLINE: a non-wikidata bind now reverse-resolves to its hub via
    the live API, and _harvest_extra degrades gracefully (keeps the raw id) when the client
    is unavailable. Stub the client to 'offline' so id-binding tests stay deterministic and
    never hit the network. (Tests that need a specific harvest monkeypatch _harvest_extra.)"""
    import catalogue.services.wikidata as W
    def _boom(url):
        raise AuthorityUnavailable("offline (test)")
    monkeypatch.setattr(verify, "_wikidata_client",
                        lambda: W.WikidataClient(transport=_boom))


# ── a kind-agnostic fake provider (no network) ────────────────────────────────
class _FakeProvider:
    def __init__(self, name, cands):
        self.name = name
        self._cands = cands

    def candidates(self, db, text, aliases=()):
        return list(self._cands)


def _cand(id, source="bdrc", label="L", score=1.0):
    return picker.Candidate(id, source, label, score=score)


# ── pure helpers ──────────────────────────────────────────────────────────────
def test_authority_url_forms():
    assert picker.authority_url("bdr:P84").endswith("/resource/P84")
    assert picker.authority_url("wikidata:Q84").endswith("/wiki/Q84")
    assert picker.authority_url("viaf:12345").endswith("/viaf/12345")
    assert picker.authority_url("toh:84").endswith("toh84.html")
    assert picker.authority_url("bogus:1") == ""


def test_parse_choice_branches():
    assert picker.parse_choice("2", 3) == ("bind", 1)
    assert picker.parse_choice("q", 3) == ("quit", None)
    assert picker.parse_choice("", 3) == ("skip", None)
    assert picker.parse_choice("s", 3) == ("skip", None)
    assert picker.parse_choice("l", 3) == ("local", None)
    assert picker.parse_choice("9", 3) == ("invalid", None)     # out of range
    assert picker.parse_choice("x", 3) == ("invalid", None)


# ── gather is generic over kind / providers ───────────────────────────────────
def test_gather_uses_injected_providers_in_order():
    provs = [_FakeProvider("wikidata", [_cand("wikidata:Q1", "wikidata")]),
             _FakeProvider("bdrc", [_cand("bdr:P1", "bdrc")])]
    out = picker.gather(None, "person", "X", (), providers=provs)
    assert [c.id for c in out] == ["wikidata:Q1", "bdr:P1"]      # provider order preserved


# ── bind dispatch per kind (real DB writes, no network) ───────────────────────
def test_bind_person_sets_external_id_and_crosslink(tmp_path):
    db = init_db(tmp_path / "p.db")
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Kamalashila')").lastrowid
    assert picker.bind(db, "person", pid, _cand("bdr:P7641", "bdrc", "Kamalashila")) is True
    row = db.execute("SELECT external_id, verification_status FROM person WHERE id=?",
                     (pid,)).fetchone()
    assert row == ("bdr:P7641", "verified")
    assert db.execute("SELECT value FROM person_external_id WHERE person_id=? AND scheme='bdrc'",
                      (pid,)).fetchone()[0] == "bdr:P7641"
    # already bound → no-op
    assert picker.bind(db, "person", pid, _cand("bdr:P9", "bdrc")) is False


def test_bind_work_sets_canonical(tmp_path):
    db = init_db(tmp_path / "w.db")
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    assert picker.bind(db, "work", wid, _cand("toh:21", "toh", "Heart Sutra")) is True
    row = db.execute("SELECT canonical_system, canonical_number FROM work WHERE id=?",
                     (wid,)).fetchone()
    assert row == ("toh", "21")
    # bdrc work id keeps its full namespaced number
    wid2 = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    picker.bind(db, "work", wid2, _cand("bdr:WA123", "bdrc", "Another"))
    assert db.execute("SELECT canonical_system, canonical_number FROM work WHERE id=?",
                      (wid2,)).fetchone() == ("bdrc", "bdr:WA123")


# ── unresolved listers ─────────────────────────────────────────────────────────
def test_unresolved_person_skips_bound(tmp_path):
    db = init_db(tmp_path / "u.db")
    db.execute("INSERT INTO person (primary_name, verification_status) VALUES ('A','provisional')")
    db.execute("INSERT INTO person (primary_name, verification_status, external_id) "
               "VALUES ('B','verified','wikidata:Q1')")
    rows = picker.unresolved(db, "person")
    assert [r[1] for r in rows] == ["A"]                        # only the unbound provisional


def test_unresolved_and_search_skip_tombstoned(tmp_path):
    """A soft-deleted person drops off the picker worklist AND out of the merge-target
    search — the routed reads see live persons only."""
    from catalogue.services import contributor_edit as CE
    from catalogue.db_store import add_alias
    db = init_db(tmp_path / "t.db")
    a = db.execute("INSERT INTO person (primary_name, verification_status) "
                   "VALUES ('Gone Soul','provisional')").lastrowid
    add_alias(db, "person", a, "Gone Soul", "english")
    db.commit()
    assert a in [r[0] for r in picker.unresolved(db, "person")]
    assert a in [r[0] for r in picker._search_persons(db, "Gone", -1)]
    CE.apply_delete(db, a)                                      # tombstone
    assert a not in [r[0] for r in picker.unresolved(db, "person")]
    assert a not in [r[0] for r in picker._search_persons(db, "Gone", -1)]


# ── interactive loop (input_fn / out injected) ────────────────────────────────
def test_run_cli_binds_chosen_candidate(tmp_path):
    db = init_db(tmp_path / "cli.db")
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('Tsongkhapa','provisional')").lastrowid
    provs = [_FakeProvider("wikidata", [_cand("wikidata:Q323439", "wikidata", "Tsongkhapa")])]
    answers = iter(["1"])                                       # pick candidate #1
    tally = picker.run_cli(db, "person", providers=provs,
                           input_fn=lambda prompt: next(answers), out=lambda *a: None)
    assert tally["bound"] == 1
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] \
        == "wikidata:Q323439"


def test_run_cli_research_then_bind(tmp_path):
    # The stored name surfaces nothing useful; operator types /better-term, the new
    # candidate appears, and #1 binds it. Proves the re-search path.
    db = init_db(tmp_path / "rs.db")
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('Dge-Dun-Rgya-Mtsho','provisional')").lastrowid

    class _QueryAwareProvider:
        name = "bdrc"
        def candidates(self, db, text, aliases=()):
            if "gendun" in text.lower():
                return [_cand("bdr:P84", "bdrc", "Dalai Lama 02 Gendun Gyatso")]
            return []                                   # stored Wylie form finds nothing

    answers = iter(["/Gendun Gyatso", "1"])             # re-search, then pick #1
    tally = picker.run_cli(db, "person", providers=[_QueryAwareProvider()],
                           input_fn=lambda p: next(answers), out=lambda *a: None)
    assert tally["bound"] == 1
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] \
        == "bdr:P84"


def test_run_cli_skip_leaves_unbound(tmp_path):
    db = init_db(tmp_path / "cli2.db")
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('X','provisional')").lastrowid
    provs = [_FakeProvider("bdrc", [_cand("bdr:P1", "bdrc")])]
    answers = iter(["s"])
    tally = picker.run_cli(db, "person", providers=provs,
                           input_fn=lambda prompt: next(answers), out=lambda *a: None)
    assert tally["skipped"] == 1
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None


# ── web UI smoke ───────────────────────────────────────────────────────────────
def test_picker_ui_routes(tmp_path, monkeypatch):
    from catalogue.webui.web import create_app
    dbp = tmp_path / "web.db"
    db = init_db(dbp)
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('Tsongkhapa','provisional')").lastrowid
    db.commit()
    # stub gather so the candidates fragment needs no network
    monkeypatch.setattr(picker, "gather",
                        lambda db, kind, text, aliases=(), providers=None:
                        [_cand("wikidata:Q323439", "wikidata", "Tsongkhapa")])
    app = create_app(str(dbp))
    c = app.test_client()

    assert c.get("/picker").status_code in (301, 302)           # → /picker/person
    assert b"Tsongkhapa" in c.get("/picker/person").data
    frag = c.get(f"/picker/person/{pid}/candidates").data
    assert b"wikidata:Q323439" in frag
    # re-search path: ?q=… re-queries and the fragment echoes the query box
    frag2 = c.get(f"/picker/person/{pid}/candidates?q=Tsongkhapa").data
    assert b"wikidata:Q323439" in frag2 and b're-search' in frag2
    # the candidate toolbar has a dedicated authority-id box, and the old `alias`
    # shortcut button is gone (alias search now lives on the alias form)
    assert b"data-id-search" in frag and b"focusAlias" not in frag
    # paste-an-id path: a `bdr:P…` query resolves to that exact record (NOT via the
    # stubbed name gather, which would have returned the wikidata cand above), and
    # the id echoes back into the dedicated id box (not the name re-search box)
    fid = c.get(f"/picker/person/{pid}/candidates?q=bdr:P4954").data
    assert b"bdr:P4954" in fid and b"wikidata:Q323439" not in fid
    assert b'value="bdr:P4954"' in fid                       # echoed into the id box
    # a work id pasted under People is a clean "no candidates", not a bogus bind
    miss = c.get(f"/picker/person/{pid}/candidates?q=bdr:W999").data
    assert b"no candidates" in miss

    r = c.post(f"/picker/person/{pid}/bind",
               data={"candidate_id": "wikidata:Q323439", "source": "wikidata",
                     "label": "Tsongkhapa"},
               headers={"X-Requested-With": "fetch"})
    assert r.get_json()["ok"] is True
    assert db.execute("SELECT external_id FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "wikidata:Q323439"


# ── person authority search box (add/edit-person auto-fill) ───────────────────
def test_person_authority_search_endpoint(tmp_path, monkeypatch):
    """The name-based authority search returns ranked candidates {id, source, label}
    from gather (BDRC/Wikidata/VIAF) — the person twin of /works/authority/search."""
    from catalogue.webui.web import create_app
    dbp = tmp_path / "pa.db"; init_db(dbp)
    monkeypatch.setattr(picker, "gather",
                        lambda db, kind, text, aliases=(), providers=None:
                        [_cand("bdr:P64", "bdrc", "Tsong kha pa"),
                         picker.Candidate("wikidata:Q323439", "wikidata", "Tsongkhapa",
                                          detail="Tibetan teacher, 1357–1419")])
    c = create_app(str(dbp)).test_client()
    j = c.get("/picker/person/authority/search?q=tsongkhapa").get_json()
    ids = [m["id"] for m in j["matches"]]
    assert ids == ["bdr:P64", "wikidata:Q323439"]
    assert j["matches"][0]["source"] == "bdrc" and j["matches"][0]["label"] == "Tsong kha pa"
    assert c.get("/picker/person/authority/search?q=").get_json() == {"matches": []}


def test_people_new_parks_pick_as_suggestion_when_no_match(tmp_path, monkeypatch):
    """A picked authority id with no existing match: the person is created PROVISIONAL with
    the pick parked as suggested_external_id (external_id stays NULL) so it enters the
    review worklist, where acceptance runs on-bind dedup. (Was: blindly wrote external_id,
    bypassing dedup — the bug that minted duplicate hubs.)"""
    from catalogue.webui.web import create_app
    # Offline: a BDRC pick that doesn't reverse-resolve (genuine miss → bind on raw id).
    monkeypatch.setattr(picker, "_harvest_extra",
                        lambda ext: (None, None, {"bdrc": ext}))
    dbp = tmp_path / "pn.db"; init_db(dbp)
    c = create_app(str(dbp)).test_client()
    r = c.post("/people/new", data={"primary_name": "Tsongkhapa", "role_hint": "author",
                                    "dates": "1357–1419", "external_id": "bdr:P64"})
    assert r.status_code in (302, 303)
    db = init_db(dbp)
    row = db.execute("SELECT dates, external_id, suggested_external_id, verification_status "
                     "FROM person WHERE primary_name='Tsongkhapa'").fetchone()
    assert row == ("1357–1419", None, "bdr:P64", "provisional")


def test_people_new_reuses_existing_person_cross_scheme(tmp_path, monkeypatch):
    """A BDRC pick that reverse-resolves to a Wikidata hub already held by another person
    must REUSE that person (cross-scheme dedup at create time), not create a duplicate —
    and add the typed spelling as an alias."""
    from catalogue.webui.web import create_app
    from catalogue.db_store import add_alias
    dbp = tmp_path / "pn.db"; db = init_db(dbp)
    # An existing person bound to the Wikidata hub (+ a viaf cross-link).
    pid = db.execute("INSERT INTO person (primary_name, external_id, verification_status) "
                     "VALUES ('Aryashura', 'wikidata:Q109478559', 'verified')").lastrowid
    add_alias(db, "person", pid, "Aryashura", "english")
    db.execute("INSERT INTO person_external_id (person_id, scheme, value) "
               "VALUES (?, 'wikidata', 'wikidata:Q109478559')", (pid,))
    db.commit()
    # The BDRC pick harvests up to the SAME hub.
    monkeypatch.setattr(picker, "_harvest_extra", lambda ext: (
        "Āryaśūra", [], {"wikidata": "wikidata:Q109478559", "bdrc": ext}))
    c = create_app(str(dbp)).test_client()
    r = c.post("/people/new", data={"primary_name": "Āryaśūra", "external_id": "bdr:P999"})
    assert r.status_code in (302, 303) and f"/person/{pid}" in r.headers["Location"]
    db = init_db(dbp)
    assert db.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 1   # no duplicate
    # the typed spelling landed as an alias on the existing person
    keys = [x[0] for x in db.execute(
        "SELECT text FROM person_alias WHERE person_id=?", (pid,)).fetchall()]
    assert "Āryaśūra" in keys


def test_accepting_suggestion_binds_and_clears_it(tmp_path, monkeypatch):
    """Accepting a suggested binding in the worklist binds the person (verified) and clears
    suggested_external_id, via the same dedup-aware path as picking a candidate."""
    from catalogue.webui.web import create_app
    from catalogue.db_store import add_alias
    dbp = tmp_path / "pn.db"; db = init_db(dbp)
    pid = db.execute("INSERT INTO person (primary_name, suggested_external_id) "
                     "VALUES ('New Author', 'wikidata:Q1')").lastrowid
    add_alias(db, "person", pid, "New Author", "english")
    db.commit()
    # The bind harvests the hub (no cross-links, no other holder → a clean verify).
    monkeypatch.setattr(picker, "_harvest_extra",
                        lambda ext: (None, None, {"wikidata": ext}))
    c = create_app(str(dbp)).test_client()
    r = c.post(f"/picker/person/{pid}/bind",
               data={"candidate_id": "wikidata:Q1", "source": "suggested", "label": "New Author"},
               headers={"X-Requested-With": "fetch"})
    assert r.get_json()["ok"]
    db = init_db(dbp)
    row = db.execute("SELECT external_id, suggested_external_id, verification_status "
                     "FROM person WHERE id=?", (pid,)).fetchone()
    assert row == ("wikidata:Q1", None, "verified")


def test_people_page_offers_authority_search(tmp_path):
    from catalogue.webui.web import create_app
    dbp = tmp_path / "pp.db"; init_db(dbp)
    page = create_app(str(dbp)).test_client().get("/people").data.decode()
    assert "mountPersonAuthoritySearch" in page and 'class="pa-search"' in page
    assert 'name="external_id"' in page and 'name="dates"' in page     # fields the search fills
    # the add/edit section comes BEFORE the list (top of page)
    assert page.index("New person") < page.index("All people")


def test_people_list_shows_all_and_searches_all_aliases(tmp_path):
    """No q → every person listed (no cap). ?q → match on ANY alias, not just the name."""
    from catalogue.db_store import add_alias
    from catalogue.webui.web import create_app
    dbp = tmp_path / "pl.db"; db = init_db(dbp)
    # 250 people (> the old LIMIT 200) so "all listed" is a real assertion
    for i in range(250):
        db.execute("INSERT INTO person (primary_name) VALUES (?)", (f"Person {i:03d}",))
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Tsongkhapa')").lastrowid
    add_alias(db, "person", pid, "Lobzang Drakpa", "english")   # a NON-primary alias
    db.commit()
    c = create_app(str(dbp)).test_client()

    page = c.get("/people").data.decode()
    assert "251 people" in page                                  # all listed, past the old 200 cap
    assert "Person 249" in page

    # searching a NON-primary alias surfaces the person (search is over every alias)
    hit = c.get("/people?q=Lobzang Drakpa").data.decode()
    assert "Tsongkhapa" in hit and "1 match" in hit
    assert "Person 000" not in hit                               # filtered to the match only


def test_works_list_form_at_top_all_listed_and_alias_search(tmp_path):
    """Works page: add-new section at the top, every work listed (no cap), and search
    matches on ANY alias — not just the display title."""
    from catalogue.db_store import add_alias
    from catalogue.webui.web import create_app
    dbp = tmp_path / "wl.db"; db = init_db(dbp)
    for i in range(250):                                          # > the old LIMIT 200
        wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
        add_alias(db, "work", wid, f"Work {i:03d}", "english")
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, "Mūlamadhyamakakārikā", "iast")   # display title
    add_alias(db, "work", wid, "Root Verses on the Middle Way", "english")   # a NON-primary alias
    db.commit()
    c = create_app(str(dbp)).test_client()

    page = c.get("/works").data.decode()
    assert page.index("New work") < page.index("All works")      # add-new at the top
    assert "251 works" in page and "Work 249" in page            # all listed, past the old cap

    hit = c.get("/works?q=Root Verses").data.decode()            # search a NON-primary alias
    assert "Mūlamadhyamakakārikā" in hit and "1 match" in hit    # surfaces the work (by display title)
    assert "Work 000" not in hit


# ── dry-run: experimentation never persists ───────────────────────────────────
def test_dry_run_connection_swallows_commit(tmp_path):
    from catalogue.db_store import DryRunConnection
    raw = init_db(tmp_path / "dr.db")
    pid = raw.execute("INSERT INTO person (primary_name) VALUES ('X')").lastrowid
    raw.commit()
    d = DryRunConnection(raw)
    d.execute("UPDATE person SET external_id='wikidata:Q1' WHERE id=?", (pid,))
    d.commit()                                  # swallowed
    d.rollback()                                # discards the write
    import sqlite3
    chk = sqlite3.connect(tmp_path / "dr.db")
    assert chk.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None


def test_web_dry_run_does_not_persist(tmp_path):
    from catalogue.webui.web import create_app
    import sqlite3
    dbp = tmp_path / "drweb.db"
    db = init_db(dbp)
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('Tsongkhapa','provisional')").lastrowid
    db.commit()
    app = create_app(str(dbp), dry_run=True)
    assert app.config["DRY_RUN"] is True
    c = app.test_client()
    assert b"DRY-RUN" in c.get("/picker/person").data        # banner shows
    r = c.post(f"/picker/person/{pid}/bind",
               data={"candidate_id": "wikidata:Q1", "source": "wikidata", "label": "T"},
               headers={"X-Requested-With": "fetch"})
    assert r.get_json()["ok"] is True                        # the op "succeeds"…
    # …but a fresh connection sees NO change — it was rolled back
    chk = sqlite3.connect(dbp)
    assert chk.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None


def test_cli_dry_run_does_not_persist(tmp_path):
    from catalogue.db_store import DryRunConnection
    import sqlite3
    dbp = tmp_path / "drcli.db"
    raw = init_db(dbp)
    pid = raw.execute("INSERT INTO person (primary_name, verification_status) "
                      "VALUES ('Tsongkhapa','provisional')").lastrowid
    raw.commit()
    db = DryRunConnection(raw)
    provs = [_FakeProvider("wikidata", [_cand("wikidata:Q323439", "wikidata", "Tsongkhapa")])]
    picker.run_cli(db, "person", ids=[pid], providers=provs,
                   input_fn=lambda p: "1", out=lambda *a: None)
    raw.rollback()                                            # what main() does in dry-run
    chk = sqlite3.connect(dbp)
    assert chk.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None


# ── a DB-level bind failure is SURFACED, not silently swallowed ───────────────
def test_bind_failure_returns_explicit_error(tmp_path, monkeypatch):
    """The bug's signature was a bind that 500'd server-side and vanished on
    reload with no signal. A write error must now come back as an explicit
    {ok: false, error: …} so the UI can tell the operator it did NOT save."""
    from catalogue.webui.web import create_app
    from catalogue.db_store import WriteError
    dbp = tmp_path / "fail.db"
    db = init_db(dbp)
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('Tsongkhapa','provisional')").lastrowid
    db.commit()
    monkeypatch.setattr(picker, "gather",
                        lambda db, kind, text, aliases=(), providers=None:
                        [_cand("wikidata:Q1", "wikidata", "T")])
    # Force the bind write to fail the way a missing column / rolled-back txn does.
    def boom(*a, **k):
        raise WriteError("write affected 0 row(s), expected 1")
    monkeypatch.setattr(picker, "bind_with_dedup", boom)
    app = create_app(str(dbp))
    c = app.test_client()
    r = c.post(f"/picker/person/{pid}/bind",
               data={"candidate_id": "wikidata:Q1", "source": "wikidata", "label": "T"},
               headers={"X-Requested-With": "fetch"})
    body = r.get_json()
    assert body["ok"] is False and "WriteError" in body["error"]


# ── bulk (multi-select) operations ────────────────────────────────────────────
def test_parse_selection_ranges_all_and_dedupe():
    ids = [10, 11, 12, 13, 14]                       # 1-based display → these ids
    assert picker._parse_selection("1-3,5", ids) == [10, 11, 12, 14]
    assert picker._parse_selection("all", ids) == ids
    assert picker._parse_selection("*", ids) == ids
    assert picker._parse_selection("2 2 2", ids) == [11]          # deduped
    assert picker._parse_selection("0 99 junk", ids) == []        # out of range / junk
    assert picker._parse_selection("", ids) == []


def test_bulk_ops_are_kind_specific():
    pkeys = [o.key for o in picker.bulk_ops("person")]
    wkeys = [o.key for o in picker.bulk_ops("work")]
    assert pkeys == ["create_new", "mark_org", "merge", "delete"]
    assert wkeys == ["merge"]                         # works: only merge
    assert picker.bulk_ops("nonesuch") == []
    # the merge op declares it needs a chosen survivor
    assert next(o for o in picker.bulk_ops("person") if o.key == "merge").target == "survivor"


def _provisional(db, name):
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES (?, 'provisional')", (name,)).lastrowid
    from catalogue.db_store import add_alias
    add_alias(db, "person", pid, name, "english")
    return pid


def test_bulk_apply_create_new_confirms_each(tmp_path):
    db = init_db(tmp_path / "b.db")
    ids = [_provisional(db, n) for n in ("A", "B", "C")]
    db.commit()
    res = picker.bulk_apply(db, "person", "create_new", ids)
    assert len(res["ok"]) == 3 and not res["failed"]
    rows = db.execute(
        "SELECT verification_status FROM person WHERE id IN (%s)"
        % ",".join("?" * len(ids)), ids).fetchall()
    assert all(r[0] == "confirmed_local" for r in rows)
    # an already-AUTHORITY-bound person can't be confirmed-local → skipped, not failed
    db.execute("UPDATE person SET external_id='bdr:P1' WHERE id=?", (ids[0],))
    db.commit()
    res2 = picker.bulk_apply(db, "person", "create_new", [ids[0]])
    assert len(res2["skipped"]) == 1 and not res2["ok"] and not res2["failed"]


def test_bulk_apply_mark_org_and_delete(tmp_path):
    db = init_db(tmp_path / "b2.db")
    a, b = _provisional(db, "Org One"), _provisional(db, "Org Two")
    db.commit()
    picker.bulk_apply(db, "person", "mark_org", [a, b])
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (a,)).fetchone()[0] == "organization"
    c, d = _provisional(db, "Gone One"), _provisional(db, "Gone Two")
    db.commit()
    res = picker.bulk_apply(db, "person", "delete", [c, d])
    assert len(res["ok"]) == 2
    # soft-delete: rows are tombstoned (gone from live reads), not physically removed
    assert db.execute("SELECT COUNT(*) FROM v_live_person WHERE id IN (?,?)",
                      (c, d)).fetchone()[0] == 0


def test_bulk_apply_merge_folds_into_survivor(tmp_path):
    db = init_db(tmp_path / "b3.db")
    keep = _provisional(db, "Canonical")
    d1, d2 = _provisional(db, "Dup One"), _provisional(db, "Dup Two")
    db.commit()
    res = picker.bulk_apply(db, "person", "merge", [keep, d1, d2], target=keep)
    assert res["target"] == keep and len(res["ok"]) == 2 and not res["failed"]
    # the survivor stays; the two duplicates are gone…
    assert db.execute("SELECT COUNT(*) FROM person WHERE id IN (?,?)",
                      (d1, d2)).fetchone()[0] == 0
    assert db.execute("SELECT 1 FROM person WHERE id=?", (keep,)).fetchone()
    # …and each merged-away NAME survives as an alias of the survivor (never lost)
    from catalogue.db_store import fold_key
    keys = {r[0] for r in db.execute(
        "SELECT normalized_key FROM person_alias WHERE person_id=?", (keep,)).fetchall()}
    assert fold_key("Dup One") in keys and fold_key("Dup Two") in keys


def test_bulk_apply_merge_needs_valid_target(tmp_path):
    db = init_db(tmp_path / "b4.db")
    a, b = _provisional(db, "A"), _provisional(db, "B")
    db.commit()
    import pytest
    with pytest.raises(ValueError):                  # no target
        picker.bulk_apply(db, "person", "merge", [a, b])
    with pytest.raises(ValueError):                  # target not among the selection
        picker.bulk_apply(db, "person", "merge", [a, b], target=999)
    with pytest.raises(ValueError):                  # unknown op
        picker.bulk_apply(db, "person", "frobnicate", [a, b])
    with pytest.raises(ValueError):                  # empty selection
        picker.bulk_apply(db, "person", "create_new", [])


def test_run_bulk_cli_select_all_then_delete(tmp_path):
    db = init_db(tmp_path / "b5.db")
    ids = [_provisional(db, n) for n in ("X", "Y", "Z")]
    db.commit()
    # scripted operator: select all → op 4 (delete) → confirm y
    replies = iter(["all", "4", "y"])
    out_lines = []
    res = picker.run_bulk_cli(db, "person", input_fn=lambda p: next(replies),
                              out=out_lines.append)
    assert len(res["ok"]) == 3
    # soft-delete: tombstoned, so gone from live reads (rows remain, id frozen)
    assert db.execute("SELECT COUNT(*) FROM v_live_person WHERE id IN (%s)"
                      % ",".join("?" * len(ids)), ids).fetchone()[0] == 0


def test_run_bulk_cli_merge_picks_target(tmp_path):
    db = init_db(tmp_path / "b6.db")
    keep, dup = _provisional(db, "Keeper"), _provisional(db, "Folded")
    db.commit()
    # select all → op 3 (merge) → target row 1 (Keeper) → confirm y
    replies = iter(["all", "3", "1", "y"])
    res = picker.run_bulk_cli(db, "person", input_fn=lambda p: next(replies),
                              out=lambda *a: None)
    assert res["op"] == "merge" and len(res["ok"]) == 1
    assert db.execute("SELECT 1 FROM person WHERE id=?", (dup,)).fetchone() is None
    assert db.execute("SELECT 1 FROM person WHERE id=?", (keep,)).fetchone()


def test_web_picker_bulk_route(tmp_path):
    from catalogue.webui.web import create_app
    dbp = tmp_path / "bweb.db"
    db = init_db(dbp)
    ids = [_provisional(db, n) for n in ("P", "Q")]
    db.commit()
    app = create_app(str(dbp))
    c = app.test_client()
    # the action bar + per-row checkboxes render on the list page
    page = c.get("/picker/person").data
    assert b"bb-selall" in page and b"bb-check" in page and b"Apply to selected" in page
    r = c.post("/picker/person/bulk",
               json={"op": "create_new", "ids": ids},
               headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200                       # success is the HTTP status
    body = r.get_json()                               # body IS the result dict
    assert body["op"] == "create_new" and len(body["ok"]) == 2 and not body["failed"]
    chk = init_db(dbp)
    assert all(chk.execute("SELECT verification_status FROM person WHERE id=?",
                           (i,)).fetchone()[0] == "confirmed_local" for i in ids)


def test_web_picker_bulk_bad_request_is_400(tmp_path):
    from catalogue.webui.web import create_app
    dbp = tmp_path / "bweb2.db"
    db = init_db(dbp)
    a = _provisional(db, "A")
    db.commit()
    app = create_app(str(dbp))
    c = app.test_client()
    r = c.post("/picker/person/bulk", json={"op": "merge", "ids": [a]},
               headers={"X-Requested-With": "fetch"})
    assert r.status_code == 400 and "error" in r.get_json()


# ── direct authority-id lookup (paste `bdr:P123` / `wikidata:Q42` in the box) ────
def test_looks_like_authority_id():
    assert picker.looks_like_authority_id("bdr:P123")
    assert picker.looks_like_authority_id("wikidata:Q42")
    assert picker.looks_like_authority_id("WD:Q42")          # scheme is case-insensitive
    assert not picker.looks_like_authority_id("Tsongkhapa")  # no scheme
    assert not picker.looks_like_authority_id("wikidata:")   # empty tail
    assert not picker.looks_like_authority_id("bogus:1")     # unknown scheme


def test_lookup_by_id_plain_name_returns_none():
    # Not id-shaped → caller falls back to a name search.
    assert picker.lookup_by_id(None, "person", "Tsongkhapa") is None


def test_lookup_by_id_wrong_kind_is_no_match():
    assert picker.lookup_by_id(None, "person", "bdr:W123") == []   # work id under People
    assert picker.lookup_by_id(None, "work", "bdr:P123") == []     # person id under Works
    assert picker.lookup_by_id(None, "person", "toh:182") == []    # toh is work-only


def test_lookup_by_id_bdr_person():
    [c] = picker.lookup_by_id(None, "person", "bdr:P123")
    assert c.id == "bdr:P123" and c.source == "bdrc"
    assert c.url == "https://purl.bdrc.io/resource/P123"


def test_lookup_by_id_scheme_aliases_normalise():
    [c] = picker.lookup_by_id(None, "person", "bdrc:P123")     # bdrc → bdr
    assert c.id == "bdr:P123"


def test_lookup_by_id_viaf_toh_dila_well_formed():
    [v] = picker.lookup_by_id(None, "person", "viaf:99")
    assert v.id == "viaf:99" and v.url.endswith("/viaf/99")
    [t] = picker.lookup_by_id(None, "work", "toh:182")
    assert t.id == "toh:182"
    [d] = picker.lookup_by_id(None, "person", "dila:A001")
    assert d.id == "dila:A001"


def test_lookup_by_id_wikidata_hit(monkeypatch):
    ent = {"labels": {"en": {"value": "Tsongkhapa"}},
           "descriptions": {"en": {"value": "Tibetan teacher"}}}

    class _WD:
        def entity(self, qid):
            assert qid == "Q42"
            return ent
    monkeypatch.setattr(picker.verify, "_wikidata_client", lambda: _WD())
    [c] = picker.lookup_by_id(None, "person", "wikidata:Q42")
    assert c.id == "wikidata:Q42" and c.label == "Tsongkhapa"
    assert c.detail == "Tibetan teacher"


def test_lookup_by_id_wikidata_miss(monkeypatch):
    class _WD:
        def entity(self, qid):
            return None
    monkeypatch.setattr(picker.verify, "_wikidata_client", lambda: _WD())
    assert picker.lookup_by_id(None, "person", "wikidata:Q999999") == []
