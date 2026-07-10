"""Unit + regression + E2E for authority-driven person dedup (catalogue/person_dedup.py).

Offline by design: no test touches the network. The one reharvest test injects a
fake cross-link harvester via monkeypatch.
"""
from __future__ import annotations

import json

import pytest

from catalogue.services import contributor_edit as CE
from catalogue.services import person_dedup as PD
from catalogue.services import verify
from catalogue.db_store import add_alias, connect, fold_key, init_db
from catalogue.services.http_util import AuthorityUnavailable
from catalogue.db_store.integrity import check_integrity
from catalogue.webui.web import create_app


# ── fixture helpers ────────────────────────────────────────────────────────────
def _person(db, name, ext=None, status="provisional"):
    pid = db.execute(
        "INSERT INTO person (primary_name, external_id, verification_status) "
        "VALUES (?,?,?)", (name, ext, status)).lastrowid
    add_alias(db, "person", pid, name, "english")
    return pid


def _xid(db, pid, scheme, value):
    db.execute("INSERT OR REPLACE INTO person_external_id (person_id, scheme, value) "
               "VALUES (?,?,?)", (pid, scheme, value))


def _work(db, title):
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?,?, 'english', ?)", (wid, title, fold_key(title)))
    return wid


def _author(db, wid, pid):
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?, 'author')",
               (wid, pid))


# ── key_set: person-type filter (§6.3) ─────────────────────────────────────────
def test_key_set_filters_wrong_type(tmp_path):
    db = init_db(tmp_path / "k.db")
    p = _person(db, "Someone", ext="wikidata:Q1")
    _xid(db, p, "bdrc", "bdr:P5")        # BDRC person — kept
    _xid(db, p, "dila", "dila:A9")       # DILA person — kept
    _xid(db, p, "viaf", "viaf:7")        # VIAF — kept
    # a BDRC *Topic* id (non-person) sneaks in via a second scheme row — must drop
    db.execute("INSERT INTO person_external_id (person_id, scheme, value) "
               "VALUES (?, 'topic', 'bdr:T123')", (p,))
    assert PD.key_set(db, p) == {"wikidata:Q1", "bdr:P5", "dila:A9", "viaf:7"}


# ── canonical selection (§1.1) ─────────────────────────────────────────────────
def test_choose_canonical_most_edges_then_verified_then_lowest_id(tmp_path):
    db = init_db(tmp_path / "c.db")
    a = _person(db, "A", ext="wikidata:Q1", status="provisional")
    b = _person(db, "B", ext="wikidata:Q1", status="verified")
    c = _person(db, "C", ext="wikidata:Q1", status="verified")
    # b gets the most edges → wins outright
    w = _work(db, "W"); _author(db, w, b); w2 = _work(db, "W2"); _author(db, w2, b)
    assert PD.choose_canonical(db, [a, b, c]) == b
    # with no edges, verified beats provisional, then lowest id
    assert PD.choose_canonical(db, [a, c]) == c          # c verified, a provisional
    d = _person(db, "D", status="verified"); e = _person(db, "E", status="verified")
    assert PD.choose_canonical(db, [e, d]) == d          # tie → lowest id


# ── components: shared hub id ───────────────────────────────────────────────────
def test_components_groups_shared_hub(tmp_path):
    db = init_db(tmp_path / "g.db")
    ids = [_person(db, f"Tsongkhapa v{i}", ext="wikidata:Q323439") for i in range(3)]
    solo = _person(db, "Someone Else", ext="wikidata:Q999")  # noqa: F841
    comps = PD.components(db)
    assert len(comps) == 1
    c = comps[0]
    assert sorted(c.members) == sorted(ids)
    assert c.routed == "merge"


def test_tombstoned_person_excluded_from_dedup(tmp_path):
    """A soft-deleted person must NOT be a dedup candidate — the engine reads live only,
    so tombstoning a member drops it from the component and from key lookups (regression
    guard for the apply_delete=tombstone interaction)."""
    db = init_db(tmp_path / "tomb.db")
    a, b, c = (_person(db, f"Milarepa v{i}", ext="wikidata:Q312477") for i in range(3))
    db.commit()
    assert {m for m in PD.components(db)[0].members} == {a, b, c}
    CE.apply_delete(db, c)                            # tombstone one member
    comps = PD.components(db)
    assert len(comps) == 1 and set(comps[0].members) == {a, b}
    assert c not in PD.persons_with_keys(db, {"wikidata:Q312477"})
    assert PD.key_set(db, c) == set()                 # a tombstone owns no identity keys


