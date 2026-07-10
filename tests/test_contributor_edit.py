"""Tests for contributor split/delete (catalogue/contributor_edit.py) + picker wiring."""
from __future__ import annotations

from catalogue.services import contributor_edit as CE
from catalogue.services import contributor_undo as U
from catalogue.services import picker
from catalogue.db_store import add_alias, fold_key, init_db


def _person(db, name, role_hint=None):
    pid = db.execute("INSERT INTO person (primary_name, role_hint) VALUES (?, ?)",
                     (name, role_hint)).lastrowid
    add_alias(db, "person", pid, name, "english")
    return pid


def _work(db, title):
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, ?, 'english', ?)", (wid, title, fold_key(title)))
    return wid


def _seed_blob(db):
    """The real case: blob author + the two clean halves already existing, with the
    work sitting in a real book (edition + holding) so the plan can name it."""
    jk = _person(db, "Jamgön Kongtrul Lodrö Taye")
    org = _person(db, "Kalu Rinpoche Translation Group", "translator")
    blob = _person(db, "Jamgon Kongtrul Lodro Taye, Kalu Rinpoche Translation Group", "author")
    w = _work(db, "Buddhist Ethics")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,?)",
               (w, blob, "author"))
    eid = db.execute("INSERT INTO edition (title) VALUES ('Buddhist Ethics (1998)')").lastrowid
    db.execute("INSERT INTO holding (edition_id, file_path) "
               "VALUES (?, '/books/BuddhistEthics.pdf')", (eid,))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,0)",
               (eid, w))
    db.commit()
    return jk, org, blob, w


def test_plan_split_maps_parts_to_existing_rows(tmp_path):
    db = init_db(tmp_path / "s.db")
    jk, org, blob, w = _seed_blob(db)
    plan = CE.plan_split(db, blob)
    names = {p["name"]: p["existing_id"] for p in plan["parts"]}
    # fold-key reuse: both parts map onto the already-present clean rows
    assert names["Jamgon Kongtrul Lodro Taye"] == jk
    assert names["Kalu Rinpoche Translation Group"] == org
    assert [wk["work_id"] for wk in plan["works"]] == [w]
    # the plan names the work AND the book it lives in, and a suggested role
    assert plan["works"][0]["label"] == "Buddhist Ethics"
    assert plan["works"][0]["books"][0]["book"] == "Buddhist Ethics (1998)"
    assert {p["role"] for p in plan["parts"]} == {"author"}      # suggested from the edge


def test_apply_split_assigns_per_part_roles(tmp_path):
    db = init_db(tmp_path / "roles.db")
    jk, org, blob, w = _seed_blob(db)
    CE.apply_split(db, blob, assignments=[
        {"name": "Jamgon Kongtrul Lodro Taye", "role": "author"},
        {"name": "Kalu Rinpoche Translation Group", "role": "translator"}])
    # author on the work, translator on the work's edition (FRBR homes)
    auth = {r[0] for r in db.execute(
        "SELECT person_id FROM work_author WHERE work_id=?", (w,))}
    trans = {r[0] for r in db.execute(
        "SELECT et.person_id FROM edition_translator et "
        "JOIN edition_work ew ON ew.edition_id=et.edition_id WHERE ew.work_id=?", (w,))}
    assert auth == {jk} and trans == {org}


def test_apply_split_repoints_and_removes_blob(tmp_path):
    db = init_db(tmp_path / "s2.db")
    jk, org, blob, w = _seed_blob(db)
    rep = CE.apply_split(db, blob)
    # blob gone
    assert db.execute("SELECT 1 FROM person WHERE id=?", (blob,)).fetchone() is None
    # both halves now author the work (role inherited), no new rows created
    authors = {r[0] for r in db.execute(
        "SELECT person_id FROM work_author WHERE work_id=? AND role='author'", (w,))}
    assert authors == {jk, org}
    assert rep["created"] == []                       # both reused existing rows


def test_apply_split_creates_missing_part(tmp_path):
    db = init_db(tmp_path / "s3.db")
    blob = _person(db, "Alice Example, Bob Sample", "author")
    w = _work(db, "Some Book")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,?)",
               (w, blob, "author"))
    db.commit()
    rep = CE.apply_split(db, blob)
    assert len(rep["created"]) == 2                   # neither existed before
    authors = {db.execute("SELECT primary_name FROM person WHERE id=?", (a,)).fetchone()[0]
               for a in (r[0] for r in db.execute(
                   "SELECT person_id FROM work_author WHERE work_id=?", (w,)))}
    assert authors == {"Alice Example", "Bob Sample"}


