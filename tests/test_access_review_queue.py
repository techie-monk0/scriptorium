"""Review-queue + promotion engine surface (`acc.review`) — the operator work list.

The 19 services that drive the queue (title proposals, work-authorship candidates, edition
dedup/verify, ingest promotions, …) route through this flat policy-gated repo instead of raw SQL.
Writes stage on the caller's connection (`system_conn`); the caller commits.
"""
from __future__ import annotations

import json

from catalogue.access_api import system_conn
from catalogue.db_store import init_db


def _acc(tmp_path):
    c = init_db(tmp_path / "rq.db")
    return c, system_conn(c)


def test_enqueue_get_and_typed_guard(tmp_path):
    c, acc = _acc(tmp_path)
    rid = acc.review.writes.enqueue("title_proposal", {"edition_id": 5})
    c.commit()
    row = acc.review.reads.get(rid)
    assert row["item_type"] == "title_proposal" and row["status"] == "pending"
    assert acc.review.reads.get_typed(rid, "title_proposal")[1] == "pending"
    assert acc.review.reads.get_typed(rid, "work_authorship") is None     # type guard
    assert acc.review.reads.get_typed(rid, ("title_proposal", "x"))       # tuple of types
    assert acc.review.reads.status_of(rid) == "pending"
    assert acc.review.reads.status_of(rid, "work_authorship") is None


def test_exists_pending_and_payload_likes(tmp_path):
    c, acc = _acc(tmp_path)
    acc.review.writes.enqueue("title_proposal", {"edition_id": 7, "new_title": "T"})
    c.commit()
    assert acc.review.reads.exists_pending("title_proposal", '%"edition_id": 7%')
    assert acc.review.reads.exists_pending(
        "title_proposal", '%"edition_id": 7%', '%"new_title": "T"%')
    assert not acc.review.reads.exists_pending("title_proposal", '%"edition_id": 99%')


def test_status_transitions_and_payload_update(tmp_path):
    c, acc = _acc(tmp_path)
    rid = acc.review.writes.enqueue("edition_dedup", {"a": 1})
    c.commit()
    acc.review.writes.set_payload(rid, {"a": 2})
    acc.review.writes.resolve(rid)
    c.commit()
    assert json.loads(acc.review.reads.get(rid)["payload_json"]) == {"a": 2}
    assert acc.review.reads.status_of(rid) == "resolved"
    assert c.execute("SELECT resolved_at FROM review_queue WHERE id=?", (rid,)).fetchone()[0]
    acc.review.writes.reopen(rid)
    c.commit()
    assert acc.review.reads.status_of(rid) == "pending"
    assert c.execute("SELECT resolved_at FROM review_queue WHERE id=?", (rid,)).fetchone()[0] is None


def test_pending_listings_and_delete(tmp_path):
    c, acc = _acc(tmp_path)
    a = acc.review.writes.enqueue("ingest", {"holding_id": 1})
    b = acc.review.writes.enqueue("ingest", {"holding_id": 2})
    acc.review.writes.enqueue("title_proposal", {"edition_id": 3})
    c.commit()
    assert {r[0] for r in acc.review.reads.pending_items("ingest")} == {a, b}
    assert len(acc.review.reads.pending_payloads("ingest")) == 2
    assert len(acc.review.reads.all_pending()) == 3
    acc.review.writes.delete(a)
    c.commit()
    assert acc.review.reads.get(a) is None
    n = acc.review.writes.delete_pending_of_type("ingest", '%"holding_id": 2%')
    c.commit()
    assert n == 1 and acc.review.reads.get(b) is None


def test_promotion_roundtrip(tmp_path):
    c, acc = _acc(tmp_path)
    rid = acc.review.writes.enqueue("ingest", {"holding_id": 4})
    acc.review.writes.insert_promotion(rid, 4, [10, 11], [20])
    c.commit()
    assert acc.review.reads.promotion_exists(rid)
    assert acc.review.reads.promotion(rid) == ("[10, 11]", "[20]", 4)
    assert acc.review.reads.promotion_column(rid, "holding_id") == 4
    assert acc.review.reads.promotion_rows("work_ids") == [(rid, "[10, 11]")]
    acc.review.writes.set_promotion_column(rid, "work_ids", json.dumps([10]))
    c.commit()
    assert acc.review.reads.promotion_column(rid, "work_ids") == "[10]"
    acc.review.writes.delete_promotion(review_item_id=rid)
    c.commit()
    assert not acc.review.reads.promotion_exists(rid)