# ── cross-authority via a STORED cross-link (E2E-4) ─────────────────────────────
def test_cross_authority_stored_link(tmp_path):
    db = init_db(tmp_path / "x.db")
    a = _person(db, "Name A", ext="wikidata:Qx", status="verified")
    b = _person(db, "Name B", ext="bdr:Py", status="verified")
    _xid(db, a, "bdrc", "bdr:Py")            # A carries the cross-link to B's hub id
    comps = PD.components(db)
    assert len(comps) == 1 and set(comps[0].members) == {a, b}
    # the relaxed guard is load-bearing: a bare merge of two different ids refuses
    assert "different authorities" in CE.plan_merge(db, b, a)["error"]
    assert "error" not in CE.plan_merge(db, b, a, allow_cross_authority=True)


# ── asymmetric link needs --reharvest (E2E-5) ──────────────────────────────────
def test_asymmetric_link_only_merges_with_reharvest(tmp_path, monkeypatch):
    db = init_db(tmp_path / "r.db")
    a = _person(db, "Name A", ext="wikidata:Qx", status="verified")
    b = _person(db, "Name B", ext="bdr:Py", status="verified")  # noqa: F841
    # offline: nothing links Qx ↔ Py → two separate records, no merge
    assert PD.plan_batch(db)["merge"] == []
    # inject a live harvester that reveals Qx → bdr:Py (no real network)
    monkeypatch.setattr(PD, "_harvest_cross_links",
                        lambda key, verifiers=None: {"bdr:Py"} if key == "wikidata:Qx" else set())
    plan = PD.plan_batch(db, reharvest=True)
    assert len(plan["merge"]) == 1
    assert set(plan["merge"][0]["members"]) == {a, b}


# ── over-merge guards (E2E-6, §6.1/6.2) ─────────────────────────────────────────
def test_multihub_component_routed_to_review(tmp_path):
    db = init_db(tmp_path / "mh.db")
    a = _person(db, "Real Person 1", ext="wikidata:Q1", status="verified")
    b = _person(db, "Real Person 2", ext="wikidata:Q2", status="verified")
    _xid(db, a, "bdrc", "bdr:Pbad")          # a bad bridging cross-link shared by
    _xid(db, b, "bdrc", "bdr:Pbad")          # two DISTINCT hub ids → conflated
    plan = PD.plan_batch(db)
    assert plan["merge"] == []
    assert len(plan["review"]) == 1 and "conflated" in plan["review"][0]["reason"]
    # and applying changes nothing
    res = PD.apply_batch(db, plan)
    assert res["merged"] == 0
    assert {r[0] for r in db.execute("SELECT external_id FROM person")} == {"wikidata:Q1", "wikidata:Q2"}


def test_oversized_component_routed_to_review(tmp_path):
    db = init_db(tmp_path / "os.db")
    for i in range(3):
        _person(db, f"v{i}", ext="wikidata:Q1")
    plan = PD.plan_batch(db, max_component=2)
    assert plan["merge"] == [] and "oversized" in plan["review"][0]["reason"]


