"""create / update / Query for the non-leaf aggregates (Edition / Person / Work).

The same mechanics the leaf engine has, lifted into `_crud` and reused by the aggregates' own
reads/writes (which keep their cascades/merge/orphans): gate (normalize + validate) → plan→apply with
a fingerprint recheck (StaleWrite), and a paginated substring `list`/`count` (the Query contract).
Work is SCALAR-only (its title/authors are edges); its Query searches the representative alias.
Uses the test-kit fixtures. See access_api/_crud.py.
"""
import pytest

from catalogue.contracts import Denied, IntegrityViolation, Query, StaleWrite
from catalogue.test_kit import DenyAll


# ── Edition ───────────────────────────────────────────────────────────────────────
def test_edition_create_normalizes_then_get(cat_acc):
    imp = cat_acc.editions.writes.create({"title": "  The   Way ", "isbn": "111"})
    assert imp.op == "create" and imp.target.id > 0
    e = cat_acc.editions.reads.get(imp.target.id)
    assert e.title == "The Way" and e.isbn == "111"        # whitespace collapsed


def test_edition_create_requires_title(cat_acc):
    assert not cat_acc.editions.writes.plan_create({"isbn": "x"}).appliable
    with pytest.raises(IntegrityViolation):
        cat_acc.editions.writes.create({"isbn": "x"})


def test_edition_update_and_stale_recheck(cat_acc):
    eid = cat_acc.editions.writes.create({"title": "Stable"}).target.id
    ref = cat_acc.editions.reads.get(eid).ref()
    cat_acc.editions.writes.apply(cat_acc.editions.writes.plan_update(ref, {"publisher": "Wisdom"}))
    assert cat_acc.editions.reads.get(eid).publisher == "Wisdom"
    # a stale plan (identity drifted) is rejected
    plan = cat_acc.editions.writes.plan_update(cat_acc.editions.reads.get(eid).ref(), {"year": 2020})
    cat_acc.rw.execute("UPDATE edition SET title='Drift' WHERE id=?", (eid,))
    cat_acc.rw.commit()
    with pytest.raises(StaleWrite):
        cat_acc.editions.writes.apply(plan)


def test_edition_list_paginates_and_filters(cat_acc):
    for t in ("Heart Sutra", "Diamond Sutra", "Lotus"):
        cat_acc.editions.writes.create({"title": t})
    assert cat_acc.editions.reads.count(Query(contains="sutra")) == 2
    assert len(cat_acc.editions.reads.list(Query(contains="sutra", limit=1))) == 1


def test_edition_create_denied(cat_acc):
    cat_acc.policy = DenyAll()
    with pytest.raises(Denied):
        cat_acc.editions.writes.create({"title": "X"})


# ── Person ────────────────────────────────────────────────────────────────────────
def test_person_create_and_update(cat_acc):
    imp = cat_acc.persons.writes.create({"primary_name": "Nagarjuna", "dates": "c.150"})
    p = cat_acc.persons.reads.get(imp.target.id)
    assert p.primary_name == "Nagarjuna" and p.dates == "c.150"
    cat_acc.persons.writes.apply(cat_acc.persons.writes.plan_update(p.ref(), {"role_hint": "author"}))
    assert cat_acc.persons.reads.get(imp.target.id).role_hint == "author"


def test_person_create_requires_name(cat_acc):
    with pytest.raises(IntegrityViolation):
        cat_acc.persons.writes.create({"dates": "x"})


def test_person_list_filters_by_name(cat_acc):
    for n in ("Tsongkhapa", "Tsong Khapa (variant)", "Atisha"):
        cat_acc.persons.writes.create({"primary_name": n})
    assert cat_acc.persons.reads.count(Query(contains="tsong")) == 2


# ── Work (scalar columns; Query by representative alias) ────────────────────────────
def test_work_create_scalar_then_update(cat_acc):
    imp = cat_acc.works.writes.create({"canonical_system": "toh", "canonical_number": "1"})
    w = cat_acc.works.reads.get(imp.target.id)
    assert w.canonical_system == "toh" and w.title is None        # no alias yet
    cat_acc.works.writes.apply(cat_acc.works.writes.plan_update(w.ref(), {"canonical_number": "9"}))
    assert cat_acc.works.reads.get(imp.target.id).canonical_number == "9"


def test_work_list_queries_by_alias(cat_conn, cat_acc):
    wid = cat_conn.execute("INSERT INTO work (canonical_system) VALUES ('toh')").lastrowid
    cat_conn.execute("INSERT INTO work_alias (work_id, text, normalized_key) "
                     "VALUES (?, 'Bodhicaryavatara', 'bodhicaryavatara')", (wid,))
    cat_conn.commit()
    assert cat_acc.works.reads.count(Query(contains="bodhi")) == 1
    got = cat_acc.works.reads.list(Query(contains="bodhi"))
    assert got[0].id == wid and got[0].title == "Bodhicaryavatara"
