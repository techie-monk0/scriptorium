"""Unit tests for person↔work joint resolution (catalogue/person_work.py) + the
extracted verify.bind_person. Fast, no network — a stub resolver maps title→consensus.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import init_db, add_alias, fold_key
from catalogue.services import person_work, verify
from catalogue.services.work_authority import WorkAuthorityConsensus


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "pw.db")
    yield conn
    conn.close()


def _person(db, name):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid
    add_alias(db, "person", pid, name, "english")
    return pid


def _work(db, title):
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, title, "english")
    return wid


def _link(db, wid, pid, role="author"):
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,?)",
               (wid, pid, role))


def _consensus(name, qid, *, canonical=None, extra=None):
    ext = f"wikidata:{qid}"
    return WorkAuthorityConsensus(
        verdict="verified", authors=[name],
        canonical_system=("toh" if canonical else None), canonical_number=canonical,
        author_ids=[{"name": name, "external_id": ext,
                     "extra_ids": extra or {"wikidata": ext}}])


class _Stub:
    sources = []
    def __init__(self, by_title):
        self._m = by_title
    def resolve(self, title, *, language=None, aliases=()):
        return self._m.get(title, WorkAuthorityConsensus("none"))


# ── verdict matrix ──────────────────────────────────────────────────────────────
def test_single_work_exact_match_binds(db):
    pid = _person(db, "Nagarjuna"); wid = _work(db, "MMK"); _link(db, wid, pid)
    r = person_work.resolve_person_via_works(
        db, _Stub({"MMK": _consensus("Nagarjuna", "Q171195",
                                     extra={"wikidata": "wikidata:Q171195",
                                            "bdrc": "bdr:P4954"})}), pid)
    assert r == "matched"
    row = db.execute("SELECT external_id, verification_status FROM person WHERE id=?",
                     (pid,)).fetchone()
    assert row == ("wikidata:Q171195", "verified")
    # cross-links harvested
    xs = dict(db.execute("SELECT scheme, value FROM person_external_id WHERE person_id=?",
                         (pid,)).fetchall())
    assert xs["bdrc"] == "bdr:P4954"


def test_agreeing_works_bind(db):
    pid = _person(db, "Nagarjuna")
    for t in ("MMK", "Vigrahavyavartani"):
        w = _work(db, t); _link(db, w, pid)
    stub = _Stub({"MMK": _consensus("Nagarjuna", "Q171195"),
                  "Vigrahavyavartani": _consensus("Nagarjuna", "Q171195")})
    assert person_work.resolve_person_via_works(db, stub, pid) == "matched"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] \
        == "wikidata:Q171195"


def test_disagreeing_works_queue_conflict_not_bound(db):
    pid = _person(db, "Nagarjuna")
    for t in ("A", "B"):
        w = _work(db, t); _link(db, w, pid)
    stub = _Stub({"A": _consensus("Nagarjuna", "Q1"), "B": _consensus("Nagarjuna", "Q2")})
    assert person_work.resolve_person_via_works(db, stub, pid) == "candidate"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None
    pj = db.execute("SELECT payload_json FROM review_queue "
                    "WHERE item_type='person_work_joint'").fetchone()[0]
    assert json.loads(pj)["reason"] == "work_conflict"


def test_fuzzy_name_queues_needs_confirm(db):
    pid = _person(db, "Nagarjuna"); w = _work(db, "T"); _link(db, w, pid)
    # author shares the 'nagarjuna' token but isn't an exact whole-name match
    stub = _Stub({"T": _consensus("Nagarjuna Gupta", "Q5")})
    assert person_work.resolve_person_via_works(db, stub, pid) == "candidate"
    pj = db.execute("SELECT payload_json FROM review_queue "
                    "WHERE item_type='person_work_joint'").fetchone()[0]
    assert json.loads(pj)["reason"] == "needs_confirm"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None


def test_name_mismatch_sajjana_guard_unmatched(db):
    pid = _person(db, "Nagarjuna"); w = _work(db, "T"); _link(db, w, pid)
    stub = _Stub({"T": _consensus("Sajjana", "Q9")})   # unrelated author
    assert person_work.resolve_person_via_works(db, stub, pid) == "unmatched"
    assert db.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0


def test_no_work_is_unmatched(db):
    pid = _person(db, "Nagarjuna")
    assert person_work.resolve_person_via_works(db, _Stub({}), pid) == "unmatched"


def test_already_bound_is_already(db):
    pid = _person(db, "Nagarjuna"); w = _work(db, "MMK"); _link(db, w, pid)
    db.execute("UPDATE person SET external_id='wikidata:Qx' WHERE id=?", (pid,))
    assert person_work.resolve_person_via_works(
        db, _Stub({"MMK": _consensus("Nagarjuna", "Q1")}), pid) == "already"


def test_tombstoned_person_is_unmatched_and_skipped(db):
    """A soft-deleted person reads as absent (engine live-only): resolve returns
    'unmatched' and the walk never visits it."""
    from catalogue.services import contributor_edit as CE
    pid = _person(db, "Nagarjuna"); w = _work(db, "MMK"); _link(db, w, pid)
    db.commit()
    CE.apply_delete(db, pid)                                # tombstone
    assert person_work.resolve_person_via_works(
        db, _Stub({"MMK": _consensus("Nagarjuna", "Q1")}), pid) == "unmatched"
    tally = person_work.resolve_all_person_works(db, _Stub({}))
    assert tally["matched"] == 0 and tally["candidate"] == 0


def test_walk_processes_only_provisional_and_resumes(db):
    p1 = _person(db, "Nagarjuna"); w1 = _work(db, "MMK"); _link(db, w1, p1)
    p2 = _person(db, "Already"); db.execute(
        "UPDATE person SET external_id='wikidata:Qz', verification_status='verified' WHERE id=?", (p2,))
    tally = person_work.resolve_all_person_works(
        db, resolver=_Stub({"MMK": _consensus("Nagarjuna", "Q171195")}))
    assert tally["matched"] == 1            # p1 bound; p2 not in worklist


# ── accept / reject ──────────────────────────────────────────────────────────────
def _queue(db, payload):
    return db.execute(
        "INSERT INTO review_queue (item_type, payload_json) VALUES ('person_work_joint', ?)",
        (json.dumps(payload),)).lastrowid


def test_accept_needs_confirm_binds(db):
    pid = _person(db, "Nagarjuna")
    iid = _queue(db, {"person_id": pid, "candidate_id": "wikidata:Q5555",
                      "candidate_name": "Nagarjuna Gupta",
                      "extra_ids": {"wikidata": "wikidata:Q5555"},
                      "reason": "needs_confirm"})
    assert person_work.accept_person_work_joint(db, iid) is True
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] \
        == "wikidata:Q5555"
    assert db.execute("SELECT status FROM review_queue WHERE id=?",
                      (iid,)).fetchone()[0] == "resolved"


def test_accept_refuses_conflict(db):
    pid = _person(db, "Nagarjuna")
    iid = _queue(db, {"person_id": pid, "candidate_id": "wikidata:Q1",
                      "reason": "work_conflict"})
    assert person_work.accept_person_work_joint(db, iid) is False
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None


def test_reject_marks_rejected(db):
    pid = _person(db, "Nagarjuna")
    iid = _queue(db, {"person_id": pid, "candidate_id": "wikidata:Q1",
                      "reason": "needs_confirm"})
    assert person_work.reject_person_work_joint(db, iid) is True
    assert db.execute("SELECT status FROM review_queue WHERE id=?",
                      (iid,)).fetchone()[0] == "rejected"


def test_accept_on_nonpending_is_noop(db):
    pid = _person(db, "Nagarjuna")
    iid = _queue(db, {"person_id": pid, "candidate_id": "wikidata:Q1",
                      "reason": "needs_confirm"})
    db.execute("UPDATE review_queue SET status='resolved' WHERE id=?", (iid,))
    assert person_work.accept_person_work_joint(db, iid) is False


# ── verify.bind_person (the extracted shared binder) ─────────────────────────────
def test_bind_person_sets_id_status_aliases_crosslinks(db):
    pid = _person(db, "Nagarjuna")
    ok = verify.bind_person(db, pid, "wikidata:Q171195", "Nāgārjuna",
                            aliases=["Klu sgrub"],
                            extra_ids={"wikidata": "wikidata:Q171195", "bdrc": "bdr:P4954"})
    assert ok is True
    row = db.execute("SELECT external_id, verification_status FROM person WHERE id=?",
                     (pid,)).fetchone()
    assert row == ("wikidata:Q171195", "verified")
    keys = {r[0] for r in db.execute(
        "SELECT normalized_key FROM person_alias WHERE person_id=?", (pid,))}
    assert fold_key("Klu sgrub") in keys
    assert dict(db.execute("SELECT scheme,value FROM person_external_id WHERE person_id=?",
                           (pid,)).fetchall())["bdrc"] == "bdr:P4954"


def test_bind_person_refuses_when_already_bound(db):
    pid = _person(db, "X")
    db.execute("UPDATE person SET external_id='wikidata:Qa' WHERE id=?", (pid,))
    assert verify.bind_person(db, pid, "wikidata:Qb") is False
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] \
        == "wikidata:Qa"
