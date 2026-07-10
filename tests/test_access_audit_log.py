"""Audit log — the engine records WHO did WHAT to WHICH root, WHEN, in the same transaction as the
mutation (so it commits/rolls back with it). Read back via acc.audit_trail. See entity_api_model.md §6.
"""
from __future__ import annotations

import pytest

from catalogue.contracts import Ref


def _seed_edition(conn, title="Audit Me"):
    eid = conn.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    conn.commit()
    return eid


def test_create_update_delete_are_logged(cat_conn, cat_acc):
    # create
    created = cat_acc.editions.writes.create({"title": "New Book"})
    eid = created.target.id
    # update
    cat_acc.editions.writes.apply(
        cat_acc.editions.writes.plan_update(Ref("edition", eid), {"subtitle": "Sub"}))
    # delete
    cat_acc.editions.writes.apply(cat_acc.editions.writes.plan_delete(Ref("edition", eid)))

    trail = cat_acc.audit_trail(entity_kind="edition", entity_id=eid)
    ops = [r["op"] for r in trail]
    assert ops == ["delete", "update", "create"]              # newest first
    assert all(r["principal"] == "system" for r in trail)     # the bound principal
    assert all(r["entity_kind"] == "edition" for r in trail)
    # the update row records which columns changed
    upd = next(r for r in trail if r["op"] == "update")
    assert "subtitle" in (upd["detail"] or "")


def test_audit_row_rolls_back_with_a_failed_write(cat_conn, cat_acc):
    eid = _seed_edition(cat_conn)
    before = len(cat_acc.audit_trail())
    # an update to a non-existent column is rejected at apply → the whole txn (incl. audit) rolls back
    with pytest.raises(Exception):
        imp = cat_acc.editions.writes.plan_update(Ref("edition", eid), {"title": "ok"})
        object.__setattr__(imp, "changes", {"not_a_column": "x"})   # force a bad staged change
        cat_acc.editions.writes.apply(imp)
    assert len(cat_acc.audit_trail()) == before                # no orphaned audit row


def test_audit_trail_scopes_and_limits(cat_conn, cat_acc):
    a = cat_acc.editions.writes.create({"title": "A"}).target.id
    b = cat_acc.editions.writes.create({"title": "B"}).target.id
    assert {r["entity_id"] for r in cat_acc.audit_trail(entity_kind="edition")} >= {a, b}
    assert [r["entity_id"] for r in cat_acc.audit_trail(entity_kind="edition", entity_id=a)] == [a]
    assert len(cat_acc.audit_trail(limit=1)) == 1
