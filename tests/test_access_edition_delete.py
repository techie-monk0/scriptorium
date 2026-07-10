"""Edition delete path — FK cascade + semantic orphans + non-FK file closure (reorg Phase 3).

Edition is the first aggregate with a real FK cascade, so its delete plan must cover three
closures: the cascade itself (holdings), the non-FK closure the cascade can't see (the
cascade-deleted holdings' file_hash caches + on-disk files, plus this edition's `e<id>*` cover
art — orphan-audit #3), and semantic orphans (a Work left with no edition), whose fate the
client's `OrphanPolicy` decides. System through a real DB. See entity_api_model.md §5/§6.
"""
from __future__ import annotations

import pytest

from catalogue.access_api import system_access
from catalogue.contracts import (
    GCOrphans,
    IntegrityViolation,
    OrphanDecision,
    Ref,
    RefuseOrphans,
    StaleWrite,
)
from catalogue.db_store import init_db


def _seed(tmp_path):
    """e1 owns: a solo work (→ orphan when e1 dies), a shared work (also in e2 → kept), one
    holding (file + hash cache), and cover/spine/pin art. e2 holds only the shared work."""
    db = tmp_path / "t.db"
    (tmp_path / ".cover-cache").mkdir()
    (tmp_path / "covers-pinned").mkdir()
    f = tmp_path / "book.pdf"
    f.write_text("pdf bytes")
    c = init_db(db)
    e1 = c.execute("INSERT INTO edition (title, isbn) VALUES ('Bk One', '111')").lastrowid
    e2 = c.execute("INSERT INTO edition (title, isbn) VALUES ('Bk Two', '222')").lastrowid
    w_solo = c.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    w_shared = c.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    for (e, w) in ((e1, w_solo), (e1, w_shared), (e2, w_shared)):
        c.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 0)", (e, w))
    hid = c.execute(
        "INSERT INTO holding (edition_id, file_path, file_hash, content_hash, text_status) "
        "VALUES (?, ?, 'fh1', 't:abc', 'ocr_good')", (e1, str(f))).lastrowid
    c.execute("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) VALUES ('fh1', 1, 'x')")
    c.commit()
    c.close()
    art_cache = tmp_path / ".cover-cache" / f"e{e1}.jpg"
    art_pin = tmp_path / "covers-pinned" / f"e{e1}.jpg"
    for a in (art_cache, art_pin, tmp_path / ".cover-cache" / f"spine-e{e1}.png"):
        a.write_text("img")
    return dict(db=db, e1=e1, e2=e2, w_solo=w_solo, w_shared=w_shared, hid=hid,
                f=f, art_cache=art_cache, art_pin=art_pin)