def test_apply_delete_tombstones_and_hides(tmp_path):
    db = init_db(tmp_path / "d.db")
    pid = _person(db, "Junk Author", "author")
    w = _work(db, "Orphaned Work")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,?)",
               (w, pid, "author"))
    db.commit()
    rep = CE.apply_delete(db, pid)
    assert [wk["label"] for wk in rep["works_detached"]] == ["Orphaned Work"]
    # SOFT delete: the row survives but is flagged + hidden from live reads
    assert db.execute("SELECT deleted_at FROM person WHERE id=?", (pid,)).fetchone()[0] is not None
    assert db.execute("SELECT 1 FROM v_live_person WHERE id=?", (pid,)).fetchone() is None
    # the edge RIDES the tombstone (nothing purged) — so restore can make the work whole again
    assert db.execute("SELECT 1 FROM work_author WHERE person_id=?", (pid,)).fetchone() is not None
    # the work itself survives
    assert db.execute("SELECT 1 FROM work WHERE id=?", (w,)).fetchone() is not None


def test_plan_split_rejects_non_comma_name(tmp_path):
    db = init_db(tmp_path / "n.db")
    pid = _person(db, "Solo Name")
    assert "error" in CE.plan_split(db, pid)


# ── CLI integration: x splits, d deletes ──────────────────────────────────────
def test_cli_split_action(tmp_path):
    db = init_db(tmp_path / "cli.db")
    jk, org, blob, w = _seed_blob(db)
    # make the blob the only unresolved row
    db.execute("UPDATE person SET external_id='wikidata:Q1', verification_status='verified' "
               "WHERE id IN (?,?)", (jk, org))
    db.commit()
    # x=split, then a role per part (blank=author default, t=translator), then confirm
    answers = iter(["x", "", "t", "y"])
    tally = picker.run_cli(db, "person", ids=[blob], providers=[],
                           input_fn=lambda p: next(answers), out=lambda *a: None)
    assert tally["edited"] == 1
    assert db.execute("SELECT 1 FROM person WHERE id=?", (blob,)).fetchone() is None
    auth = {r[0] for r in db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w,))}
    trans = {r[0] for r in db.execute(
        "SELECT et.person_id FROM edition_translator et "
        "JOIN edition_work ew ON ew.edition_id=et.edition_id WHERE ew.work_id=?", (w,))}
    assert auth == {jk} and trans == {org}


def test_cli_delete_action(tmp_path):
    db = init_db(tmp_path / "cli2.db")
    pid = _person(db, "Junk", "author")
    answers = iter(["d", "y"])
    picker.run_cli(db, "person", ids=[pid], providers=[],
                   input_fn=lambda p: next(answers), out=lambda *a: None)
    assert db.execute("SELECT 1 FROM v_live_person WHERE id=?", (pid,)).fetchone() is None
    assert db.execute("SELECT deleted_at FROM person WHERE id=?", (pid,)).fetchone()[0] is not None


# ── web routes ─────────────────────────────────────────────────────────────────
def test_web_split_and_delete_routes(tmp_path):
    from catalogue.webui.web import create_app
    dbp = tmp_path / "web.db"
    db = init_db(dbp)
    jk, org, blob, w = _seed_blob(db)
    c = create_app(str(dbp)).test_client()

    plan = c.get(f"/picker/person/{blob}/split").get_json()
    assert {p["name"] for p in plan["parts"]} == {
        "Jamgon Kongtrul Lodro Taye", "Kalu Rinpoche Translation Group"}
    assert plan["works"][0]["books"][0]["book"] == "Buddhist Ethics (1998)"
    # apply with per-part roles via JSON body
    rep = c.post(f"/picker/person/{blob}/split", json={"assignments": [
        {"name": "Jamgon Kongtrul Lodro Taye", "role": "author"},
        {"name": "Kalu Rinpoche Translation Group", "role": "translator"}]}).get_json()
    assert rep["split"] == blob and len(rep["into"]) == 2
    assert db.execute("SELECT 1 FROM person WHERE id=?", (blob,)).fetchone() is None
    auth = {r[0] for r in db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w,))}
    trans = {r[0] for r in db.execute(
        "SELECT et.person_id FROM edition_translator et "
        "JOIN edition_work ew ON ew.edition_id=et.edition_id WHERE ew.work_id=?", (w,))}
    assert auth == {jk} and trans == {org}

    pid = _person(db, "ToDelete", "author"); db.commit()
    assert c.get(f"/picker/person/{pid}/delete").get_json()["pid"] == pid
    assert c.post(f"/picker/person/{pid}/delete").get_json()["deleted"] == pid
    assert db.execute("SELECT 1 FROM v_live_person WHERE id=?", (pid,)).fetchone() is None