# ── headline collapse + idempotency (E2E-1, E2E-2) ─────────────────────────────
def test_tsongkhapa_collapse_and_idempotent(tmp_path):
    db = init_db(tmp_path / "tk.db")
    names = ["Tsongkhapa", "Je Tsongkhapa", "Lama Jey Tsongkhapa",
             "Lama Tsong Khapa", "Tsongkhapa Lobzang Drakpa", "Rje Tsong kha pa"]
    pids = [_person(db, n, ext="wikidata:Q323439") for n in names]
    # spread edges across several of the duplicates
    for i, p in enumerate(pids):
        w = _work(db, f"Work {i}")
        _author(db, w, p)
    total_authors_before = db.execute("SELECT COUNT(*) FROM work_author").fetchone()[0]

    plan = PD.plan_batch(db)
    assert len(plan["merge"]) == 1
    log = tmp_path / "merge.jsonl"
    res = PD.apply_batch(db, plan, log_path=str(log), commit=True)

    # exactly one person remains for the hub id, with all 6 names as aliases
    survivors = db.execute(
        "SELECT id FROM person WHERE external_id = 'wikidata:Q323439'").fetchall()
    assert len(survivors) == 1
    sid = survivors[0][0]
    alias_keys = {r[0] for r in db.execute(
        "SELECT normalized_key FROM person_alias WHERE person_id = ?", (sid,))}
    assert all(fold_key(n) in alias_keys for n in names)
    # all author edges preserved, now all on the survivor; integrity clean
    assert db.execute("SELECT COUNT(*) FROM work_author").fetchone()[0] == total_authors_before
    assert db.execute(
        "SELECT COUNT(*) FROM work_author WHERE person_id = ?", (sid,)).fetchone()[0] == total_authors_before
    assert check_integrity(db)["ok"]
    assert res["merged"] == 5
    assert sum(1 for _ in log.open()) == 5            # one log line per merge
    via = json.loads(log.open().readline())["via"]
    assert "wikidata:Q323439" in via                  # the joining key recorded

    # re-run is a no-op (E2E-2)
    assert PD.plan_batch(db)["merge"] == []
    assert PD.apply_batch(db, PD.plan_batch(db))["merged"] == 0


# ── person-FK coverage audit (E2E-7, §6.10) ────────────────────────────────────
def test_person_fk_coverage(tmp_path):
    """Every table with a FK → person(id) must be handled by a merge, else a dup
    delete silently loses (or errors on) those edges. Fails on a new uncovered FK."""
    db = init_db(tmp_path / "fk.db")
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    referencing = set()
    for t in tables:
        for r in db.execute(f"PRAGMA foreign_key_list({t})").fetchall():
            if r[2] == "person":                       # r[2] = referenced table
                referencing.add(t)
    assert referencing == set(PD.PERSON_FK_HANDLED), (
        f"person FK tables {referencing} != handled {set(PD.PERSON_FK_HANDLED)} "
        "— update PERSON_FK_HANDLED and repoint logic")


# ── integrity warning surfaces & clears (E2E-9) ────────────────────────────────
def test_integrity_warning_surfaces_and_clears(tmp_path):
    db = init_db(tmp_path / "iw.db")
    label = "persons sharing an authority identity (run person-dedup batch)"
    _person(db, "Dup 1", ext="wikidata:Q500")
    _person(db, "Dup 2", ext="wikidata:Q500")
    db.commit()
    warns = {w["check"] for w in check_integrity(db)["warnings"]}
    assert label in warns
    PD.apply_batch(db, PD.plan_batch(db), commit=True)
    warns = {w["check"] for w in check_integrity(db)["warnings"]}
    assert label not in warns


# ══ Phase 2: on-bind hook ══════════════════════════════════════════════════════
def test_on_bind_auto_merges_single_strong(tmp_path):
    db = init_db(tmp_path / "ob.db")
    r = _person(db, "Established", ext="wikidata:Q1", status="verified")
    _xid(db, r, "wikidata", "wikidata:Q1")
    w = _work(db, "W"); _author(db, w, r)            # r is the richer record
    fresh = _person(db, "Variant", ext="wikidata:Q1", status="verified")
    _xid(db, fresh, "wikidata", "wikidata:Q1")
    out = PD.dedup_on_bind(db, fresh)
    # on-bind auto-merge is intentionally NOT journaled for undo (reverse via Unbind)
    assert out == {"merged_into": r, "merged_away": fresh, "via": ["wikidata:Q1"]}
    assert db.execute("SELECT COUNT(*) FROM person WHERE external_id='wikidata:Q1'").fetchone()[0] == 1
    assert db.execute("SELECT 1 FROM person WHERE id=?", (fresh,)).fetchone() is None


def test_on_bind_no_match_returns_none(tmp_path):
    db = init_db(tmp_path / "ob2.db")
    p = _person(db, "Solo", ext="wikidata:Q1", status="verified")
    _xid(db, p, "wikidata", "wikidata:Q1")
    assert PD.dedup_on_bind(db, p) is None


