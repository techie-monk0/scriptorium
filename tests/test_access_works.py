"""Work aggregate — read + soft-delete + merge (LinkRepoint) + the over-purge-safe non-FK closure.

Work is the hub of the FRBR graph, so its writer carries two ops: `delete` (tombstone; edges ride
along; purge ONLY work-owned review/promotion refs) and `merge` (re-point every edge onto the
winner, backfill scalars, re-point work-owned refs, tombstone the loser). The headline regression:
deleting/merging a work must NOT touch an edition-owned `title_proposal` that merely carries a
secondary `work_id` (the ~254-item over-purge). System through a real DB. See entity_api_model.md §5/§6.
"""
from __future__ import annotations

import json

import pytest

from catalogue.access_api import system_access
from catalogue.contracts import (
    IntegrityViolation,
    Ref,
    StaleWrite,
    Work,
    work_fingerprint,
)
from catalogue.db_store import fold_key, init_db


def _alias(c, wid, text):
    # normalized_key carries fold_key(text) — the production invariant the merge dedup relies on.
    c.execute("INSERT INTO work_alias (work_id, text, normalized_key) VALUES (?, ?, ?)",
              (wid, text, fold_key(text)))


def _seed(tmp_path):
    """Two duplicate works of one composition (w_lose, w_win), each in its own edition, sharing an
    author. w_lose also carries a subject link, an extra alias, a work-owned `work_canonical` review
    item, and a promotion row — all of which a merge must re-point. A separate edition-owned
    `title_proposal` carries w_win's id as a SECONDARY ref (must survive a w_win delete/merge)."""
    db = tmp_path / "t.db"
    c = init_db(db)
    e1 = c.execute("INSERT INTO edition (title, isbn) VALUES ('Bk One', '111')").lastrowid
    e2 = c.execute("INSERT INTO edition (title, isbn) VALUES ('Bk Two', '222')").lastrowid
    p = c.execute("INSERT INTO person (primary_name) VALUES ('Author A')").lastrowid
    subj = c.execute("INSERT INTO subject (name) VALUES ('Madhyamaka')").lastrowid
    w_win = c.execute("INSERT INTO work (canonical_system) VALUES ('toh')").lastrowid
    w_lose = c.execute("INSERT INTO work (canonical_number) VALUES ('123')").lastrowid
    _alias(c, w_win, "Bodhicaryavatara")
    _alias(c, w_lose, "Bodhicaryavatara")           # dup key → deduped on merge
    _alias(c, w_lose, "Way of the Bodhisattva")     # unique → gained on merge
    for w, e in ((w_win, e1), (w_lose, e2)):
        c.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 0)", (e, w))
        c.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (w, p))
    c.execute("INSERT INTO work_subject (work_id, subject_id) VALUES (?, ?)", (w_lose, subj))
    # work-OWNED review item pointing at the loser (must re-point/purge with the work)
    rq_work = c.execute(
        "INSERT INTO review_queue (item_type, payload_json) VALUES ('work_canonical', ?)",
        (json.dumps({"work_id": w_lose, "candidate_id": "toh1"}),)).lastrowid
    # edition-OWNED item carrying w_win as a SECONDARY work_id (the over-purge trap — must survive)
    rq_edition = c.execute(
        "INSERT INTO review_queue (item_type, payload_json) VALUES ('title_proposal', ?)",
        (json.dumps({"edition_id": e1, "work_id": w_win, "title": "New"}),)).lastrowid
    # a promotion record anchored to a holding-owned item (survives a work delete/merge), so its
    # work_ids array is scrubbed/re-pointed in place — not cascade-removed with a purged work item.
    rq_promo = c.execute(
        "INSERT INTO review_queue (item_type, payload_json) VALUES ('ingest', ?)",
        (json.dumps({"holding_id": 0}),)).lastrowid
    c.execute("INSERT INTO promotion (review_item_id, work_ids, person_ids) VALUES (?, ?, '[]')",
              (rq_promo, json.dumps([w_lose, w_win])))
    c.commit()
    c.close()
    return dict(db=db, e1=e1, e2=e2, p=p, subj=subj, w_win=w_win, w_lose=w_lose,
                rq_work=rq_work, rq_edition=rq_edition, rq_promo=rq_promo)