# ── MERGE ────────────────────────────────────────────────────────────────────────
def _seed_dup(db):
    """A duplicate ('Atifa', authored a work, has an alias + an external id) and the
    canonical row ('Atiśa') it should fold into."""
    canon = _person(db, "Atiśa")
    dup = _person(db, "Atifa", "author")
    add_alias(db, "person", dup, "Atifa Dipankara", "english")
    db.execute("UPDATE person SET external_id = 'bdr:P320150' WHERE id = ?", (dup,))
    db.execute("INSERT INTO person_external_id (person_id, scheme, value) "
               "VALUES (?, 'viaf', 'viaf:777')", (dup,))
    w = _work(db, "A Lamp for the Path")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')",
               (w, dup))
    eid = db.execute("INSERT INTO edition (title) VALUES ('Lamp (2012)')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence, translator_person_id) "
               "VALUES (?,?,0,?)", (eid, w, dup))
    db.commit()
    return canon, dup, w, eid


def test_plan_merge_previews_moves(tmp_path):
    db = init_db(tmp_path / "m.db")
    canon, dup, w, eid = _seed_dup(db)
    plan = CE.plan_merge(db, dup, canon)
    assert plan["dup"]["name"] == "Atifa" and plan["canon"]["name"] == "Atiśa"
    assert [wk["work_id"] for wk in plan["works"]] == [w]
    assert [e["edition_id"] for e in plan["editions"]] == [eid]
    assert "Atifa" in plan["aliases_gained"]            # dup's primary-name alias is new to canon
    assert plan["external_id_after"] == "bdr:P320150"   # canon had none → inherits dup's


def test_apply_merge_repoints_and_deletes_dup(tmp_path):
    db = init_db(tmp_path / "m2.db")
    canon, dup, w, eid = _seed_dup(db)
    rep = CE.apply_merge(db, dup, canon)
    assert rep["merged"] == dup and rep["into"] == canon
    # dup gone; its edges now point at canon
    assert db.execute("SELECT 1 FROM person WHERE id=?", (dup,)).fetchone() is None
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?",
                      (w,)).fetchone()[0] == canon
    assert db.execute("SELECT translator_person_id FROM edition_work WHERE edition_id=?",
                      (eid,)).fetchone()[0] == canon
    # aliases + external ids carried over
    keys = {a[0] for a in db.execute(
        "SELECT normalized_key FROM person_alias WHERE person_id=?", (canon,)).fetchall()}
    assert fold_key("Atifa") in keys and fold_key("Atifa Dipankara") in keys
    assert db.execute("SELECT external_id FROM person WHERE id=?", (canon,)).fetchone()[0] \
        == "bdr:P320150"
    assert ("viaf", "viaf:777") in db.execute(
        "SELECT scheme, value FROM person_external_id WHERE person_id=?", (canon,)).fetchall()


def test_merge_rejects_self_and_conflicting_authority(tmp_path):
    db = init_db(tmp_path / "m3.db")
    canon, dup, w, eid = _seed_dup(db)
    assert CE.plan_merge(db, dup, dup).get("error")          # self
    db.execute("UPDATE person SET external_id='bdr:P999' WHERE id=?", (canon,)); db.commit()
    assert "different authorities" in CE.apply_merge(db, dup, canon).get("error", "")


def test_cli_merge_action(tmp_path):
    db = init_db(tmp_path / "cli3.db")
    canon, dup, w, eid = _seed_dup(db)
    # search 'Atisa' → pick #1 → confirm
    answers = iter(["m", "Atisa", "1", "y"])
    picker.run_cli(db, "person", ids=[dup], providers=[],
                   input_fn=lambda p: next(answers), out=lambda *a: None)
    assert db.execute("SELECT 1 FROM person WHERE id=?", (dup,)).fetchone() is None
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?",
                      (w,)).fetchone()[0] == canon


def test_cli_add_alias_action(tmp_path):
    db = init_db(tmp_path / "cli4.db")
    pid = _person(db, "Panchen Lama")
    answers = iter(["a", "4th Panchen Lama", "english"])
    picker.run_cli(db, "person", ids=[pid], providers=[],
                   input_fn=lambda p: next(answers), out=lambda *a: None)
    keys = {a[0] for a in db.execute(
        "SELECT normalized_key FROM person_alias WHERE person_id=?", (pid,)).fetchall()}
    assert fold_key("4th Panchen Lama") in keys


