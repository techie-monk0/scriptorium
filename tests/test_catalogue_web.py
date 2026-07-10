"""Black-box HTTP tests for the catalogue browse/search + review surfaces."""
from __future__ import annotations

import pytest

from catalogue.db_store import add_alias, connect, init_db
from catalogue.webui.web import create_app


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    with app.test_client() as c:
        yield c, app


def _seed(app):
    db = connect(app.config["DB_PATH"])
    eid = db.execute("INSERT INTO edition (title) VALUES ('Bodhicaryāvatāra')").lastrowid
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", wid, "Bodhicaryāvatāra", "english")
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Śāntideva')").lastrowid
    add_alias(db, "person", pid, "Śāntideva", "english")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?, ?, 'author')",
               (wid, pid))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)",
               (eid, wid))
    db.commit(); db.close()
    return eid


# NOTE: the standalone /catalogue browse + A–Z pages and the unified `/find`
# ("Browse") surface were removed; the diacritic-fold search behaviour they
# exercised is pinned here against the surviving domain `aggregate_search` (the
# grouped search the Search page is built on). See tests/system/DELETIONS.md.

def _find_labels(app, q):
    """All hit labels + sublabels from the grouped search (the diacritic-folded
    data the Search surface renders)."""
    from catalogue.services import search as SEARCH
    db = connect(app.config["DB_PATH"])
    try:
        doc = SEARCH.aggregate_search(db, q)
    finally:
        db.close()
    return " ".join(h["label"] + " " + h.get("sublabel", "")
                    for g in doc["groups"] for h in g["hits"])


def test_search_by_author_diacritic_insensitive(env):
    c, app = env
    _seed(app)
    # Query WITHOUT diacritics must still match the IAST-stored person name
    # (the People group of grouped search).
    assert "Śāntideva" in _find_labels(app, "Santideva")


def test_search_by_work_title(env):
    c, app = env
    _seed(app)
    # The Works group matches any alias on the folded key.
    assert "Bodhicaryāvatāra" in _find_labels(app, "bodhicary")


def test_works_card_renders_and_saves(env):
    c, app = env
    eid = _seed(app)
    r = c.get(f"/edition/{eid}/works")
    assert r.status_code == 200
    assert b'name="w0_title"' in r.data
    # Toggle the single contributor author→translator and save.
    db = connect(app.config["DB_PATH"])
    wid = db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (eid,)).fetchone()[0]
    pid = db.execute("SELECT person_id FROM work_author WHERE work_id=?", (wid,)).fetchone()[0]
    db.close()
    c.post(f"/edition/{eid}/works", data={
        "structure": "", "w0_included": "on", "w0_work_id": str(wid),
        "w0_title": "Bodhicaryāvatāra", "w0_c0_name": "Śāntideva",
        "w0_c0_role": "translator", f"w0_c0_pid": str(pid)})
    db = connect(app.config["DB_PATH"])
    # toggled author→translator: no longer an author; now the edition's translator
    assert db.execute("SELECT COUNT(*) FROM work_author WHERE work_id=?", (wid,)).fetchone()[0] == 0
    assert db.execute("SELECT person_id FROM edition_translator WHERE edition_id=?",
                      (eid,)).fetchone()[0] == pid
    db.close()


def test_verdict_card_saves(env):
    c, app = env
    eid = _seed(app)
    c.post(f"/edition/{eid}/review-card", data={
        "status": "ok", "f_title": "on", "f_contributors": "on", "note": "looks good"})
    db = connect(app.config["DB_PATH"])
    rs, rf, note = db.execute(
        "SELECT review_status, review_flags, review_note FROM edition WHERE id=?",
        (eid,)).fetchone()
    db.close()
    import json
    assert rs == "ok"
    assert json.loads(rf)["title"] is True
    assert note == "looks good"


def test_picker_person_surfaces_aliases_and_add_form(env):
    c, app = env
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Tsongkhapa')").lastrowid
    add_alias(db, "person", pid, "Tsongkhapa", "english")
    add_alias(db, "person", pid, "Je Rinpoche", "english")
    db.commit(); db.close()
    r = c.get("/picker/person")
    assert r.status_code == 200
    assert b"known aliases" in r.data
    assert b"Je Rinpoche" in r.data
    assert f"/picker/person/{pid}/alias".encode() in r.data  # add-alias form
    # the persons-review intro blurb
    assert b"Review &amp; correct persons here" in r.data


