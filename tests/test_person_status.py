"""Tests for the 3-state person verification status + Wikidata/VIAF person
verifiers (catalogue/verify.py).

Status states: provisional → verified (authority match) | confirmed_local
(human says no authority exists). The verify walk only touches 'provisional'.
"""
from __future__ import annotations

import pytest

from catalogue.db_store import init_db, fold_key
from catalogue.services import verify


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "status.db")
    yield conn
    conn.close()


def _person(db, name):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, ?, 'english', ?)", (pid, name, fold_key(name)))
    return pid


# ── schema/migration default ────────────────────────────────────────────────────
def test_new_person_defaults_provisional(db):
    pid = _person(db, "Fresh Author")
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "provisional"


# ── confirm_local ────────────────────────────────────────────────────────────────
def test_confirm_local_sets_state_and_exits_worklist(db):
    pid = _person(db, "Modern Selfpublished Author")
    assert verify.confirm_local(db, pid) is True
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "confirmed_local"
    # The verify walk must now skip it (worklist is 'provisional' only).
    ids = verify._run  # smoke: function exists
    summary = verify.verify_all(db, verifiers=[], kinds=("person",))
    assert summary["person"]["matched"] == 0   # nothing matched; nothing crashed


def test_confirm_local_refuses_to_downgrade_verified(db):
    pid = _person(db, "Has Authority")
    db.execute("UPDATE person SET external_id='bdr:P9', "
               "verification_status='verified' WHERE id=?", (pid,))
    assert verify.confirm_local(db, pid) is False
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "verified"


# ── verify walk only processes provisional ───────────────────────────────────────
class _AlwaysPerson:
    name = "always"
    def verify(self, db, kind, text):
        return verify.Match("wikidata:Q1", "wikidata", text, [], self.name) \
            if kind == "person" else None


def test_walk_skips_confirmed_local(db):
    p1 = _person(db, "Needs Check")
    p2 = _person(db, "Already Local")
    verify.confirm_local(db, p2)
    summary = verify.verify_all(db, verifiers=[_AlwaysPerson()], kinds=("person",))
    # p1 matched; p2 untouched (not in worklist).
    assert summary["person"]["matched"] == 1
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (p2,)).fetchone()[0] == "confirmed_local"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (p2,)).fetchone()[0] is None


def test_match_sets_verified_status(db):
    pid = _person(db, "Some One")
    verify.verify_person(db, [_AlwaysPerson()], pid)
    row = db.execute("SELECT external_id, verification_status FROM person WHERE id=?",
                     (pid,)).fetchone()
    assert row == ("wikidata:Q1", "verified")


# ── Wikidata / VIAF person verifiers ─────────────────────────────────────────────
class _FakeWd:
    def __init__(self, hits, ents):
        self._hits, self._ents = hits, ents
    def search(self, text, *, language="en"):
        return self._hits
    def entity(self, qid):
        return self._ents.get(qid)


