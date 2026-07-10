"""Holding write path — plan → apply (reorg Phase 3). System + unit through a real DB.

Exercises the pipeline: plan (read) → blocks on invalid input → apply (WRITE-gated) →
integrity re-check (fingerprint/StaleWrite) → transactional update. See entity_api_model.md §5.
"""
from __future__ import annotations

import pytest

from catalogue.access_api import bind, system_access
from catalogue.contracts import (
    AccessMode,
    Denied,
    Impact,
    IntegrityViolation,
    Policy,
    Principal,
    Ref,
    StaleWrite,
    ValidationError,
)
from catalogue.db_store import init_db


def _seed(tmp_path):
    p = tmp_path / "t.db"
    c = init_db(p)
    eid = c.execute("INSERT INTO edition (title) VALUES ('Bk')").lastrowid
    hid = c.execute("INSERT INTO holding (edition_id, file_path, content_hash, text_status) "
                    "VALUES (?, '/lib/a.pdf', 't:abc', 'ocr_poor')", (eid,)).lastrowid
    c.commit()
    c.close()
    return p, hid


def test_plan_then_apply_updates_text_status(tmp_path):
    p, hid = _seed(tmp_path)
    with system_access(p) as acc:
        plan = acc.holdings.writes.plan_set_text_status(Ref("holding", hid), "ocr_good")
        assert plan.appliable and plan.op == "update"
        assert plan.target.fingerprint == "t:abc" and plan.changes == {"text_status": "ocr_good"}
        applied = acc.holdings.writes.apply(plan)
        assert applied.op == "update" and applied.appliable   # returns the receipt
        assert acc.holdings.reads.get(hid).text_status == "ocr_good"   # persisted


def test_plan_blocks_an_invalid_status_and_apply_refuses(tmp_path):
    p, hid = _seed(tmp_path)
    with system_access(p) as acc:
        plan = acc.holdings.writes.plan_set_text_status(Ref("holding", hid), "bogus")
        assert not plan.appliable and plan.blocks[0].code == "validation"
        with pytest.raises(IntegrityViolation):
            acc.holdings.writes.apply(plan)


def test_plan_blocks_a_missing_holding(tmp_path):
    p, _ = _seed(tmp_path)
    with system_access(p) as acc:
        plan = acc.holdings.writes.plan_set_text_status(Ref("holding", 99999), "ocr_good")
        assert not plan.appliable and plan.blocks[0].code == "not_found"


def test_write_requires_write_authorization(tmp_path):
    p, hid = _seed(tmp_path)

    class ReadOnly(Policy):       # viewer: reads allowed, writes denied
        def allows(self, principal, action):
            return action.mode is AccessMode.READ

    with bind(Principal(id="viewer"), ReadOnly(), p) as acc:
        plan = acc.holdings.writes.plan_set_text_status(Ref("holding", hid), "ocr_good")  # READ ok
        with pytest.raises(Denied):
            acc.holdings.writes.apply(plan)                                                # WRITE denied


def test_stale_write_is_caught_by_fingerprint(tmp_path):
    p, hid = _seed(tmp_path)
    with system_access(p) as acc:
        plan = acc.holdings.writes.plan_set_text_status(Ref("holding", hid), "ocr_good")
        # a concurrent change moves the holding's content fingerprint after the plan was built
        acc.rw.execute("UPDATE holding SET content_hash = 't:NEW' WHERE id = ?", (hid,))
        acc.rw.commit()
        with pytest.raises(StaleWrite):
            acc.holdings.writes.apply(plan)


def test_apply_rejects_non_updatable_columns(tmp_path):
    p, hid = _seed(tmp_path)
    with system_access(p) as acc:
        crafted = Impact("update", Ref("holding", hid, "t:abc"), changes={"edition_id": 7})
        with pytest.raises(ValidationError):
            acc.holdings.writes.apply(crafted)
