"""Holding delete path — the non-FK closure (reorg Phase 3). System through a real DB.

Deleting a holding must clean what no FK cascade can see: the file_hash-keyed caches and the
on-disk file — but only when this was the LAST holding referencing them (a shared file's caches
+ file stay live). This is the orphan-audit logic, systematized. See entity_api_model.md §6.
"""
from __future__ import annotations

from catalogue.access_api import system_access
from catalogue.contracts import Ref
from catalogue.db_store import init_db


def _seed_with_file_and_caches(tmp_path):
    f = tmp_path / "book.pdf"
    f.write_text("pdf bytes")
    p = tmp_path / "t.db"
    c = init_db(p)
    eid = c.execute("INSERT INTO edition (title) VALUES ('Bk')").lastrowid
    hid = c.execute(
        "INSERT INTO holding (edition_id, file_path, file_hash, content_hash, text_status) "
        "VALUES (?, ?, 'fh1', 't:abc', 'ocr_good')", (eid, str(f))).lastrowid
    # non-FK, hash-keyed caches that no cascade can reach
    c.execute("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) VALUES ('fh1', 1, 'txt')")
    c.execute("INSERT INTO page_text_cache (file_hash, extract_version, page_no, text) VALUES ('fh1', 1, 1, 'p1')")
    c.commit()
    c.close()
    return p, hid, f


def test_plan_delete_enumerates_file_and_cache_purges(tmp_path):
    p, hid, f = _seed_with_file_and_caches(tmp_path)
    with system_access(p) as acc:
        plan = acc.holdings.writes.plan_delete(Ref("holding", hid))
        assert plan.op == "delete" and plan.appliable
        assert any(op.op == "trash" and op.path == str(f) for op in plan.file_ops)
        purged_tables = {rp.locator.split(":")[0] for rp in plan.ref_purges}
        assert {"raw_extract_cache", "page_text_cache"} <= purged_tables


def test_apply_delete_removes_row_purges_caches_and_trashes_file(tmp_path):
    p, hid, f = _seed_with_file_and_caches(tmp_path)
    with system_access(p) as acc:
        acc.holdings.writes.apply(acc.holdings.writes.plan_delete(Ref("holding", hid)))
        assert acc.holdings.reads.get(hid) is None                        # row gone
        assert acc.ro.execute("SELECT count(*) FROM raw_extract_cache WHERE file_hash='fh1'").fetchone()[0] == 0
        assert acc.ro.execute("SELECT count(*) FROM page_text_cache WHERE file_hash='fh1'").fetchone()[0] == 0
        assert not f.exists()                                             # file trashed,
        assert (acc.trash_dir / f.name).exists()                         # not vaporized


def test_shared_file_hash_keeps_caches_and_file(tmp_path):
    p, hid, f = _seed_with_file_and_caches(tmp_path)
    with system_access(p) as acc:
        # a SECOND holding of the same file (same hash + path)
        acc.rw.execute(
            "INSERT INTO holding (edition_id, file_path, file_hash, content_hash, text_status) "
            "SELECT edition_id, file_path, file_hash, 't:abc2', text_status FROM holding WHERE id = ?",
            (hid,))
        acc.rw.commit()
        plan = acc.holdings.writes.plan_delete(Ref("holding", hid))
        assert plan.ref_purges == () and plan.file_ops == ()             # nothing orphaned
        acc.holdings.writes.apply(plan)
        assert acc.ro.execute("SELECT count(*) FROM raw_extract_cache WHERE file_hash='fh1'").fetchone()[0] == 1
        assert f.exists()                                                # still referenced → kept