def test_picker_person_add_and_remove_alias(env):
    c, app = env
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Lobsang')").lastrowid
    add_alias(db, "person", pid, "Lobsang", "english")
    db.commit(); db.close()
    # add
    c.post(f"/picker/person/{pid}/alias", data={"text": "Lozang", "scheme": "phonetic"})
    db = connect(app.config["DB_PATH"])
    aliases = {r[0]: r[1] for r in db.execute(
        "SELECT text, id FROM person_alias WHERE person_id=?", (pid,)).fetchall()}
    db.close()
    assert "Lozang" in aliases
    # remove it
    c.post(f"/picker/person/{pid}/alias/{aliases['Lozang']}/delete")
    db = connect(app.config["DB_PATH"])
    remaining = [r[0] for r in db.execute(
        "SELECT text FROM person_alias WHERE person_id=?", (pid,)).fetchall()]
    db.close()
    assert "Lozang" not in remaining and "Lobsang" in remaining


def test_picker_shows_n_of_m_total(env):
    """The list is capped (limit) but the header shows N of the TOTAL unresolved, so
    'finished but reload shows more' is visible. Generic over kind (works too)."""
    from catalogue.services import picker as P
    c, app = env
    db = connect(app.config["DB_PATH"])
    for n in ("A", "B", "C"):
        pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                         "VALUES (?, 'provisional')", (n,)).lastrowid
        add_alias(db, "person", pid, n, "english")
    db.commit()
    assert P.count_unresolved(db, "person") == 3
    db.close()
    r = c.get("/picker/person?limit=2")                    # truncated
    assert b"2 of 3" in r.data and b"load all 3" in r.data
    assert b"3 of 3" in c.get("/picker/person").data        # default limit ≥ 3 → all


def test_picker_person_rename(env):
    c, app = env
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Atifa')").lastrowid
    add_alias(db, "person", pid, "Atifa", "english")
    db.commit(); db.close()
    # The pencil edit affordance is on the page.
    assert b'class="rename-btn"' in c.get("/picker/person").data
    # Rename → primary_name updated; new spelling seeded as an alias; old kept.
    r = c.post(f"/picker/person/{pid}/rename", data={"primary_name": "Atiśa"},
               headers={"X-Requested-With": "fetch"})
    assert r.get_json() == {"ok": True, "name": "Atiśa"}
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT primary_name FROM person WHERE id=?", (pid,)).fetchone()[0] == "Atiśa"
    aliases = [r[0] for r in db.execute(
        "SELECT text FROM person_alias WHERE person_id=?", (pid,)).fetchall()]
    db.close()
    assert "Atiśa" in aliases and "Atifa" in aliases     # old name never lost
    # Idempotent: renaming to the same name doesn't pile duplicate aliases.
    c.post(f"/picker/person/{pid}/rename", data={"primary_name": "Atiśa"})
    db = connect(app.config["DB_PATH"])
    assert [a for a in db.execute("SELECT text FROM person_alias WHERE person_id=?",
                                  (pid,)).fetchall()].count(("Atiśa",)) == 1
    db.close()
    assert c.post(f"/picker/person/{pid}/rename", data={"primary_name": "  "}).status_code == 400


def test_picker_person_rebind_requires_force(env, monkeypatch):
    import catalogue.services.picker as pk
    monkeypatch.setattr(pk, "_harvest_extra", lambda ext: (None, None, {}))  # no network
    c, app = env
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('X','provisional')").lastrowid
    db.execute("INSERT INTO person_external_id (person_id, scheme, value) "
               "VALUES (?, 'bdrc', 'bdr:OLD')", (pid,))
    db.commit(); db.close()

    def bind(cid, force=False):
        data = {"candidate_id": cid, "source": "wikidata", "label": "X"}
        if force:
            data["force"] = "1"
        return c.post(f"/picker/person/{pid}/bind", data=data,
                      headers={"X-Requested-With": "fetch"}).get_json()

    assert bind("wikidata:Q1")["ok"] is True
    # A second bind is refused (one-shot) but surfaces the current id so the UI
    # can offer a rebind.
    blocked = bind("wikidata:Q2")
    assert blocked["ok"] is False and blocked["already_bound"] is True
    assert blocked["current"] == "wikidata:Q1"
    # Forced rebind overwrites and drops the stale authority's harvested cross-links.
    assert bind("wikidata:Q2", force=True)["ok"] is True
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] == "wikidata:Q2"
    assert db.execute("SELECT COUNT(*) FROM person_external_id WHERE person_id=? AND value='bdr:OLD'",
                      (pid,)).fetchone()[0] == 0
    db.close()


# ── Stale-record edge cases: ops on a person that was merged away / deleted ──
def _bind(c, pid, cid, force=False):
    data = {"candidate_id": cid, "source": "wikidata", "label": "X"}
    if force:
        data["force"] = "1"
    return c.post(f"/picker/person/{pid}/bind", data=data,
                  headers={"X-Requested-With": "fetch"}).get_json()


