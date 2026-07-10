"""Optimistic-concurrency `rev` + idempotent create.

`rev` is a per-root version counter bumped on every update; a write planned against an old `rev` is
rejected (StaleWrite) — catching lost updates the identity fingerprint can't see (two edits to
non-identity columns). Idempotent create dedups a retried create via a client key. Spans a leaf
(Subject) and an aggregate (Edition/Person) to prove the shared mechanics. Uses the test-kit fixtures.
See db.py v5 / access_api/_crud.py.
"""
import pytest

from catalogue.contracts import Query, Ref, StaleWrite


# ── rev: lost-update prevention on non-identity columns ─────────────────────────────
def test_rev_starts_zero_and_bumps_on_update(cat_acc):
    sid = cat_acc.subjects.writes.create({"name": "Madhyamaka"}).target.id
    assert cat_acc.subjects.reads.get(sid).rev == 0
    ref = cat_acc.subjects.reads.get(sid).ref()
    cat_acc.subjects.writes.apply(cat_acc.subjects.writes.plan_update(ref, {"kind": "series"}))
    assert cat_acc.subjects.reads.get(sid).rev == 1


def test_concurrent_update_loses_on_rev_even_without_identity_change(cat_acc):
    # Edition publisher is NOT part of the identity fingerprint (title+isbn), so only `rev` catches
    # this lost update.
    eid = cat_acc.editions.writes.create({"title": "Heart Sutra"}).target.id
    base = cat_acc.editions.reads.get(eid).ref()
    plan = cat_acc.editions.writes.plan_update(base, {"publisher": "First"})
    # a concurrent write lands first, advancing rev
    cat_acc.editions.writes.apply(cat_acc.editions.writes.plan_update(base, {"publisher": "Second"}))
    with pytest.raises(StaleWrite):
        cat_acc.editions.writes.apply(plan)
    assert cat_acc.editions.reads.get(eid).publisher == "Second"   # the winner stands


def test_refetched_ref_succeeds_after_rev_bump(cat_acc):
    pid = cat_acc.persons.writes.create({"primary_name": "Nagarjuna"}).target.id
    cat_acc.persons.writes.apply(
        cat_acc.persons.writes.plan_update(cat_acc.persons.reads.get(pid).ref(), {"role_hint": "a"}))
    # re-read → fresh rev → second update applies cleanly
    cat_acc.persons.writes.apply(
        cat_acc.persons.writes.plan_update(cat_acc.persons.reads.get(pid).ref(), {"dates": "c.150"}))
    p = cat_acc.persons.reads.get(pid)
    assert p.role_hint == "a" and p.dates == "c.150" and p.rev == 2


def test_rev_survives_json_roundtrip_on_ref():
    r = Ref("edition", 5, "fp", rev=3)
    assert Ref.from_dict(r.to_dict()) == r
    assert "rev" not in Ref("edition", 5).to_dict()      # omitted when None (like fingerprint)


# ── idempotent create ───────────────────────────────────────────────────────────────
def test_idempotent_create_returns_same_row(cat_acc):
    first = cat_acc.editions.writes.create({"title": "Diamond Sutra"}, idempotency_key="imp-1")
    again = cat_acc.editions.writes.create({"title": "Diamond Sutra"}, idempotency_key="imp-1")
    assert again.op == "create_idempotent" and again.target.id == first.target.id
    assert cat_acc.editions.reads.count(Query(contains="Diamond")) == 1   # no twin row


def test_distinct_keys_create_distinct_rows(cat_acc):
    a = cat_acc.subjects.writes.create({"name": "A"}, idempotency_key="k-a")
    b = cat_acc.subjects.writes.create({"name": "B"}, idempotency_key="k-b")
    assert a.target.id != b.target.id