def test_on_bind_suggests_multiple_matches(tmp_path):
    db = init_db(tmp_path / "ob3.db")
    a = _person(db, "A", ext="wikidata:Q1", status="verified"); _xid(db, a, "bdrc", "bdr:Pbad")
    b = _person(db, "B", ext="wikidata:Q2", status="verified"); _xid(db, b, "bdrc", "bdr:Pbad")
    fresh = _person(db, "C", ext="bdr:Pbad", status="verified")
    _xid(db, fresh, "bdrc", "bdr:Pbad")
    out = PD.dedup_on_bind(db, fresh)
    assert out["suggest"] == sorted([a, b]) and "candidate" in out["reason"]
    # nothing merged
    assert db.execute("SELECT 1 FROM person WHERE id=?", (fresh,)).fetchone() is not None


def test_on_bind_suggests_conflated_hubs(tmp_path):
    db = init_db(tmp_path / "ob4.db")
    a = _person(db, "A", ext="wikidata:Q1", status="verified"); _xid(db, a, "bdrc", "bdr:Pbad")
    fresh = _person(db, "C", ext="wikidata:Q2", status="verified"); _xid(db, fresh, "bdrc", "bdr:Pbad")
    out = PD.dedup_on_bind(db, fresh)         # single match, but two distinct hub ids
    assert out["suggest"] == [a] and "conflated" in out["reason"]
    assert db.execute("SELECT 1 FROM person WHERE id=?", (fresh,)).fetchone() is not None


def test_on_bind_incomplete_suggests_not_merge(tmp_path):
    db = init_db(tmp_path / "ob5.db")
    r = _person(db, "Established", ext="wikidata:Q1", status="verified")
    _xid(db, r, "wikidata", "wikidata:Q1")
    fresh = _person(db, "Variant", ext="wikidata:Q1", status="verified")
    _xid(db, fresh, "wikidata", "wikidata:Q1")
    db.execute("UPDATE person SET harvest_incomplete=1 WHERE id=?", (fresh,))
    out = PD.dedup_on_bind(db, fresh)         # partial key-set → suggest, never merge
    assert out["suggest"] == [r] and "incomplete" in out["reason"]
    assert db.execute("SELECT 1 FROM person WHERE id=?", (fresh,)).fetchone() is not None


# ── harvest-incomplete persistence (§2.6/§6.17) ────────────────────────────────
def test_bind_records_and_clears_harvest_incomplete(tmp_path):
    db = init_db(tmp_path / "hi.db")
    p = _person(db, "X")
    # a network-degraded bind: the _incomplete sentinel sets the flag, is NOT stored
    verify.bind_person(db, p, "wikidata:Qx", "X", [], {"wikidata": "wikidata:Qx", "_incomplete": True})
    assert db.execute("SELECT harvest_incomplete FROM person WHERE id=?", (p,)).fetchone()[0] == 1
    vals = {r[0] for r in db.execute("SELECT value FROM person_external_id WHERE person_id=?", (p,))}
    assert vals == {"wikidata:Qx"}            # sentinel popped, never a junk row
    # bound-but-unharvested integrity warning surfaces it
    labels = {w["check"] for w in check_integrity(db)["warnings"]}
    assert "person bound but harvest incomplete (re-harvest pending)" in labels
    # a complete rebind clears the flag
    verify.bind_person(db, p, "wikidata:Qx", "X", [], {"wikidata": "wikidata:Qx", "bdrc": "bdr:P9"},
                       force=True)
    assert db.execute("SELECT harvest_incomplete FROM person WHERE id=?", (p,)).fetchone()[0] == 0


# ── E2E: on-bind via the HTTP route (E2E-10/11/13), offline ────────────────────
def _web(tmp_path):
    app = create_app(tmp_path / "web.db", ingest_verify=False)
    app.testing = True
    return app


def _seed_web_person(app, name, ext=None, status="provisional", with_edge=False):
    db = connect(app.config["DB_PATH"])
    pid = db.execute(
        "INSERT INTO person (primary_name, external_id, verification_status) VALUES (?,?,?)",
        (name, ext, status)).lastrowid
    add_alias(db, "person", pid, name, "english")
    if ext:
        scheme = "bdrc" if ext.startswith("bdr:") else "wikidata" if ext.startswith("wikidata:") else "viaf"
        db.execute("INSERT OR REPLACE INTO person_external_id (person_id, scheme, value) "
                   "VALUES (?,?,?)", (pid, scheme, ext))
    if with_edge:
        wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
        db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?, 'author')", (wid, pid))
    db.commit(); db.close()
    return pid