def _unbind(c, pid):
    return c.post(f"/picker/person/{pid}/unbind",
                  headers={"X-Requested-With": "fetch"}).get_json()


def _pcount(app):
    db = connect(app.config["DB_PATH"])
    try:
        return db.execute("SELECT COUNT(*) FROM person").fetchone()[0]
    finally:
        db.close()


def test_ops_after_merge_collapsed_two_records(env, monkeypatch):
    """Merge folds B into A (B is deleted). Operating on the gone loser is a safe
    no-op — never 500s, never resurrects it, never touches the survivor."""
    import catalogue.services.picker as pk
    monkeypatch.setattr(pk, "_harvest_extra", lambda ext: (None, None, {}))
    c, app = env
    db = connect(app.config["DB_PATH"])
    a = db.execute("INSERT INTO person (primary_name, verification_status) "
                   "VALUES ('Atisha','provisional')").lastrowid
    b = db.execute("INSERT INTO person (primary_name, verification_status) "
                   "VALUES ('Atifa','provisional')").lastrowid
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (wid, b))
    db.commit(); db.close()

    assert _bind(c, a, "wikidata:Q1")["ok"] is True
    # Fold B into A → B deleted, its work edge re-pointed to A.
    assert "error" not in c.post(f"/picker/person/{b}/merge", json={"into": a}).get_json()
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT 1 FROM person WHERE id=?", (b,)).fetchone() is None
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (wid,)).fetchone()[0] == a
    db.close()

    before = _pcount(app)
    # Every op on the gone loser B: graceful, no resurrection, A unaffected.
    assert _unbind(c, b)["ok"] is False
    gone_bind = _bind(c, b, "wikidata:Q9")
    assert gone_bind["ok"] is False and "already_bound" not in gone_bind   # not a bind, not a rebind
    assert c.post(f"/picker/person/{b}/rename", data={"primary_name": "Z"}).status_code == 404
    assert c.post(f"/picker/person/{b}/alias", data={"text": "Z"}).status_code == 404
    assert _pcount(app) == before                                         # B stayed gone
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT external_id FROM person WHERE id=?", (a,)).fetchone()[0] == "wikidata:Q1"
    db.close()

    # The survivor unbinds normally → back to provisional, work edge intact.
    assert _unbind(c, a)["ok"] is True
    db = connect(app.config["DB_PATH"])
    ext, vs = db.execute("SELECT external_id, verification_status FROM person WHERE id=?", (a,)).fetchone()
    assert ext is None and vs == "provisional"
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (wid,)).fetchone()[0] == a
    db.close()


def test_ops_after_delete(env, monkeypatch):
    """Bind a person, delete it, then every follow-up op fails gracefully and
    never recreates the row."""
    import catalogue.services.picker as pk
    monkeypatch.setattr(pk, "_harvest_extra", lambda ext: (None, None, {}))
    c, app = env
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('Solo','provisional')").lastrowid
    db.commit(); db.close()
    assert _bind(c, pid, "wikidata:Q1")["ok"] is True
    assert c.post(f"/picker/person/{pid}/delete").get_json()["deleted"] == pid
    before = _pcount(app)
    assert _unbind(c, pid)["ok"] is False
    assert _bind(c, pid, "wikidata:Q2")["ok"] is False
    assert c.post(f"/picker/person/{pid}/rename", data={"primary_name": "Z"}).status_code == 404
    assert c.post(f"/picker/person/{pid}/alias", data={"text": "Z"}).status_code == 404
    assert c.post(f"/picker/person/{pid}/local",
                  headers={"X-Requested-With": "fetch"}).get_json()["ok"] is False
    assert _pcount(app) == before                                         # never resurrected


def test_merge_from_already_merged_source_explains_itself(env):
    """Repro of the 'no such person #N' confusion: merge P into A (P is deleted),
    then a SECOND merge from the gone P must return a message that names it as the
    source record — not a bare id the operator can't place."""
    c, app = env
    db = connect(app.config["DB_PATH"])
    p = db.execute("INSERT INTO person (primary_name) VALUES ('P')").lastrowid
    a = db.execute("INSERT INTO person (primary_name) VALUES ('A')").lastrowid
    b = db.execute("INSERT INTO person (primary_name) VALUES ('B')").lastrowid
    db.commit(); db.close()
    assert "error" not in c.post(f"/picker/person/{p}/merge", json={"into": a}).get_json()
    j = c.post(f"/picker/person/{p}/merge", json={"into": b}).get_json()   # P is gone now
    assert "error" in j
    assert "merging from" in j["error"] and str(p) in j["error"]           # names the source
    assert "merged or deleted" in j["error"]


