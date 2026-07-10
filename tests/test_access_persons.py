"""Person aggregate — read + soft-delete with the identity guard + second-order orphans (Phase 3).

Person is a soft-delete root: a delete tombstones the row (aliases/external-ids ride along and return
on restore — no non-FK file/cache closure to purge), and the only non-trivial closure is semantic and
second-order — a Work left with no live author, whose fate the client's `OrphanPolicy` decides. The
identity fingerprint (name-fold + dates) is `person_identity_ok` expressed as a `Ref` revalidation, so
a recycled id is caught at apply. System through a real DB. See entity_api_model.md §3/§5/§6.
"""
from __future__ import annotations

import pytest

from catalogue.access_api import system_access
from catalogue.contracts import (
    GCOrphans,
    IntegrityViolation,
    OrphanDecision,
    Person,
    Ref,
    RefuseOrphans,
    StaleWrite,
    person_fingerprint,
)
from catalogue.db_store import init_db


def _seed(tmp_path):
    """p_solo solely authors w_solo (→ orphan when deleted). p_a + p_b co-author w_shared (deleting
    either leaves a live author → no orphan). p_solo also carries an alias + external-id (parts that
    must ride the tombstone and return on restore)."""
    db = tmp_path / "t.db"
    c = init_db(db)
    p_solo = c.execute(
        "INSERT INTO person (primary_name, dates, external_id) "
        "VALUES ('Chögyam Trungpa', '1939-1987', 'wikidata:Q1')").lastrowid
    p_a = c.execute("INSERT INTO person (primary_name) VALUES ('Author A')").lastrowid
    p_b = c.execute("INSERT INTO person (primary_name) VALUES ('Author B')").lastrowid
    w_solo = c.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    w_shared = c.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    for (w, p) in ((w_solo, p_solo), (w_shared, p_a), (w_shared, p_b)):
        c.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (w, p))
    c.execute("INSERT INTO person_alias (person_id, text, normalized_key) "
              "VALUES (?, 'Trungpa Rinpoche', 'trungpa rinpoche')", (p_solo,))
    c.execute("INSERT INTO person_external_id (person_id, scheme, value) "
              "VALUES (?, 'viaf', '12345')", (p_solo,))
    c.commit()
    c.close()
    return dict(db=db, p_solo=p_solo, p_a=p_a, p_b=p_b, w_solo=w_solo, w_shared=w_shared)


