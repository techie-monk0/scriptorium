"""Wiring tests for the EditionVerifier pass (catalogue/edition_verify.py):
inferred-metadata gathering, the actionable-only review queue, and the
accept (safe backfill) / reject actions. Engine logic itself is covered by
test_edition_verify.py; here we exercise the DB-facing batch + review wiring
with stub sources (no network).
"""
from __future__ import annotations

import json

import catalogue.services.edition_verify as EV
from catalogue.db_store import init_db


class _StubSource(EV.EditionSource):
    def __init__(self, name, isbn_recs=None, tp_recs=None):
        self.name = name
        self._isbn = isbn_recs or []
        self._tp = tp_recs or []

    def by_isbn(self, isbn):
        return list(self._isbn)

    def by_title_publisher(self, title, *, publisher=None, year=None):
        return list(self._tp)


def _db():
    return init_db(":memory:")


def _person(db, name):
    return db.execute("INSERT INTO person (primary_name) VALUES (?)",
                      (name,)).lastrowid


def _work(db):
    return db.execute("INSERT INTO work (notes) VALUES (NULL)").lastrowid


def _edition(db, title, **kw):
    cols = ["title"] + list(kw)
    vals = [title] + list(kw.values())
    ph = ", ".join("?" for _ in cols)
    return db.execute(
        f"INSERT INTO edition ({', '.join(cols)}) VALUES ({ph})", vals).lastrowid


def _link(db, eid, wid, *, seq=1, translator_pid=None):
    db.execute(
        "INSERT INTO edition_work (edition_id, work_id, sequence, translator_person_id) "
        "VALUES (?, ?, ?, ?)", (eid, wid, seq, translator_pid))


def _contrib(db, wid, pid, role="author"):
    # Authors live on the work; translators are edition-level (seed those directly
    # in the test, since this helper has no edition context).
    db.execute(
        "INSERT INTO work_author (work_id, person_id, role) VALUES (?, ?, ?)",
        (wid, pid, role))


# ── inferred-metadata gathering ──────────────────────────────────────────────────
def test_inferred_pulls_authors_and_translators():
    db = _db()
    eid = _edition(db, "Some Book", publisher="Wisdom", year=2001, isbn="x")
    wid = _work(db)
    a = _person(db, "Jane Author")
    t = _person(db, "Tom Translator")
    bt = _person(db, "Book Translator")
    _contrib(db, wid, a, "author")
    # work-level translator → now an edition translator; book-level via override
    db.execute("INSERT INTO edition_translator (edition_id, person_id, seq) VALUES (?,?,1)",
               (eid, t))
    _link(db, eid, wid, translator_pid=bt)
    inf = EV._inferred_for_edition(db, eid)
    assert inf["title"] == "Some Book"
    assert inf["publisher"] == "Wisdom" and inf["year"] == 2001
    assert inf["authors"] == ["Jane Author"]
    # work-level + book-level translators both gathered, deduped
    assert set(inf["translators"]) == {"Tom Translator", "Book Translator"}


# ── actionable-only queue ────────────────────────────────────────────────────────
def test_clean_report_is_not_queued():
    db = _db()
    eid = _edition(db, "Real Title", publisher="Penguin", year=1999)
    wid = _work(db)
    _contrib(db, wid, _person(db, "A"), "author")
    _link(db, eid, wid)
    v = EV.EditionVerifier(sources=[_StubSource("s", tp_recs=[
        EV.EditionRecord("s", title="Real Title", authors=("A",),
                         publisher="Penguin", year=1999)])], db=None)
    assert EV.verify_edition(db, v, eid) == "clean"
    assert db.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0


def test_no_authority_is_not_queued():
    db = _db()
    eid = _edition(db, "Unfindable")
    v = EV.EditionVerifier(sources=[_StubSource("s")], db=None)   # returns nothing
    assert EV.verify_edition(db, v, eid) == "no_authority"
    assert db.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0


def test_mismatch_is_queued_once():
    db = _db()
    eid = _edition(db, "Real Title", publisher="Random House", year=1999)
    wid = _work(db)
    _contrib(db, wid, _person(db, "A"), "author")
    _link(db, eid, wid)
    v = EV.EditionVerifier(sources=[_StubSource("s", tp_recs=[
        EV.EditionRecord("s", title="Real Title", authors=("A",),
                         publisher="Penguin", year=1999)])], db=None)
    assert EV.verify_edition(db, v, eid) == "actionable"
    assert db.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 1
    # idempotent: a pending item blocks a duplicate
    assert EV.verify_edition(db, v, eid) == "already"
    assert db.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 1
    p = json.loads(db.execute("SELECT payload_json FROM review_queue").fetchone()[0])
    assert p["edition_id"] == eid
    assert next(f for f in p["fields"] if f["field"] == "publisher")["status"] == "mismatch"