def test_merge_keeps_or_drops_dup_name_as_alias(env):
    """The merge checkbox: keep_name_alias=True (default) guarantees the merged-away
    name survives as an alias of the winner even when it wasn't already an alias row
    (the silent-name-loss bug); =False ensures it isn't kept."""
    c, app = env

    def names_after(keep):
        db = connect(app.config["DB_PATH"])
        # dup has NO alias for its primary_name (the gap case)
        dup = db.execute("INSERT INTO person (primary_name) VALUES ('Lonely Name')").lastrowid
        canon = db.execute("INSERT INTO person (primary_name) VALUES ('Canon')").lastrowid
        add_alias(db, "person", canon, "Canon", "english")
        db.commit(); db.close()
        body = {"into": canon} if keep else {"into": canon, "keep_name_alias": False}
        assert "error" not in c.post(f"/picker/person/{dup}/merge", json=body).get_json()
        db = connect(app.config["DB_PATH"])
        al = [r[0] for r in db.execute(
            "SELECT text FROM person_alias WHERE person_id=?", (canon,)).fetchall()]
        db.close()
        return al

    assert "Lonely Name" in names_after(keep=True)        # default: name preserved
    assert "Lonely Name" not in names_after(keep=False)    # unchecked: name dropped


def test_picker_page_has_merge_keep_name_checkbox(env):
    # The checkbox is wired in the merge confirm JS on the page.
    assert b'id="keepname"' in env[0].get("/picker/person").data


def test_merge_into_deleted_target_is_graceful(env):
    """Merging into a target that was deleted returns an error, not a 500, and
    leaves the source intact."""
    c, app = env
    db = connect(app.config["DB_PATH"])
    src = db.execute("INSERT INTO person (primary_name) VALUES ('Src')").lastrowid
    tgt = db.execute("INSERT INTO person (primary_name) VALUES ('Tgt')").lastrowid
    db.commit(); db.close()
    assert c.post(f"/picker/person/{tgt}/delete").get_json()["deleted"] == tgt
    j = c.post(f"/picker/person/{src}/merge", json={"into": tgt}).get_json()
    assert "error" in j
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT 1 FROM person WHERE id=?", (src,)).fetchone() is not None  # src survives
    db.close()


def test_unbind_returns_person_to_worklist(env, monkeypatch):
    """After unbind the person is provisional + external_id NULL, so it reappears
    in the picker's unresolved worklist (and can be re-bound)."""
    import catalogue.services.picker as pk
    monkeypatch.setattr(pk, "_harvest_extra", lambda ext: (None, None, {}))
    c, app = env
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('W','provisional')").lastrowid
    db.commit(); db.close()
    assert _bind(c, pid, "wikidata:Q1")["ok"] is True
    db = connect(app.config["DB_PATH"])
    assert pid not in [r[0] for r in pk.unresolved(db, "person")]          # bound → off worklist
    db.close()
    assert _unbind(c, pid)["ok"] is True
    db = connect(app.config["DB_PATH"])
    assert pid in [r[0] for r in pk.unresolved(db, "person")]              # unbound → back on
    assert _bind(c, pid, "wikidata:Q2")["ok"] is True                      # and re-bindable
    db.close()


def test_sandbox_promote_via_web_swaps_in(tmp_path):
    from catalogue.services import sandbox as sb
    live = str(tmp_path / "live.db")
    init_db(live).close()
    sb.fork(live)
    sbpath = sb.sandbox_path(live)
    # edit the sandbox copy: add an edition
    db = connect(sbpath)
    db.execute("INSERT INTO edition (title) VALUES ('Sandbox Edit')")
    db.commit(); db.close()

    app = create_app(sbpath, ingest_verify=False)
    app.testing = True
    with app.test_client() as c:
        r = c.post("/sandbox/promote")
        assert r.status_code == 200
        assert b"Promoted to live" in r.data
    # live now carries the sandbox edit; backup exists; sandbox consumed
    db = connect(live)
    assert db.execute("SELECT count(*) FROM edition WHERE title='Sandbox Edit'").fetchone()[0] == 1
    db.close()
    import glob
    assert glob.glob(live + ".pre-swap-*.bak")
    assert not __import__("os").path.exists(sbpath)


def test_sandbox_banner_only_when_sandbox(env, tmp_path):
    c, app = env
    # default db isn't a .sandbox → no banner
    assert b"SANDBOX" not in c.get("/review").data
    # a .sandbox path app shows the banner + promote/discard
    sb_app = create_app(str(tmp_path / "x.db.sandbox"), ingest_verify=False)
    sb_app.testing = True
    with sb_app.test_client() as sc:
        assert b"SANDBOX" in sc.get("/library").data
        assert b"/sandbox/promote" in sc.get("/library").data