# ── reads + fingerprint ─────────────────────────────────────────────────────────────
def test_reader_get_and_by_edition(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        w = acc.works.reads.get(s["w_win"])
        assert w.title == "Bodhicaryavatara" and w.canonical_system == "toh"
        assert w.author_ids == (s["p"],)
        assert [w.id for w in acc.works.reads.by_edition(s["e2"])] == [s["w_lose"]]


def test_fingerprint_folds_title_and_pins_authors(tmp_path):
    assert work_fingerprint(" Way  of  the   Bodhisattva ", (2, 1)) == \
        work_fingerprint("way of the bodhisattva", (1, 2))         # fold + author order-independent
    assert work_fingerprint("X", (1,)) != work_fingerprint("X", (1, 2))
    assert Work(1, "X", author_ids=(1,)).ref().kind == "work"


# ── soft-delete ─────────────────────────────────────────────────────────────────────
def test_delete_tombstones_then_restore(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.works.writes.apply(acc.works.writes.plan_delete(Ref("work", s["w_lose"])))
        assert acc.works.reads.get(s["w_lose"]) is None
        assert acc.ro.execute("SELECT deleted_at FROM work WHERE id=?", (s["w_lose"],)).fetchone()[0] is not None
        acc.works.writes.restore(Ref("work", s["w_lose"]))
        assert acc.works.reads.get(s["w_lose"]).title == "Bodhicaryavatara"


def test_fingerprint_mismatch_is_stale(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.works.writes.plan_delete(Ref("work", s["w_lose"]))
        acc.rw.execute("DELETE FROM work_author WHERE work_id=?", (s["w_lose"],))   # author set changes
        acc.rw.commit()
        with pytest.raises(StaleWrite):
            acc.works.writes.apply(plan)


# ── over-purge regression (the headline) ─────────────────────────────────────────────
def test_delete_purges_only_work_owned_refs(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.works.writes.plan_delete(Ref("work", s["w_lose"]))
        # plan enumerates the work-owned review item + promotion row, nothing edition-owned
        locs = {p.locator for p in plan.ref_purges}
        assert f"review_queue:{s['rq_work']}" in locs
        assert f"promotion.work_ids:{s['rq_promo']}" in locs
        acc.works.writes.apply(plan)
        # work-owned item purged; promotion array scrubbed of the dead work (w_win stays)
        assert acc.ro.execute("SELECT count(*) FROM review_queue WHERE id=?", (s["rq_work"],)).fetchone()[0] == 0
        assert json.loads(acc.ro.execute(
            "SELECT work_ids FROM promotion WHERE review_item_id=?", (s["rq_promo"],)).fetchone()[0]) == [s["w_win"]]
        # the edition-owned title_proposal (secondary work_id) SURVIVES — no over-purge
        assert acc.ro.execute("SELECT count(*) FROM review_queue WHERE id=?", (s["rq_edition"],)).fetchone()[0] == 1


# ── merge ─────────────────────────────────────────────────────────────────────────
def test_plan_merge_previews_repoints_and_gains(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.works.writes.plan_merge(Ref("work", s["w_lose"]), Ref("work", s["w_win"]))
        assert plan.appliable
        edges = {lr.edge for lr in plan.link_repoints}
        assert {"edition_work", "work_author", "work_subject", "work_alias", "review_queue.work_id",
                "promotion.work_ids"} <= edges
        assert plan.changes["aliases_gained"] == ["Way of the Bodhisattva"]
        assert plan.changes["canonical_after"] == ["toh", "123"]      # winner system + loser number


def test_apply_merge_repoints_edges_and_tombstones_loser(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.works.writes.apply(
            acc.works.writes.plan_merge(Ref("work", s["w_lose"]), Ref("work", s["w_win"])))
        assert acc.works.reads.get(s["w_lose"]) is None              # loser tombstoned
        win = acc.works.reads.get(s["w_win"])
        assert win.canonical_number == "123"                         # backfilled from loser
        # both editions now point at the winner; loser holds no edges
        assert {w.id for w in acc.works.reads.by_edition(s["e1"])} == {s["w_win"]}
        assert {w.id for w in acc.works.reads.by_edition(s["e2"])} == {s["w_win"]}
        assert acc.ro.execute("SELECT count(*) FROM edition_work WHERE work_id=?", (s["w_lose"],)).fetchone()[0] == 0
        assert acc.ro.execute("SELECT count(*) FROM work_subject WHERE work_id=?", (s["w_win"],)).fetchone()[0] == 1
        # alias deduped (one "Bodhicaryavatara") + gained ("Way of the Bodhisattva") = 2 on winner
        assert acc.ro.execute("SELECT count(*) FROM work_alias WHERE work_id=?", (s["w_win"],)).fetchone()[0] == 2
        # work-owned review item re-pointed onto the winner (decision survives, not purged)
        payload = json.loads(acc.ro.execute(
            "SELECT payload_json FROM review_queue WHERE id=?", (s["rq_work"],)).fetchone()[0])
        assert payload["work_id"] == s["w_win"]
        assert json.loads(acc.ro.execute(
            "SELECT work_ids FROM promotion WHERE review_item_id=?", (s["rq_promo"],)).fetchone()[0]) == [s["w_win"]]


def test_merge_leaves_edition_owned_ref_untouched(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        # merge INTO w_win; the title_proposal carries w_win as a secondary ref — must not change
        acc.works.writes.apply(
            acc.works.writes.plan_merge(Ref("work", s["w_lose"]), Ref("work", s["w_win"])))
        payload = json.loads(acc.ro.execute(
            "SELECT payload_json FROM review_queue WHERE id=?", (s["rq_edition"],)).fetchone()[0])
        assert payload["work_id"] == s["w_win"] and payload["edition_id"] == s["e1"]


def test_merge_self_is_blocked(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.works.writes.plan_merge(Ref("work", s["w_win"]), Ref("work", s["w_win"]))
        assert not plan.appliable and any(b.code == "invalid" for b in plan.blocks)
        with pytest.raises(IntegrityViolation):
            acc.works.writes.apply(plan)


def test_merge_canonical_conflict_is_blocked(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.rw.execute("UPDATE work SET canonical_number='999' WHERE id=?", (s["w_win"],))
        acc.rw.commit()
        plan = acc.works.writes.plan_merge(Ref("work", s["w_lose"]), Ref("work", s["w_win"]))
        assert not plan.appliable and any(b.code == "conflict" for b in plan.blocks)


def test_merge_into_missing_winner_is_blocked(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.works.writes.plan_merge(Ref("work", s["w_lose"]), Ref("work", 9999))
        assert not plan.appliable and any(b.code == "not_found" for b in plan.blocks)


def test_merge_rechecks_winner_fingerprint(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.works.writes.plan_merge(Ref("work", s["w_lose"]), Ref("work", s["w_win"]))
        acc.rw.execute("UPDATE work_alias SET text='Renamed', normalized_key='renamed' "
                       "WHERE work_id=? ", (s["w_win"],))             # winner identity drifts
        acc.rw.commit()
        with pytest.raises(StaleWrite):
            acc.works.writes.apply(plan)


# ── Session staging ───────────────────────────────────────────────────────────────
def test_session_stages_merge(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        with acc.session() as sess:
            sess.stage(acc.works.writes,
                       acc.works.writes.plan_merge(Ref("work", s["w_lose"]), Ref("work", s["w_win"])))
        assert acc.works.reads.get(s["w_lose"]) is None
        assert {w.id for w in acc.works.reads.by_edition(s["e2"])} == {s["w_win"]}