# ── web routes: merge / alias / create / search ──────────────────────────────────
def test_web_merge_alias_create_search(tmp_path):
    from catalogue.webui.web import create_app
    dbp = tmp_path / "web2.db"
    db = init_db(dbp)
    canon, dup, w, eid = _seed_dup(db)
    c = create_app(str(dbp)).test_client()

    # search finds canon by a folded substring, excluding the dup
    matches = c.get(f"/picker/person/search?q=atisa&exclude={dup}").get_json()["matches"]
    assert any(m["id"] == canon for m in matches)

    # merge plan + apply (GET returns the plan: dup/canon brief; POST applies)
    assert c.get(f"/picker/person/{dup}/merge?into={canon}").get_json()["canon"]["id"] == canon
    rep = c.post(f"/picker/person/{dup}/merge", json={"into": canon}).get_json()
    assert rep["into"] == canon
    assert db.execute("SELECT 1 FROM person WHERE id=?", (dup,)).fetchone() is None

    # add alias to the survivor
    r = c.post(f"/picker/person/{canon}/alias",
               data={"text": "Jowo Je"}, headers={"X-Requested-With": "fetch"}).get_json()
    assert r["ok"]
    keys = {a[0] for a in db.execute(
        "SELECT normalized_key FROM person_alias WHERE person_id=?", (canon,)).fetchall()}
    assert fold_key("Jowo Je") in keys

    # create a brand-new person
    r = c.post("/picker/person/new",
               data={"primary_name": "Brand New Author"},
               headers={"X-Requested-With": "fetch"}).get_json()
    assert r["ok"] and r["name"] == "Brand New Author"
    assert db.execute("SELECT 1 FROM person WHERE id=?", (r["id"],)).fetchone()


# ── organization marking (picker) ────────────────────────────────────────────────
def test_cli_mark_org_action(tmp_path):
    db = init_db(tmp_path / "cli5.db")
    pid = _person(db, "Padmakara Translation Group", "translator")
    answers = iter(["o", "y"])
    picker.run_cli(db, "person", ids=[pid], providers=[],
                   input_fn=lambda p: next(answers), out=lambda *a: None)
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "organization"


# ── UNDO (reversible merge / delete / split) ─────────────────────────────────────
def test_undo_snapshot_covers_every_person_fk_table(tmp_path):
    """Maintenance guard for the generic snapshot: EVERY table with a foreign key to
    person(id) must be captured by contributor_undo, or an undo would silently drop
    those rows when it deletes-and-reinstates the involved persons. Derived from the
    live schema (PRAGMA foreign_key_list), so adding a new person-referencing table
    that nobody wired into _PERSON_TABLES fails here instead of corrupting an undo.
    edition_work is covered via its translator_person_id override (handled specially)."""
    db = init_db(tmp_path / "cov.db")
    covered = {t for t, _ in U._PERSON_TABLES} | {"edition_work"}
    referencing = set()
    for (tbl,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                             "AND name NOT LIKE 'sqlite_%'").fetchall():
        for fk in db.execute(f"PRAGMA foreign_key_list({tbl})").fetchall():
            if fk[2] == "person":                    # fk[2] = referenced table
                referencing.add(tbl)
    missing = referencing - covered
    assert not missing, (
        f"tables with a FK to person(id) NOT captured by the undo snapshot: "
        f"{sorted(missing)} — add them to contributor_undo._PERSON_TABLES (or handle "
        f"like the edition_work override), else undo will drop their rows.")


def _person_state(db, pids):
    """A comparable fingerprint of every row the contributor ops touch for the given
    person ids — used to assert undo restored the prior state EXACTLY."""
    ids = tuple(sorted(pids))
    ph = ",".join("?" * len(ids))

    def rows(sql):
        return sorted(map(tuple, db.execute(sql, ids).fetchall()))
    return {
        "person": rows(f"SELECT id, primary_name, role_hint, origin, dates, external_id, "
                       f"verification_status, harvest_incomplete FROM person "
                       f"WHERE id IN ({ph})"),
        "alias": rows(f"SELECT person_id, text, scheme, normalized_key FROM person_alias "
                      f"WHERE person_id IN ({ph})"),
        "ext": rows(f"SELECT person_id, scheme, value FROM person_external_id "
                    f"WHERE person_id IN ({ph})"),
        "work_author": rows(f"SELECT work_id, person_id, role FROM work_author "
                            f"WHERE person_id IN ({ph})"),
        "translator": rows(f"SELECT edition_id, person_id, seq FROM edition_translator "
                           f"WHERE person_id IN ({ph})"),
        "override": rows(f"SELECT edition_id, work_id, translator_person_id FROM edition_work "
                         f"WHERE translator_person_id IN ({ph})"),
    }


