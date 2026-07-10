"""Leaf entity create / update / paginated list through the bound gateway.

The generic leaf engine gained the write half of CRUD: plan_create/create + plan_update/apply (gated
by the IntegrityGate), and a paginated, substring-filtered `list`/`count` (the Query contract).
Subject/Collection/Tradition all get it from one engine. Identity is still pinned: update rechecks
the target fingerprint (StaleWrite). Uses the test-kit fixtures. See access_api/_leaf.py.
"""
import pytest

from catalogue.contracts import Denied, IntegrityViolation, Query, StaleWrite
from catalogue.test_kit import DenyAll


def test_create_then_get(cat_acc):
    impact = cat_acc.subjects.writes.create({"name": "Madhyamaka"})
    assert impact.op == "create" and impact.target.id > 0
    s = cat_acc.subjects.reads.get(impact.target.id)
    assert s.name == "Madhyamaka" and s.kind == "topic"      # kind defaulted by the DB


def test_create_normalizes_whitespace(cat_acc):
    sid = cat_acc.subjects.writes.create({"name": "  Pure   Land "}).target.id
    assert cat_acc.subjects.reads.get(sid).name == "Pure Land"


def test_create_requires_name(cat_acc):
    assert not cat_acc.subjects.writes.plan_create({"kind": "series"}).appliable
    with pytest.raises(IntegrityViolation):
        cat_acc.subjects.writes.create({"kind": "series"})


def test_create_rejects_unknown_field(cat_acc):
    with pytest.raises(IntegrityViolation):
        cat_acc.subjects.writes.create({"name": "X", "color": "red"})


def test_update_changes_a_field(cat_acc):
    sid = cat_acc.subjects.writes.create({"name": "Old"}).target.id
    ref = cat_acc.subjects.reads.get(sid).ref()
    cat_acc.subjects.writes.apply(cat_acc.subjects.writes.plan_update(ref, {"name": "New"}))
    assert cat_acc.subjects.reads.get(sid).name == "New"


def test_update_rechecks_fingerprint(cat_acc):
    sid = cat_acc.subjects.writes.create({"name": "Stable"}).target.id
    plan = cat_acc.subjects.writes.plan_update(cat_acc.subjects.reads.get(sid).ref(), {"name": "Changed"})
    cat_acc.rw.execute("UPDATE subject SET name='Drift' WHERE id=?", (sid,))   # identity drifts
    cat_acc.rw.commit()
    with pytest.raises(StaleWrite):
        cat_acc.subjects.writes.apply(plan)


def test_list_paginates_and_filters(cat_acc):
    for n in ("Madhyamaka", "Yogacara", "Madhyamika School"):
        cat_acc.subjects.writes.create({"name": n})
    assert cat_acc.subjects.reads.count(Query(contains="madhy")) == 2     # case-insensitive
    assert len(cat_acc.subjects.reads.list(Query(contains="madhy", limit=1))) == 1
    names = {s.name for s in cat_acc.subjects.reads.list(Query(contains="madhy", limit=10))}
    assert names == {"Madhyamaka", "Madhyamika School"}


def test_create_denied_by_policy(cat_acc):
    cat_acc.policy = DenyAll()
    with pytest.raises(Denied):
        cat_acc.subjects.writes.create({"name": "X"})


def test_collection_and_tradition_share_the_engine(cat_acc):
    c = cat_acc.collections.writes.create({"name": "Kangyur"})
    assert cat_acc.collections.reads.get(c.target.id).name == "Kangyur"
    # 'Bön' is outside the config-seeded vocab (vocab.json `_tradition`), so create doesn't
    # collide with a seeded row on the UNIQUE(name) constraint.
    t = cat_acc.traditions.writes.create({"name": "Bön"})
    assert cat_acc.traditions.reads.get(t.target.id).name == "Bön"