# ── accept: safe additive backfill ───────────────────────────────────────────────
def test_accept_backfills_empty_year_only():
    db = _db()
    # publisher present (Wisdom), year MISSING → authority supplies both; only the
    # empty year is backfilled, the populated publisher is left untouched.
    eid = _edition(db, "Book", publisher="Wisdom")
    wid = _work(db)
    _contrib(db, wid, _person(db, "A"), "author")
    _link(db, eid, wid)
    v = EV.EditionVerifier(sources=[_StubSource("s", tp_recs=[
        EV.EditionRecord("s", title="Book", authors=("A",),
                         publisher="Shambhala", year=2010)])], db=None)
    assert EV.verify_edition(db, v, eid) == "actionable"
    item_id = db.execute("SELECT id FROM review_queue").fetchone()[0]
    assert EV.accept_edition_verify(db, item_id) is True
    pub, yr = db.execute("SELECT publisher, year FROM edition WHERE id = ?",
                         (eid,)).fetchone()
    assert pub == "Wisdom"        # populated field never overwritten (a mismatch)
    assert yr == 2010             # empty field backfilled from the authority
    row = db.execute("SELECT status, payload_json FROM review_queue "
                     "WHERE id = ?", (item_id,)).fetchone()
    assert row[0] == "resolved"
    assert json.loads(row[1])["applied_fills"] == {"year": 2010}


def test_accept_skips_ambiguous_authority_value():
    db = _db()
    eid = _edition(db, "Book")     # publisher + year both empty
    wid = _work(db)
    _contrib(db, wid, _person(db, "A"), "author")
    _link(db, eid, wid)
    # two sources disagree on year → ambiguous → not backfilled; publisher agrees.
    v = EV.EditionVerifier(sources=[
        _StubSource("s1", tp_recs=[EV.EditionRecord("s1", title="Book",
                    authors=("A",), publisher="Wisdom", year=2010)]),
        _StubSource("s2", tp_recs=[EV.EditionRecord("s2", title="Book",
                    authors=("A",), publisher="Wisdom", year=2011)]),
    ], db=None)
    EV.verify_edition(db, v, eid)
    item_id = db.execute("SELECT id FROM review_queue").fetchone()[0]
    EV.accept_edition_verify(db, item_id)
    pub, yr = db.execute("SELECT publisher, year FROM edition WHERE id = ?",
                         (eid,)).fetchone()
    assert pub == "Wisdom"        # unambiguous → filled
    assert yr is None             # 2010 vs 2011 → left for a human


def test_reject_writes_nothing():
    db = _db()
    eid = _edition(db, "Book")
    wid = _work(db)
    _contrib(db, wid, _person(db, "A"), "author")
    _link(db, eid, wid)
    v = EV.EditionVerifier(sources=[_StubSource("s", tp_recs=[
        EV.EditionRecord("s", title="Book", authors=("A",),
                         publisher="Wisdom", year=2010)])], db=None)
    EV.verify_edition(db, v, eid)
    item_id = db.execute("SELECT id FROM review_queue").fetchone()[0]
    assert EV.reject_edition_verify(db, item_id) is True
    pub, yr = db.execute("SELECT publisher, year FROM edition WHERE id = ?",
                         (eid,)).fetchone()
    assert pub is None and yr is None
    assert db.execute("SELECT status FROM review_queue WHERE id = ?",
                      (item_id,)).fetchone()[0] == "rejected"
    # second action on a non-pending item is a no-op
    assert EV.reject_edition_verify(db, item_id) is False
    assert EV.accept_edition_verify(db, item_id) is False


# ── batch walk ────────────────────────────────────────────────────────────────────
def test_verify_all_editions_tally():
    db = _db()
    e1 = _edition(db, "Real Title", publisher="Random House", year=1999)  # mismatch
    e2 = _edition(db, "Clean Book", publisher="Penguin", year=2000)       # confirmed
    e3 = _edition(db, "Unfindable")                                       # no authority
    for e in (e1, e2, e3):
        w = _work(db)
        _contrib(db, w, _person(db, f"A{e}"), "author")
        _link(db, e, w)
    recs = {
        "Real Title": EV.EditionRecord("s", title="Real Title", authors=("A1",),
                                        publisher="Penguin", year=1999),
        "Clean Book": EV.EditionRecord("s", title="Clean Book", authors=("A2",),
                                       publisher="Penguin", year=2000),
    }

    class _Router(EV.EditionSource):
        name = "s"
        def by_title_publisher(self, title, *, publisher=None, year=None):
            r = recs.get(title)
            return [r] if r else []

    v = EV.EditionVerifier(sources=[_Router()], db=None)
    tally = EV.verify_all_editions(db, v)
    assert tally["actionable"] == 1 and tally["clean"] == 1
    assert tally["no_authority"] == 1
    assert db.execute("SELECT COUNT(*) FROM review_queue "
                      "WHERE item_type='edition_verify'").fetchone()[0] == 1