def _bind(c, pid, cid, source="bdrc", label="X"):
    return c.post(f"/picker/person/{pid}/bind",
                  data={"candidate_id": cid, "source": source, "label": label},
                  headers={"X-Requested-With": "fetch"})


def test_e2e_on_bind_merges_into_existing(tmp_path):
    app = _web(tmp_path)
    r = _seed_web_person(app, "Established", ext="bdr:P64", status="verified", with_edge=True)
    fresh = _seed_web_person(app, "Variant")          # provisional, unbound
    with app.test_client() as c:
        j = _bind(c, fresh, "bdr:P64").get_json()      # bdr id → no network
    assert j["ok"] and j["dedup"]["merged_into"] == r
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM person WHERE external_id='bdr:P64'").fetchone()[0] == 1
    assert db.execute("SELECT 1 FROM person WHERE id=?", (fresh,)).fetchone() is None


def test_e2e_on_bind_suggests_on_multiple(tmp_path):
    app = _web(tmp_path)
    a = _seed_web_person(app, "A", ext="wikidata:Q1", status="verified")
    b = _seed_web_person(app, "B", ext="wikidata:Q2", status="verified")
    db = connect(app.config["DB_PATH"])
    for p in (a, b):
        db.execute("INSERT INTO person_external_id (person_id, scheme, value) VALUES (?, 'bdrc', 'bdr:Pbad')", (p,))
    db.commit(); db.close()
    fresh = _seed_web_person(app, "C")
    with app.test_client() as c:
        j = _bind(c, fresh, "bdr:Pbad").get_json()
    assert j["ok"] and sorted(j["dedup"]["suggest"]) == sorted([a, b])
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT 1 FROM person WHERE id=?", (fresh,)).fetchone() is not None  # not merged


def test_e2e_network_loss_mid_bind(tmp_path, monkeypatch):
    app = _web(tmp_path)
    r = _seed_web_person(app, "Established", ext="wikidata:Qx", status="verified")
    fresh = _seed_web_person(app, "Variant")
    # simulate Wikidata being unreachable during the harvest
    class _Dead:
        def entity(self, qid):
            raise AuthorityUnavailable("down")
    monkeypatch.setattr(verify, "_wikidata_client", lambda: _Dead())
    with app.test_client() as c:
        j = _bind(c, fresh, "wikidata:Qx", source="wikidata").get_json()
    # bound, but NOT merged: incomplete harvest → suggest only
    assert j["ok"] and j["dedup"]["suggest"] == [r] and "incomplete" in j["dedup"]["reason"]
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT harvest_incomplete FROM person WHERE id=?", (fresh,)).fetchone()[0] == 1
    assert db.execute("SELECT 1 FROM person WHERE id=?", (fresh,)).fetchone() is not None
    labels = {w["check"] for w in check_integrity(db)["warnings"]}
    assert "person bound but harvest incomplete (re-harvest pending)" in labels


# ── CLI parity: the same on-bind dedup runs in the interactive picker ───────────
class _FakeProvider:
    """Offline candidate source (no network)."""
    name = "bdrc"

    def __init__(self, cands):
        self._cands = cands

    def candidates(self, db, text, aliases=()):
        return list(self._cands)


def test_bind_with_dedup_helper_merges(tmp_path):
    # The shared entry point used by BOTH the web route and the CLI.
    from catalogue.services import picker
    db = init_db(tmp_path / "h.db")
    r = _person(db, "Established", ext="bdr:P64", status="verified")
    _xid(db, r, "bdrc", "bdr:P64")
    w = _work(db, "W"); _author(db, w, r)
    fresh = _person(db, "Variant")
    res = picker.bind_with_dedup(db, "person", fresh, picker.Candidate("bdr:P64", "bdrc", "Variant"))
    assert res["ok"] and res["dedup"]["merged_into"] == r
    assert db.execute("SELECT 1 FROM person WHERE id=?", (fresh,)).fetchone() is None


