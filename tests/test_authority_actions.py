"""Accept/reject actions for queued authority candidates — the /review approve
flow (verify.accept_person_authority/accept_work_canonical/reject_candidate and
work_authority.accept_work_authorship/reject_work_authorship).

These are what turn a provisional BLMP/consensus candidate into a committed
binding (or discard it), driven by a human in the UI.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import init_db, add_alias, fold_key
from catalogue.services import verify, work_authority


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "aa.db")
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


def _queue(db, item_type, payload):
    return db.execute(
        "INSERT INTO review_queue (item_type, payload_json) VALUES (?, ?)",
        (item_type, json.dumps(payload))).lastrowid


# ── person_authority ────────────────────────────────────────────────────────────
def test_accept_person_authority_binds_and_resolves(db):
    pid = _person(db, "Nagarjuna")
    iid = _queue(db, "person_authority", {
        "person_id": pid, "candidate_id": "bdr:P4954",
        "canonical_name": "Nāgārjuna", "aliases": ["Klu sgrub"],
        "verifier": "bdrc", "reason": "bdrc_blmp_fuzzy"})
    assert verify.accept_person_authority(db, iid) is True
    row = db.execute("SELECT external_id, verification_status FROM person WHERE id=?",
                     (pid,)).fetchone()
    assert row == ("bdr:P4954", "verified")
    # candidate aliases attached
    keys = {r[0] for r in db.execute(
        "SELECT normalized_key FROM person_alias WHERE person_id=?", (pid,))}
    assert fold_key("Klu sgrub") in keys
    # item resolved
    assert db.execute("SELECT status FROM review_queue WHERE id=?",
                      (iid,)).fetchone()[0] == "resolved"


def test_accept_person_authority_refuses_if_already_bound(db):
    pid = _person(db, "X")
    db.execute("UPDATE person SET external_id='wikidata:Q1', "
               "verification_status='verified' WHERE id=?", (pid,))
    iid = _queue(db, "person_authority", {"person_id": pid, "candidate_id": "bdr:P9"})
    assert verify.accept_person_authority(db, iid) is False
    # untouched
    assert db.execute("SELECT external_id FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "wikidata:Q1"


def test_reject_candidate_marks_rejected_without_binding(db):
    pid = _person(db, "Y")
    iid = _queue(db, "person_authority", {"person_id": pid, "candidate_id": "bdr:P9"})
    assert verify.reject_candidate(db, iid) is True
    assert db.execute("SELECT status FROM review_queue WHERE id=?",
                      (iid,)).fetchone()[0] == "rejected"
    assert db.execute("SELECT external_id FROM person WHERE id=?",
                      (pid,)).fetchone()[0] is None


def test_accept_is_noop_on_nonpending(db):
    pid = _person(db, "Z")
    iid = _queue(db, "person_authority", {"person_id": pid, "candidate_id": "bdr:P9"})
    db.execute("UPDATE review_queue SET status='resolved' WHERE id=?", (iid,))
    assert verify.accept_person_authority(db, iid) is False


# ── work_canonical ──────────────────────────────────────────────────────────────
def test_accept_work_canonical_sets_id_and_resolves(db):
    wid = _work(db, "Heart Sutra")
    iid = _queue(db, "work_canonical", {
        "work_id": wid, "candidate_id": "bdr:WA123", "system": "bdrc",
        "canonical_name": "Prajñāpāramitāhṛdaya", "aliases": [], "verifier": "bdrc"})
    assert verify.accept_work_canonical(db, iid) is True
    assert db.execute("SELECT canonical_system, canonical_number FROM work WHERE id=?",
                      (wid,)).fetchone() == ("bdrc", "bdr:WA123")
    assert db.execute("SELECT status FROM review_queue WHERE id=?",
                      (iid,)).fetchone()[0] == "resolved"


def test_accept_work_canonical_refuses_if_already_set(db):
    wid = _work(db, "W")
    db.execute("UPDATE work SET canonical_system='toh', canonical_number='1' WHERE id=?",
               (wid,))
    iid = _queue(db, "work_canonical", {"work_id": wid, "candidate_id": "bdr:WA9",
                                        "system": "bdrc"})
    assert verify.accept_work_canonical(db, iid) is False
    assert db.execute("SELECT canonical_number FROM work WHERE id=?",
                      (wid,)).fetchone()[0] == "1"


# ── work_authorship ─────────────────────────────────────────────────────────────
def test_accept_work_authorship_links_contributors(db):
    wid = _work(db, "Bodhicaryāvatāra")
    eid = db.execute("INSERT INTO edition (title) VALUES ('Bca')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
               (eid, wid))
    iid = _queue(db, "work_authorship", {
        "work_id": wid, "verdict": "candidate",
        "authors": ["Śāntideva"], "translators": ["Some Translator"],
        "canonical_system": "toh", "canonical_number": "3871",
        "external_ids": {}, "agreement": 1, "sources": ["a"]})
    assert work_authority.accept_work_authorship(db, iid) is True
    # author on the work, translator on the work's edition
    assert {r[0] for r in db.execute(
        "SELECT role FROM work_author WHERE work_id=?", (wid,))} == {"author"}
    assert db.execute("SELECT COUNT(*) FROM edition_translator WHERE edition_id=?",
                      (eid,)).fetchone()[0] == 1
    assert db.execute("SELECT canonical_number FROM work WHERE id=?",
                      (wid,)).fetchone()[0] == "3871"
    assert db.execute("SELECT status FROM review_queue WHERE id=?",
                      (iid,)).fetchone()[0] == "resolved"


def test_reject_work_authorship_discards(db):
    wid = _work(db, "T")
    iid = _queue(db, "work_authorship", {"work_id": wid, "authors": ["X"]})
    assert work_authority.reject_work_authorship(db, iid) is True
    assert db.execute("SELECT status FROM review_queue WHERE id=?",
                      (iid,)).fetchone()[0] == "rejected"
    assert db.execute("SELECT COUNT(*) FROM work_author WHERE work_id=?",
                      (wid,)).fetchone()[0] == 0