def test_undo_merge_restores_dup_and_survivor(tmp_path):
    db = init_db(tmp_path / "um.db")
    canon, dup, w, eid = _seed_dup(db)
    before = _person_state(db, [canon, dup])
    rep = CE.apply_merge(db, dup, canon, record_undo=True)
    assert rep["undo_token"] is not None
    assert db.execute("SELECT 1 FROM person WHERE id=?", (dup,)).fetchone() is None
    res = U.apply_undo(db, rep["undo_token"])
    assert set(res["person_ids"]) == {canon, dup}
    assert _person_state(db, [canon, dup]) == before        # byte-for-byte restoration
    # the dup, its work edge and translator override are all back where they were
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w,)).fetchone()[0] == dup
    assert db.execute("SELECT translator_person_id FROM edition_work WHERE edition_id=?",
                      (eid,)).fetchone()[0] == dup
    assert db.execute("SELECT external_id FROM person WHERE id=?", (canon,)).fetchone()[0] is None
    # token is consumed — a second undo is a no-op error, not a double-restore
    assert U.apply_undo(db, rep["undo_token"]).get("error")


def test_undo_delete_restores_person_and_edges(tmp_path):
    db = init_db(tmp_path / "ud.db")
    pid = _person(db, "Junk Author", "author")
    w = _work(db, "Orphaned Work")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w, pid))
    db.commit()
    before = _person_state(db, [pid])
    rep = CE.apply_delete(db, pid, record_undo=True)
    assert db.execute("SELECT 1 FROM v_live_person WHERE id=?", (pid,)).fetchone() is None  # tombstoned
    U.apply_undo(db, rep["undo_token"])
    assert _person_state(db, [pid]) == before
    assert db.execute("SELECT 1 FROM v_live_person WHERE id=?", (pid,)).fetchone()           # live again
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w,)).fetchone()[0] == pid


def test_undo_split_removes_created_parts_and_restores_blob(tmp_path):
    db = init_db(tmp_path / "us.db")
    blob = _person(db, "Alice Example, Bob Sample", "author")
    w = _work(db, "Some Book")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w, blob))
    db.commit()
    before = _person_state(db, [blob])
    rep = CE.apply_split(db, blob, record_undo=True)
    created = [c["id"] for c in rep["created"]]
    assert len(created) == 2 and db.execute("SELECT 1 FROM person WHERE id=?", (blob,)).fetchone() is None
    U.apply_undo(db, rep["undo_token"])
    assert _person_state(db, [blob]) == before
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w,)).fetchone()[0] == blob
    for c in created:                                        # op-created parts are gone again
        assert db.execute("SELECT 1 FROM person WHERE id=?", (c,)).fetchone() is None


def test_undo_split_restores_existing_targets(tmp_path):
    """Split into PRE-EXISTING persons: undo strips the edges it added to them and
    brings the blob back, WITHOUT deleting those persons."""
    db = init_db(tmp_path / "us2.db")
    jk, org, blob, w = _seed_blob(db)
    before = _person_state(db, [jk, org, blob])
    rep = CE.apply_split(db, blob, record_undo=True)
    assert rep["created"] == []
    U.apply_undo(db, rep["undo_token"])
    assert _person_state(db, [jk, org, blob]) == before
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w,)).fetchone()[0] == blob


def test_undo_refused_when_records_changed_since(tmp_path):
    """Guard: if either involved record is edited after the op, undo is refused (it
    would discard the newer edit) — and refusing leaves the op in place, undamaged."""
    db = init_db(tmp_path / "uguard.db")
    canon, dup, w, eid = _seed_dup(db)
    rep = CE.apply_merge(db, dup, canon, record_undo=True)
    # an intervening edit to the survivor: add a new alias
    add_alias(db, "person", canon, "Jowo Je", "english")
    db.commit()
    res = U.apply_undo(db, rep["undo_token"])
    assert "changed since" in res.get("error", "")
    # nothing was restored — the merge still stands, the token still present (not consumed)
    assert db.execute("SELECT 1 FROM person WHERE id=?", (dup,)).fetchone() is None
    assert db.execute("SELECT 1 FROM undo_log WHERE id=?", (rep["undo_token"],)).fetchone()