def test_run_cli_dedupes_on_bind(tmp_path):
    # End-to-end through the interactive CLI loop: binding a fresh row to an id an
    # established record already holds auto-merges it (web-UI behaviour, in the CLI).
    from catalogue.services import picker
    db = init_db(tmp_path / "cli.db")
    r = _person(db, "Established", ext="bdr:P64", status="verified")
    _xid(db, r, "bdrc", "bdr:P64")
    w = _work(db, "W"); _author(db, w, r)            # r is the richer record → survivor
    fresh = _person(db, "Variant")
    provs = [_FakeProvider([picker.Candidate("bdr:P64", "bdrc", "Variant")])]  # bdr → offline
    msgs = []
    tally = picker.run_cli(db, "person", ids=[fresh], providers=provs,
                           input_fn=lambda p: "1",
                           out=lambda *a: msgs.append(" ".join(str(x) for x in a)))
    assert tally["bound"] == 1 and tally["merged"] == 1
    assert db.execute("SELECT 1 FROM person WHERE id=?", (fresh,)).fetchone() is None
    assert any(f"MERGED into #{r}" in m for m in msgs)


# ── report (which record merged into which) ─────────────────────────────────────
def test_dedup_report_preview_then_applied(tmp_path):
    db = init_db(tmp_path / "rep.db")
    ids = [_person(db, f"Tsongkhapa v{i}", ext="wikidata:Q1") for i in range(3)]
    db.commit()
    plan = PD.plan_batch(db)
    rep = PD.dedup_report(plan)                       # preview
    assert rep["applied"] is False and len(rep["merges"]) == 1
    m = rep["merges"][0]
    assert m["into"] in ids and len(m["absorbed"]) == 2
    assert all(a["name"].startswith("Tsongkhapa") for a in m["absorbed"])   # names, not ids
    assert all("wikidata:Q1" in a["via"] for a in m["absorbed"])            # the linking key
    assert all(a["works"] is None for a in m["absorbed"])                   # no counts pre-apply
    res = PD.apply_batch(db, plan, commit=True)
    rep2 = PD.dedup_report(plan, res)                 # applied
    assert rep2["applied"] is True and rep2["merged_rows"] == 2
    assert all(a["works"] is not None for a in rep2["merges"][0]["absorbed"])
    survivors = {r[0] for r in db.execute("SELECT id FROM person")}
    assert survivors == {m["into"]}                  # only the canonical remains


def test_report_lines_is_human_readable(tmp_path):
    db = init_db(tmp_path / "rl.db")
    for n in ("Alpha", "Beta"):
        _person(db, n, ext="wikidata:Q1")
    db.commit()
    txt = PD.report_lines(PD.dedup_report(PD.plan_batch(db)))
    assert "Would merge" in txt and "absorbs" in txt
    assert ("Alpha" in txt and "Beta" in txt)


def test_web_dedupe_preview_is_dry_run_then_apply_merges(tmp_path):
    dbp = tmp_path / "ddweb.db"
    db = init_db(dbp)
    for i in range(3):
        _person(db, f"Tsongkhapa v{i}", ext="wikidata:Q1")
    db.commit()
    app = create_app(str(dbp))
    c = app.test_client()
    # the picker page links to the dedupe action
    assert b"/picker/person/dedupe" in c.get("/picker/person").data
    # preview: shows the plan + Apply, writes nothing
    pg = c.get("/picker/person/dedupe")
    assert pg.status_code == 200 and b"Proposed merges" in pg.data and b"Apply" in pg.data
    assert init_db(dbp).execute("SELECT COUNT(*) FROM person").fetchone()[0] == 3
    # apply: collapses the three into one, report says so
    r = c.post("/picker/person/dedupe")
    assert r.status_code == 200 and b"Merged" in r.data
    assert init_db(dbp).execute("SELECT COUNT(*) FROM person").fetchone()[0] == 1