def test_plan_lists_orphans_cascade_closure_and_art(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.editions.writes.plan_delete(Ref("edition", s["e1"]))   # default FlagOrphans
        assert plan.appliable
        # only the solo work is orphaned (the shared one stays anchored by e2), and it's FLAGged
        assert {o.ref.id: o.decision for o in plan.orphans} == {s["w_solo"]: OrphanDecision.FLAG}
        # the cascade-deleted holding is listed, and its file + the edition's art are trashed
        assert any(c.id == s["hid"] for c in plan.cascades)
        trashed = {op.path for op in plan.file_ops if op.op == "trash"}
        assert {str(s["f"]), str(s["art_cache"]), str(s["art_pin"])} <= trashed
        # the cascade holding's non-FK hash cache is enumerated for purge
        assert any(rp.locator.startswith("raw_extract_cache:") for rp in plan.ref_purges)


def test_apply_gc_deletes_edition_orphan_work_caches_and_art(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.editions.writes.apply(
            acc.editions.writes.plan_delete(Ref("edition", s["e1"]), policy=GCOrphans()))
        assert acc.editions.reads.get(s["e1"]) is None              # edition tombstoned (hidden)
        assert acc.holdings.reads.get(s["hid"]) is None             # holding HARD-deleted
        # the edition row persists as a frozen tombstone (id never reused → cover refs stay safe)
        assert acc.ro.execute("SELECT deleted_at FROM edition WHERE id=?", (s["e1"],)).fetchone()[0] is not None
        # GC tombstones the orphaned work (soft-delete); the shared work stays live
        assert acc.ro.execute("SELECT deleted_at FROM work WHERE id=?", (s["w_solo"],)).fetchone()[0] is not None
        assert acc.ro.execute("SELECT deleted_at FROM work WHERE id=?", (s["w_shared"],)).fetchone()[0] is None
        # non-FK closure cleaned: cache purged, files trashed (recoverable), art removed
        assert acc.ro.execute("SELECT count(*) FROM raw_extract_cache WHERE file_hash='fh1'").fetchone()[0] == 0
        assert not s["f"].exists() and (acc.trash_dir / s["f"].name).exists()
        assert not s["art_cache"].exists() and not s["art_pin"].exists()


def test_gc_orphan_runs_the_full_work_delete_closure(tmp_path):
    """A GC'd orphan work must get the SAME closure as a direct work delete — its own pending
    work-OWNED review items are purged — while a still-live work's review items survive."""
    import json
    s = _seed(tmp_path)
    c = init_db(s["db"])
    # one work_canonical (work-owned) item per work; only the GC'd orphan's should be purged.
    c.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('work_canonical', ?)",
              (json.dumps({"work_id": s["w_solo"], "candidate_id": "bdr:W1"}),))
    c.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('work_canonical', ?)",
              (json.dumps({"work_id": s["w_shared"], "candidate_id": "bdr:W2"}),))
    c.commit(); c.close()
    with system_access(s["db"]) as acc:
        acc.editions.writes.apply(
            acc.editions.writes.plan_delete(Ref("edition", s["e1"]), policy=GCOrphans()))
        # the orphan (w_solo) was GC'd → its review item is purged; w_shared stays live → its survives
        assert acc.ro.execute("SELECT deleted_at FROM work WHERE id=?", (s["w_solo"],)).fetchone()[0] is not None
        rows = acc.ro.execute(
            "SELECT payload_json FROM review_queue WHERE item_type='work_canonical'").fetchall()
        work_ids = {json.loads(r[0])["work_id"] for r in rows}
        assert work_ids == {s["w_shared"]}   # solo's purged, shared's kept


def test_apply_flag_keeps_orphan_work(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        acc.editions.writes.apply(acc.editions.writes.plan_delete(Ref("edition", s["e1"])))
        assert acc.editions.reads.get(s["e1"]) is None
        # FLAG keeps the now-unanchored work LIVE (a later pass routes it to review)
        assert acc.ro.execute("SELECT deleted_at FROM work WHERE id=?", (s["w_solo"],)).fetchone()[0] is None


def test_refuse_orphan_blocks_apply(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.editions.writes.plan_delete(Ref("edition", s["e1"]), policy=RefuseOrphans())
        assert not plan.appliable and any(b.code == "orphan_refuse" for b in plan.blocks)
        with pytest.raises(IntegrityViolation):
            acc.editions.writes.apply(plan)
        assert acc.editions.reads.get(s["e1"]) is not None          # nothing happened


def test_no_orphan_when_all_works_shared(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        # e2's only work is shared with e1, so deleting e2 orphans nothing
        plan = acc.editions.writes.plan_delete(Ref("edition", s["e2"]))
        assert plan.orphans == ()


def test_fingerprint_mismatch_is_stale(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        plan = acc.editions.writes.plan_delete(Ref("edition", s["e1"]))
        acc.rw.execute("UPDATE edition SET title='Renamed' WHERE id=?", (s["e1"],))
        acc.rw.commit()
        with pytest.raises(StaleWrite):
            acc.editions.writes.apply(plan)


def test_reader_get_and_by_work(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        ed = acc.editions.reads.get(s["e1"])
        assert ed.title == "Bk One" and ed.isbn == "111"
        assert {e.id for e in acc.editions.reads.by_work(s["w_shared"])} == {s["e1"], s["e2"]}