# ── reads ─────────────────────────────────────────────────────────────────────────
def test_reader_get_and_by_work(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        p = acc.persons.reads.get(s["p_solo"])
        assert p.primary_name == "Chögyam Trungpa" and p.dates == "1939-1987"
        assert p.external_id == "wikidata:Q1"
        assert {p.id for p in acc.persons.reads.by_work(s["w_shared"])} == {s["p_a"], s["p_b"]}


def test_fingerprint_folds_name_and_pins_dates(tmp_path):
    # name-fold ignores case/whitespace; dates are part of identity
    assert person_fingerprint(" Chögyam   Trungpa ", "1939-1987") == \
        person_fingerprint("chögyam trungpa", "1939-1987")
    assert person_fingerprint("Chögyam Trungpa", "1939-1987") != \
        person_fingerprint("Chögyam Trungpa", "1000-1100")
    assert Person(1, "Name", dates="x").ref().kind == "person"


# ── soft-delete + identity guard ────────────────────────────────────────────────────
def test_tombstone_hides_person_but_keeps_parts_then_restore(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.persons.writes.apply(acc.persons.writes.plan_delete(Ref("person", s["p_a"])))
        assert acc.persons.reads.get(s["p_a"]) is None               # hidden from reads
        # row persists as a frozen tombstone (id never reused → cache/review refs stay safe)
        assert acc.ro.execute("SELECT deleted_at FROM person WHERE id=?", (s["p_a"],)).fetchone()[0] is not None
        # owned parts ride the tombstone (no purge) — and come back on restore
        acc.persons.writes.restore(Ref("person", s["p_a"]))
        assert acc.persons.reads.get(s["p_a"]).primary_name == "Author A"


def test_owned_parts_survive_tombstone(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.persons.writes.apply(acc.persons.writes.plan_delete(Ref("person", s["p_solo"])))
        # alias + external-id rows are NOT purged (soft-delete keeps the shell intact)
        assert acc.ro.execute("SELECT count(*) FROM person_alias WHERE person_id=?", (s["p_solo"],)).fetchone()[0] == 1
        assert acc.ro.execute("SELECT count(*) FROM person_external_id WHERE person_id=?", (s["p_solo"],)).fetchone()[0] == 1


def test_redelete_of_tombstone_is_blocked(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.persons.writes.apply(acc.persons.writes.plan_delete(Ref("person", s["p_a"])))
        replan = acc.persons.writes.plan_delete(Ref("person", s["p_a"]))
        assert not replan.appliable and any(b.code == "not_found" for b in replan.blocks)


def test_fingerprint_mismatch_is_stale(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.persons.writes.plan_delete(Ref("person", s["p_solo"]))
        acc.rw.execute("UPDATE person SET primary_name='Someone Else' WHERE id=?", (s["p_solo"],))
        acc.rw.commit()
        with pytest.raises(StaleWrite):     # recycled-id guard fires
            acc.persons.writes.apply(plan)


# ── second-order (semantic) orphans ──────────────────────────────────────────────────
def test_plan_flags_authorless_work_by_default(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.persons.writes.plan_delete(Ref("person", s["p_solo"]))   # default FlagOrphans
        assert plan.appliable
        assert {o.ref.id: o.decision for o in plan.orphans} == {s["w_solo"]: OrphanDecision.FLAG}


def test_no_orphan_when_coauthor_remains(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        # deleting one of two co-authors leaves w_shared with a live author → no orphan
        plan = acc.persons.writes.plan_delete(Ref("person", s["p_a"]))
        assert plan.orphans == ()


def test_apply_gc_tombstones_orphan_work(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.persons.writes.apply(
            acc.persons.writes.plan_delete(Ref("person", s["p_solo"]), policy=GCOrphans()))
        assert acc.persons.reads.get(s["p_solo"]) is None
        # GC tombstones the now-authorless work (the second-order effect)
        assert acc.ro.execute("SELECT deleted_at FROM work WHERE id=?", (s["w_solo"],)).fetchone()[0] is not None
        assert acc.ro.execute("SELECT deleted_at FROM work WHERE id=?", (s["w_shared"],)).fetchone()[0] is None


def test_apply_flag_keeps_orphan_work_live(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.persons.writes.apply(acc.persons.writes.plan_delete(Ref("person", s["p_solo"])))
        # FLAG (default) keeps the authorless work LIVE — authorless works are normal here
        assert acc.ro.execute("SELECT deleted_at FROM work WHERE id=?", (s["w_solo"],)).fetchone()[0] is None


def test_refuse_orphan_blocks_apply(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.persons.writes.plan_delete(Ref("person", s["p_solo"]), policy=RefuseOrphans())
        assert not plan.appliable and any(b.code == "orphan_refuse" for b in plan.blocks)
        with pytest.raises(IntegrityViolation):
            acc.persons.writes.apply(plan)
        assert acc.persons.reads.get(s["p_solo"]) is not None        # nothing happened


# ── Session/UoW staging (the person delete + its second-order work GC, atomic) ─────────
def test_session_stages_person_delete_with_orphan_gc(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        with acc.session() as sess:
            sess.stage(acc.persons.writes,
                       acc.persons.writes.plan_delete(Ref("person", s["p_solo"]), policy=GCOrphans()))
        assert acc.persons.reads.get(s["p_solo"]) is None
        assert acc.ro.execute("SELECT deleted_at FROM work WHERE id=?", (s["w_solo"],)).fetchone()[0] is not None


def test_session_rolls_back_on_error(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        with pytest.raises(StaleWrite):
            with acc.session() as sess:
                plan = acc.persons.writes.plan_delete(Ref("person", s["p_solo"]))
                acc.rw.execute("UPDATE person SET primary_name='Drifted' WHERE id=?", (s["p_solo"],))
                sess.stage(acc.persons.writes, plan)   # recheck fails → raise → rollback
        # the in-session UPDATE was rolled back too (all-or-nothing)
        assert acc.ro.execute("SELECT primary_name FROM person WHERE id=?", (s["p_solo"],)).fetchone()[0] \
            == "Chögyam Trungpa"
        assert acc.persons.reads.get(s["p_solo"]) is not None


# ── people directory (the webui /people list, routed through the access-API) ────────
def test_directory_name_ordered_alias_search_and_tombstone_excluded(tmp_path):
    db = tmp_path / "d.db"
    c = init_db(db)

    def per(name):
        return c.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid

    def alias(pid, text, key):
        c.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
                  "VALUES (?, ?, 'other', ?)", (pid, text, key))

    a = per("Zenkar Rinpoche"); alias(a, "Zenkar Rinpoche", "zenkar rinpoche")
    b = per("Alak Zenkar"); alias(b, "Alak Zenkar", "alak zenkar"); alias(b, "Thubten Nyima", "thubten nyima")
    dead = per("Deleted Person")
    c.execute("UPDATE person SET deleted_at = datetime('now') WHERE id = ?", (dead,))
    c.commit(); c.close()

    with system_access(db) as acc:
        names = [p.primary_name for p in acc.persons.reads.directory()]
        assert names == ["Alak Zenkar", "Zenkar Rinpoche"]      # primary_name order, tombstone hidden
        assert acc.persons.reads.count() == 2                   # live total
        # search spans EVERY alias, not just the primary name
        assert [p.primary_name for p in acc.persons.reads.directory("nyima")] == ["Alak Zenkar"]
        # both match "zenkar" (one on the primary name, one on an alias), still primary_name-ordered
        assert [p.primary_name for p in acc.persons.reads.directory("zenkar")] == [
            "Alak Zenkar", "Zenkar Rinpoche"]
        assert acc.persons.reads.directory("nobody") == []


def test_update_sets_notes_and_writes_audit(tmp_path):
    """person update accepts the `notes` field (gate + DTO) and the edit lands in the audit trail."""
    db = tmp_path / "u.db"
    c = init_db(db)
    pid = c.execute("INSERT INTO person (primary_name) VALUES ('Old Name')").lastrowid
    c.commit(); c.close()
    with system_access(db) as acc:
        acc.persons.writes.apply(acc.persons.writes.plan_update(
            Ref("person", pid),
            {"primary_name": "New Name", "dates": "1900-1980", "notes": "a careful note"}))
        p = acc.persons.reads.get(pid)
        assert (p.primary_name, p.dates, p.notes) == ("New Name", "1900-1980", "a careful note")
        assert p.rev == 1                                          # optimistic-concurrency bump
        trail = acc.audit_trail(entity_kind="person", entity_id=pid)
        assert trail and trail[0]["op"] == "update" and "notes" in (trail[0]["detail"] or "")


def test_aliases_and_external_ids_reads(tmp_path):
    db = tmp_path / "ax.db"
    c = init_db(db)
    pid = c.execute("INSERT INTO person (primary_name) VALUES ('Person X')").lastrowid
    c.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
              "VALUES (?, 'X Variant', 'english', 'x variant')", (pid,))
    c.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
              "VALUES (?, 'X Tibetan', 'wylie', 'x tibetan')", (pid,))
    c.execute("INSERT INTO person_external_id (person_id, scheme, value) VALUES (?, 'viaf', 'V1')", (pid,))
    c.execute("INSERT INTO person_external_id (person_id, scheme, value) VALUES (?, 'wikidata', 'Q9')", (pid,))
    c.commit(); c.close()
    with system_access(db) as acc:
        aliases = acc.persons.reads.aliases(pid)
        assert [a[1] for a in aliases] == ["X Variant", "X Tibetan"]          # id-ordered, (id,text,scheme)
        assert {a[2] for a in aliases} == {"english", "wylie"}
        assert acc.persons.reads.external_ids(pid) == [("viaf", "V1"), ("wikidata", "Q9")]  # scheme-ordered


# ── alias sub-entity commands (add / remove / set_primary) ──────────────────────────
def test_alias_commands_add_remove_set_primary_and_audit(tmp_path):
    db = tmp_path / "ac.db"
    c = init_db(db)
    pid = c.execute("INSERT INTO person (primary_name) VALUES ('First Name')").lastrowid
    c.commit(); c.close()
    with system_access(db) as acc:
        w = acc.persons.writes
        # add
        w.add_alias(pid, "A Spelling", "english")
        assert [a[1] for a in acc.persons.reads.aliases(pid)] == ["A Spelling"]
        # add if_absent is idempotent on the fold; a plain add always inserts
        w.add_alias(pid, "A Spelling", "english", if_absent=True)        # no-op (fold exists)
        assert len(acc.persons.reads.aliases(pid)) == 1
        w.add_alias(pid, "A Spelling", "english")                        # explicit add → duplicate
        assert len(acc.persons.reads.aliases(pid)) == 2
        # remove the first
        first_id = acc.persons.reads.aliases(pid)[0][0]
        w.remove_alias(pid, first_id)
        assert first_id not in {a[0] for a in acc.persons.reads.aliases(pid)}
        # set_primary: promote an alias, keep the old primary name searchable
        promote = acc.persons.reads.aliases(pid)[0][0]
        w.set_primary(pid, promote)
        assert acc.persons.reads.get(pid).primary_name == "A Spelling"
        assert "First Name" in {a[1] for a in acc.persons.reads.aliases(pid)}   # old name kept
        # audited
        ops = [r["op"] for r in acc.audit_trail(entity_kind="person", entity_id=pid)]
        assert "add_alias" in ops and "remove_alias" in ops and "set_primary" in ops


def test_add_alias_to_missing_person_raises(tmp_path):
    db = tmp_path / "m.db"
    init_db(db).close()
    with system_access(db) as acc:
        with pytest.raises(Exception):           # NotFound
            acc.persons.writes.add_alias(9999, "x")


def test_contributed_works_and_appearing_editions(tmp_path):
    db = tmp_path / "cw.db"
    c = init_db(db)
    pid = c.execute("INSERT INTO person (primary_name) VALUES ('Author P')").lastrowid
    w = c.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    c.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
              "VALUES (?, 'The Work', 'english', 'the work')", (w,))
    c.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?, ?, 'author')", (w, pid))
    e_live = c.execute("INSERT INTO edition (title, year) VALUES ('Live Book', 2020)").lastrowid
    c.execute("INSERT INTO edition_author (edition_id, person_id, seq) VALUES (?, ?, 1)", (e_live, pid))
    e_dead = c.execute("INSERT INTO edition (title, year) VALUES ('Dead Book', 2019)").lastrowid
    c.execute("INSERT INTO edition_author (edition_id, person_id, seq) VALUES (?, ?, 1)", (e_dead, pid))
    c.execute("UPDATE edition SET deleted_at = datetime('now') WHERE id = ?", (e_dead,))
    c.commit(); c.close()
    with system_access(db) as acc:
        works = acc.persons.reads.contributed_works(pid)
        assert works == [(w, "author", "The Work")]
        eds = acc.persons.reads.appearing_editions(pid)
        assert eds == [(e_live, "Live Book", 2020, "author")]      # tombstoned edition excluded


# ── merge (fold loser into winner) ──────────────────────────────────────────────────
def test_merge_repoints_edges_aliases_extids_refs_and_tombstones_loser(tmp_path):
    import json
    c = init_db(tmp_path / "mrg.db")
    winner = c.execute("INSERT INTO person (primary_name) VALUES ('Canonical')").lastrowid
    loser = c.execute("INSERT INTO person (primary_name, dates, external_id) "
                      "VALUES ('Duplicate', '1900-1980', 'viaf:V1')").lastrowid
    w = c.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    e = c.execute("INSERT INTO edition (title) VALUES ('Bk')").lastrowid
    c.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w, loser))
    c.execute("INSERT INTO edition_author (edition_id, person_id, seq) VALUES (?,?,1)", (e, loser))
    c.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
              "VALUES (?, 'Dup Alias', 'english', 'dup alias')", (loser,))
    c.execute("INSERT INTO person_external_id (person_id, scheme, value) VALUES (?, 'viaf', 'V1')", (loser,))
    # a person-owned review item + a promotion array referencing the loser
    rid = c.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('person_authority', ?)",
                    (json.dumps({"person_id": loser, "candidate_id": "P1"}),)).lastrowid
    prom = c.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('ingest', ?)",
                     (json.dumps({"path": "/x"}),)).lastrowid
    c.execute("INSERT INTO promotion (review_item_id, work_ids, person_ids) VALUES (?, '[]', ?)",
              (prom, json.dumps([loser])))
    c.commit(); c.close()

    with system_access(tmp_path / "mrg.db") as acc:
        imp = acc.persons.writes.merge(Ref("person", loser), Ref("person", winner))
        assert imp.op == "merge"
        # loser tombstoned, winner live
        assert acc.persons.reads.get(loser) is None
        win = acc.persons.reads.get(winner)
        assert win is not None
        # edges + alias + external-id + dates carried onto the winner
        assert [w_ for (w_, _r, _l) in acc.persons.reads.contributed_works(winner)] == [w]
        names = {a[1] for a in acc.persons.reads.aliases(winner)}
        assert "Dup Alias" in names and "Duplicate" in names      # loser name kept searchable
        assert ("viaf", "V1") in acc.persons.reads.external_ids(winner)
        assert win.dates == "1900-1980" and win.external_id == "viaf:V1"   # backfilled
        # non-FK refs repointed to the winner
        assert acc.rw.execute("SELECT payload_json FROM review_queue WHERE id=?", (rid,)).fetchone()[0]
        pay = json.loads(acc.rw.execute("SELECT payload_json FROM review_queue WHERE id=?",
                                        (rid,)).fetchone()[0])
        assert pay["person_id"] == winner
        parr = json.loads(acc.rw.execute("SELECT person_ids FROM promotion WHERE review_item_id=?",
                                         (prom,)).fetchone()[0])
        assert parr == [winner]
        # edges no longer reference the loser
        assert acc.rw.execute("SELECT COUNT(*) FROM work_author WHERE person_id=?",
                              (loser,)).fetchone()[0] == 0


def test_merge_blocks_self_and_cross_authority(tmp_path):
    c = init_db(tmp_path / "mb.db")
    a = c.execute("INSERT INTO person (primary_name, external_id) VALUES ('A', 'viaf:1')").lastrowid
    b = c.execute("INSERT INTO person (primary_name, external_id) VALUES ('B', 'viaf:2')").lastrowid
    c.commit(); c.close()
    with system_access(tmp_path / "mb.db") as acc:
        self_p = acc.persons.writes.plan_merge(Ref("person", a), Ref("person", a))
        assert not self_p.appliable and any("itself" in bl.message for bl in self_p.blocks)
        cross = acc.persons.writes.plan_merge(Ref("person", a), Ref("person", b))
        assert not cross.appliable and any(bl.code == "conflict" for bl in cross.blocks)
        # allow_cross_authority lifts the rail
        assert acc.persons.writes.plan_merge(Ref("person", a), Ref("person", b),
                                             allow_cross_authority=True).appliable


def test_split_attaches_targets_and_hard_deletes_the_blob(tmp_path):
    c = init_db(tmp_path / "sp.db")
    blob = c.execute("INSERT INTO person (primary_name) VALUES ('A, B')").lastrowid
    w = c.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    e = c.execute("INSERT INTO edition (title) VALUES ('Bk')").lastrowid
    c.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (e, w))
    c.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w, blob))
    a = c.execute("INSERT INTO person (primary_name) VALUES ('A')").lastrowid
    b = c.execute("INSERT INTO person (primary_name) VALUES ('B')").lastrowid
    c.commit(); c.close()
    with system_access(tmp_path / "sp.db") as acc:
        acc.persons.writes.split(Ref("person", blob),
                                 [{"id": a, "role": "author"}, {"id": b, "role": "translator"}])
        assert acc.persons.reads.get(blob) is None                       # blob hard-deleted
        # 'A' inherits the work as author; 'B' becomes a translator of its edition
        assert acc.rw.execute("SELECT role FROM work_author WHERE work_id=? AND person_id=?",
                              (w, a)).fetchone()[0] == "author"
        assert acc.rw.execute("SELECT COUNT(*) FROM edition_translator WHERE edition_id=? AND person_id=?",
                              (e, b)).fetchone()[0] == 1
        assert acc.rw.execute("SELECT COUNT(*) FROM work_author WHERE person_id=?",
                              (blob,)).fetchone()[0] == 0                 # blob detached