# ── atomic, integrity-verified merge/dedupe (links MUST move, else roll back) ──────
def test_verified_commit_rolls_back_a_dangling_ref(tmp_path):
    from catalogue.db_store.integrity import verified_commit, IntegrityError
    db = init_db(tmp_path / "vc.db"); db.commit()
    # FK is ON (would reject the orphan at insert) — drop it just to plant a dangling
    # row, then let verified_commit's referential scan catch it and roll back.
    db.execute("PRAGMA foreign_keys=OFF")
    w = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?, 999, 'author')", (w,))
    with pytest.raises(IntegrityError):
        verified_commit(db)                          # 999 has no person row → dangling
    assert db.execute("SELECT COUNT(*) FROM work_author WHERE person_id=999").fetchone()[0] == 0


def test_verified_commit_commits_when_clean(tmp_path):
    import sqlite3
    from catalogue.db_store.integrity import verified_commit
    dbp = tmp_path / "vk.db"; db = init_db(dbp)
    db.execute("INSERT INTO person (primary_name) VALUES ('X')")
    verified_commit(db)                              # clean → persists
    assert sqlite3.connect(dbp).execute("SELECT COUNT(*) FROM person").fetchone()[0] == 1


def test_merge_rolls_back_when_links_not_moved(tmp_path, monkeypatch):
    """A merge that deletes the loser without re-pointing its edge LOSES the link
    (work_author is ON DELETE CASCADE, so it vanishes — it does NOT dangle). The
    edge-preservation post-condition must catch that and roll the whole merge back,
    leaving the loser and its authorship intact."""
    from catalogue.access_api.persons import store as person_store
    from catalogue.db_store.integrity import IntegrityError, check_integrity
    db = init_db(tmp_path / "mr.db")
    a = _person(db, "Dupe"); b = _person(db, "Canon")
    w = _work(db, "W"); _author(db, w, a)            # a authors w
    db.commit()
    # a BROKEN engine merge: delete the loser, never re-point — cascade silently drops its edge.
    # apply_merge's _assert_links_moved post-condition must still catch it and roll back.
    monkeypatch.setattr(person_store.SqlitePersonStore, "merge",
                        lambda self, loser_id, winner_id, keep_name_alias=True:
                        self._a.rw.execute("DELETE FROM person WHERE id=?", (loser_id,)))
    with pytest.raises(IntegrityError):
        CE.apply_merge(db, a, b)
    assert db.execute("SELECT 1 FROM person WHERE id=?", (a,)).fetchone()          # loser survives
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w,)).fetchone()[0] == a
    assert check_integrity(db)["ok"]                 # DB left consistent


def test_clean_merge_still_succeeds_and_moves_the_link(tmp_path):
    """Regression: the real merge passes the new check — the link moves to the canon."""
    db = init_db(tmp_path / "ok.db")
    a = _person(db, "Dupe"); b = _person(db, "Canon")
    w = _work(db, "W"); _author(db, w, a)
    db.commit()
    rep = CE.apply_merge(db, a, b)
    assert rep["merged"] == a and rep["into"] == b
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w,)).fetchone()[0] == b
    assert db.execute("SELECT 1 FROM person WHERE id=?", (a,)).fetchone() is None


def test_apply_batch_rolls_back_whole_batch_on_integrity_failure(tmp_path, monkeypatch):
    from catalogue.access_api.persons import store as person_store
    from catalogue.db_store.integrity import IntegrityError
    db = init_db(tmp_path / "bb.db")
    canon = _person(db, "Canon", ext="wikidata:Q1")
    dup = _person(db, "Dupe", ext="wikidata:Q1")
    for t in ("c0", "c1"):                           # canon has MORE edges → stays canonical
        _author(db, _work(db, t), canon)
    wd = _work(db, "dupwork"); _author(db, wd, dup)  # dup authors a work a broken merge would lose
    db.commit()
    monkeypatch.setattr(person_store.SqlitePersonStore, "merge",
                        lambda self, loser_id, winner_id, keep_name_alias=True:
                        self._a.rw.execute("DELETE FROM person WHERE id=?", (loser_id,)))
    plan = PD.plan_batch(db)
    with pytest.raises(IntegrityError):
        PD.apply_batch(db, plan, commit=True)
    # the ENTIRE batch rolled back — both persons AND the dup's authorship survive
    assert db.execute("SELECT COUNT(*) FROM person WHERE id IN (?,?)", (canon, dup)).fetchone()[0] == 2
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (wd,)).fetchone()[0] == dup