def test_undo_delete_after_referenced_work_deleted_still_restores(tmp_path):
    """Under the tombstone model a delete leaves the person's edges in place (riding the
    tombstone), so deleting a work afterward just drops that edge — there is no snapshot
    to re-insert and thus no missing-ref FK hazard. Undo restores the person cleanly."""
    db = init_db(tmp_path / "uref.db")
    pid = _person(db, "Junk Author", "author")
    w = _work(db, "Doomed Work")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w, pid))
    db.commit()
    rep = CE.apply_delete(db, pid, record_undo=True)
    db.execute("DELETE FROM work WHERE id=?", (w,)); db.commit()   # the work is now gone
    res = U.apply_undo(db, rep["undo_token"])
    assert res.get("undone") == rep["undo_token"]
    assert db.execute("SELECT 1 FROM v_live_person WHERE id=?", (pid,)).fetchone()  # restored, live


def test_undo_out_of_order_independent_ops_is_safe(tmp_path):
    """Undo is keyed to 'records unchanged', not to op order: two INDEPENDENT merges
    (disjoint persons) can be undone in any order. Undoing the older one restores only
    its records and leaves the newer merge intact."""
    db = init_db(tmp_path / "ooo.db")
    c1, d1, w1, e1 = _seed_dup(db)
    c2, d2, w2, e2 = _seed_dup(db)                       # a second, independent pair
    r1 = CE.apply_merge(db, d1, c1, record_undo=True)
    CE.apply_merge(db, d2, c2, record_undo=True)         # a later, unrelated merge
    res = U.apply_undo(db, r1["undo_token"])             # undo the OLDER op — allowed
    assert set(res["person_ids"]) == {c1, d1}
    assert db.execute("SELECT 1 FROM person WHERE id=?", (d1,)).fetchone() is not None
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w1,)).fetchone()[0] == d1
    # the later, independent merge is untouched
    assert db.execute("SELECT 1 FROM person WHERE id=?", (d2,)).fetchone() is None
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w2,)).fetchone()[0] == c2


def test_undo_refused_when_a_later_op_touched_the_same_records(tmp_path):
    """The dependent case: if a later op changed a record the older undo depends on,
    the fingerprint no longer matches and the stale undo is refused (not a clobber)."""
    db = init_db(tmp_path / "chain.db")
    c, d, w, e = _seed_dup(db)
    third = _person(db, "Third Person"); db.commit()
    r1 = CE.apply_merge(db, d, c, record_undo=True)      # d → c
    CE.apply_merge(db, c, third, record_undo=True)       # c → third: touches c, which r1 needs
    res = U.apply_undo(db, r1["undo_token"])
    assert "changed since" in res.get("error", "")       # refused — c is gone now
    assert db.execute("SELECT 1 FROM person WHERE id=?", (d,)).fetchone() is None  # not restored


def test_web_undo_route(tmp_path):
    from catalogue.webui.web import create_app
    dbp = tmp_path / "uweb.db"
    db = init_db(dbp)
    canon, dup, w, eid = _seed_dup(db)
    c = create_app(str(dbp)).test_client()
    rep = c.post(f"/picker/person/{dup}/merge", json={"into": canon}).get_json()
    assert rep["undo_token"] is not None
    res = c.post("/picker/person/undo", json={"token": rep["undo_token"]},
                 headers={"X-Requested-With": "fetch"}).get_json()
    assert set(res["person_ids"]) == {canon, dup}
    assert db.execute("SELECT 1 FROM person WHERE id=?", (dup,)).fetchone() is not None
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w,)).fetchone()[0] == dup


def test_web_mark_org_route(tmp_path):
    from catalogue.webui.web import create_app
    dbp = tmp_path / "web3.db"
    db = init_db(dbp)
    pid = _person(db, "Marpa Translation Society", "translator"); db.commit()
    c = create_app(str(dbp)).test_client()
    assert c.post(f"/picker/person/{pid}/org",
                  headers={"X-Requested-With": "fetch"}).get_json()["ok"]
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "organization"
    # it drops off the person worklist (picker selects provisional + external_id null)
    assert pid not in [r[0] for r in picker.unresolved(db, "person")]
    # undo restores it
    assert c.post(f"/picker/person/{pid}/org?undo=1",
                  headers={"X-Requested-With": "fetch"}).get_json()["ok"]
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "provisional"