def test_wikidata_person_verifier_matches_human(db):
    ent = {"labels": {"en": {"value": "Tsongkhapa"}},
           "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]}}
    v = verify.WikidataPersonVerifier(
        client=_FakeWd([("Q187310", "Tsongkhapa", "")], {"Q187310": ent}))
    pid = _person(db, "Tsongkhapa")
    assert verify.verify_person(db, [v], pid) == "matched"
    assert db.execute("SELECT external_id FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "wikidata:Q187310"


def test_wikidata_harvests_authority_cross_links(db):
    """One resolved Wikidata person yields the BDRC/DILA/VIAF ids in a single hit
    (P2477/P1187/P214), stored as person_external_id rows — the hub strategy."""
    ent = {"labels": {"en": {"value": "Nagarjuna"}},
           "claims": {
               "P31":   [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
               "P2477": [{"mainsnak": {"datavalue": {"value": "P4954"}}}],
               "P1187": [{"mainsnak": {"datavalue": {"value": "A001583"}}}],
               "P214":  [{"mainsnak": {"datavalue": {"value": "8711937"}}}],
           }}
    v = verify.WikidataPersonVerifier(
        client=_FakeWd([("Q171195", "Nagarjuna", "")], {"Q171195": ent}))
    pid = _person(db, "Nagarjuna")
    assert verify.verify_person(db, [v], pid) == "matched"
    assert db.execute("SELECT external_id FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "wikidata:Q171195"
    got = dict(db.execute(
        "SELECT scheme, value FROM person_external_id WHERE person_id=?", (pid,)).fetchall())
    assert got == {"wikidata": "wikidata:Q171195", "bdrc": "bdr:P4954",
                   "dila": "dila:A001583", "viaf": "viaf:8711937"}


def test_wikidata_person_verifier_rejects_nonhuman(db):
    work_ent = {"labels": {"en": {"value": "Some Book"}},
                "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}]}}
    v = verify.WikidataPersonVerifier(
        client=_FakeWd([("Q2", "Some Book", "")], {"Q2": work_ent}))
    pid = _person(db, "Some Book")
    assert verify.verify_person(db, [v], pid) == "unmatched"


def test_wikidata_person_verifier_rejects_nonoverlapping_name(db):
    ent = {"labels": {"en": {"value": "Completely Different"}},
           "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]}}
    v = verify.WikidataPersonVerifier(
        client=_FakeWd([("Q9", "Completely Different", "")], {"Q9": ent}))
    pid = _person(db, "Nagarjuna")
    assert verify.verify_person(db, [v], pid) == "unmatched"


class _FakeViaf:
    def __init__(self, rows):
        self._rows = rows
    def suggest(self, text, *, nametype="personal"):
        return self._rows


def test_viaf_person_verifier_matches(db):
    v = verify.ViafPersonVerifier(client=_FakeViaf([("12345", "Thurman, Robert")]))
    pid = _person(db, "Robert Thurman")
    assert verify.verify_person(db, [v], pid) == "matched"
    assert db.execute("SELECT external_id FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "viaf:12345"


def test_offline_person_verifiers_noop(db):
    """offline=True → no client touch, returns unmatched, caches nothing."""
    wd = verify.WikidataPersonVerifier(offline=True)
    vf = verify.ViafPersonVerifier(offline=True)
    pid = _person(db, "Someone")
    assert verify.verify_person(db, [wd, vf], pid) == "unmatched"
    assert db.execute("SELECT COUNT(*) FROM resolver_cache").fetchone()[0] == 0


# ── purge restores provisional + accepts new id prefixes ─────────────────────────
def test_purge_resets_status_and_keeps_valid_prefixes(db):
    good_wd = _person(db, "WD Person")
    good_viaf = _person(db, "VIAF Person")
    bad = _person(db, "Bad Type")
    db.execute("UPDATE person SET external_id='wikidata:Q5', verification_status='verified' WHERE id=?", (good_wd,))
    db.execute("UPDATE person SET external_id='viaf:99', verification_status='verified' WHERE id=?", (good_viaf,))
    db.execute("UPDATE person SET external_id='bdr:WA42', verification_status='verified' WHERE id=?", (bad,))
    out = verify.purge_suspect_matches(db)
    assert out["person_wrongtype"] == 1
    # valid prefixes survive
    assert db.execute("SELECT external_id FROM person WHERE id=?", (good_wd,)).fetchone()[0] == "wikidata:Q5"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (good_viaf,)).fetchone()[0] == "viaf:99"
    # bad one cleared AND dropped back to provisional for re-check
    row = db.execute("SELECT external_id, verification_status FROM person WHERE id=?", (bad,)).fetchone()
    assert row == (None, "provisional")


# ── work-attached guard: name-only pass defers to the joint pass ────────────────
def _human(name):
    return {"labels": {"en": {"value": name}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]}}


def _work(db, title):
    from catalogue.db_store import add_alias
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, title, "english")
    return wid


class _FuzzyStub:
    """A verifier that returns a FUZZY (provisional) hit — like a BDRC BLMP match."""
    name = "bdrc"
    def verify(self, db, kind, text):
        return verify.Match("bdr:P4954", "bdrc", text, [], self.name, provisional=True)


def test_work_attached_person_hard_match_binds(db):
    """A HARD (non-provisional) exact-name hit binds even with a work edge: the
    verifier's name guard makes it safe, and the work-driven joint pass can't resolve
    persons whose only works are modern containers (the Atisha case)."""
    pid = _person(db, "Nagarjuna")
    wid = _work(db, "MMK")
    db.execute("INSERT INTO work_author (work_id, person_id, role) "
               "VALUES (?, ?, 'author')", (wid, pid))
    v = verify.WikidataPersonVerifier(
        client=_FakeWd([("Q171195", "Nagarjuna", "")], {"Q171195": _human("Nagarjuna")}))
    assert verify.verify_person(db, [v], pid) == "matched"
    row = db.execute("SELECT external_id, verification_status FROM person WHERE id=?",
                     (pid,)).fetchone()
    assert row[0] is not None and row[1] == "verified"


def test_work_attached_person_fuzzy_match_queues_candidate(db):
    """A FUZZY hit on a work-attached person is NEVER auto-bound (the Sajjana trap).
    DEFAULT now (joint pass deprecated): queue it as a candidate for human pick in
    /picker, instead of dead-ending it as 'deferred'. The row stays provisional."""
    pid = _person(db, "Nagarjuna")
    wid = _work(db, "MMK")
    db.execute("INSERT INTO work_author (work_id, person_id, role) "
               "VALUES (?, ?, 'author')", (wid, pid))
    assert verify.verify_person(db, [_FuzzyStub()], pid) == "candidate"
    row = db.execute("SELECT external_id, verification_status FROM person WHERE id=?",
                     (pid,)).fetchone()
    assert row == (None, "provisional")          # not bound; surfaced for review
    # legacy opt-in still defers for anyone who re-enables the joint pass
    pid2 = _person(db, "Vasubandhu")
    db.execute("INSERT INTO work_author (work_id, person_id, role) "
               "VALUES (?, ?, 'author')", (wid, pid2))
    assert verify.verify_person(db, [_FuzzyStub()], pid2, defer_to_joint=True) == "deferred"


def test_edition_translator_person_fuzzy_queues_candidate(db):
    pid = _person(db, "Some Translator")
    eid = db.execute("INSERT INTO edition (title) VALUES ('Bk')").lastrowid
    wid = _work(db, "W")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence, "
               "translator_person_id) VALUES (?, ?, 1, ?)", (eid, wid, pid))
    assert verify.verify_person(db, [_FuzzyStub()], pid) == "candidate"
    assert verify.verify_person(db, [_FuzzyStub()], pid, defer_to_joint=True) == "deferred"


def test_context_less_person_still_binds(db):
    """No work edge → name-only pass binds an unambiguous exact match as before."""
    pid = _person(db, "Bhikkhu Bodhi")
    v = verify.WikidataPersonVerifier(
        client=_FakeWd([("Q854944", "Bhikkhu Bodhi", "")], {"Q854944": _human("Bhikkhu Bodhi")}))
    assert verify.verify_person(db, [v], pid) == "matched"
    assert db.execute("SELECT external_id FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "wikidata:Q854944"


# ── ambiguity guard: 2+ exact-name humans → queue, don't guess ──────────────────
def test_two_exact_name_hits_queue_not_bind(db):
    """If the name matches 2+ distinct humans exactly, name-only must NOT pick the
    first — it queues a person_authority candidate for a human."""
    pid = _person(db, "John Smith")            # context-less, so the guard allows it
    v = verify.WikidataPersonVerifier(
        client=_FakeWd([("Q1", "John Smith", ""), ("Q2", "John Smith", "")],
                       {"Q1": _human("John Smith"), "Q2": _human("John Smith")}))
    assert verify.verify_person(db, [v], pid) == "candidate"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None
    q = db.execute("SELECT COUNT(*) FROM review_queue "
                   "WHERE item_type='person_authority' AND status='pending'").fetchone()[0]
    assert q == 1


def test_single_exact_among_nonmatches_still_binds(db):
    """One exact human + other non-matching candidates → unambiguous → bind."""
    pid = _person(db, "Nagarjuna")
    v = verify.WikidataPersonVerifier(
        client=_FakeWd([("Q9", "Someone Else", ""), ("Q171195", "Nagarjuna", "")],
                       {"Q9": _human("Someone Else"), "Q171195": _human("Nagarjuna")}))
    assert verify.verify_person(db, [v], pid) == "matched"
    assert db.execute("SELECT external_id FROM person WHERE id=?",
                      (pid,)).fetchone()[0] == "wikidata:Q171195"
