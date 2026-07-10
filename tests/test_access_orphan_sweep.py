"""OrphanSweep — the access-API health backstop that reconciles the non-FK registry.

Every store that keeps an entity id OUTSIDE a foreign key is declared in access_api.registry, so this
sweep can SEE it: hash caches with no holding, pending review items / promotion ids whose owner is
gone, and cover art whose edition is gone. scan() reports; apply() fixes (DB in one txn, art trashed
after). Existence — not soft-delete — is the test: a tombstone freezes the id, so it's left alone.
Uses the test-kit cat_conn/cat_acc fixtures. See entity_api_model.md §6.
"""
import json
import os

import pytest

from catalogue.contracts import Denied
from catalogue.test_kit import DenyAll


def _seed(conn):
    eid = conn.execute("INSERT INTO edition (title) VALUES ('Live Ed')").lastrowid
    hid = conn.execute("INSERT INTO holding (edition_id, form, file_hash) "
                       "VALUES (?, 'electronic', 'live-hash')", (eid,)).lastrowid
    wid = conn.execute("INSERT INTO work (canonical_system) VALUES ('toh')").lastrowid
    pid = conn.execute("INSERT INTO person (primary_name) VALUES ('P')").lastrowid
    # hash caches: one referenced by a holding (kept), one orphaned (audit #5)
    conn.execute("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
                 "VALUES ('live-hash', 1, 'x')")
    conn.execute("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
                 "VALUES ('dead-hash', 1, 'y')")
    # review items: orphan (gone work) + kept (live work) — both work-OWNED
    orphan = conn.execute("INSERT INTO review_queue (item_type, payload_json) "
                          "VALUES ('work_canonical', ?)", (json.dumps({"work_id": 99999}),)).lastrowid
    kept = conn.execute("INSERT INTO review_queue (item_type, payload_json) "
                        "VALUES ('work_canonical', ?)", (json.dumps({"work_id": wid}),)).lastrowid
    # promotion array carrying a gone person id (88888) alongside the live one; its ingest item
    # points at a LIVE holding so the item itself isn't flagged (only the array entry is)
    promo = conn.execute("INSERT INTO review_queue (item_type, payload_json) "
                         "VALUES ('ingest', ?)", (json.dumps({"holding_id": hid}),)).lastrowid
    conn.execute("INSERT INTO promotion (review_item_id, work_ids, person_ids) VALUES (?, '[]', ?)",
                 (promo, json.dumps([pid, 88888])))
    conn.commit()
    return dict(eid=eid, hid=hid, wid=wid, pid=pid, orphan=orphan, kept=kept, promo=promo)


def _art(acc, live_eid, gone_eid):
    os.makedirs(acc.cover_cache, exist_ok=True)
    live = os.path.join(acc.cover_cache, f"e{live_eid}.jpg")
    gone = os.path.join(acc.cover_cache, f"e{gone_eid}.jpg")
    for p in (live, gone):
        with open(p, "wb") as fh:
            fh.write(b"\xff\xd8\xff")
    return live, gone


def test_scan_reports_every_orphan_class(cat_conn, cat_acc):
    s = _seed(cat_conn)
    live_art, gone_art = _art(cat_acc, s["eid"], 99999)
    rep = cat_acc.health.scan()
    assert ("raw_extract_cache", "dead-hash") in rep["hash_cache_orphans"]
    assert ("raw_extract_cache", "live-hash") not in rep["hash_cache_orphans"]
    assert [it["id"] for it in rep["review_item_orphans"]] == [s["orphan"]]
    assert rep["promotion_orphans"] == [
        {"review_item_id": s["promo"], "column": "person_ids", "removed": [88888]}]
    assert gone_art in rep["cover_art_orphans"] and live_art not in rep["cover_art_orphans"]


def test_apply_cleans_everything(cat_conn, cat_acc):
    s = _seed(cat_conn)
    live_art, gone_art = _art(cat_acc, s["eid"], 99999)
    res = cat_acc.health.apply(cat_acc.health.scan())
    assert res == {"hash_caches_purged": 1, "review_items_dropped": 1,
                   "promotion_arrays_scrubbed": 1, "cover_art_trashed": 1}
    q = lambda sql, *a: cat_conn.execute(sql, a).fetchone()[0]
    assert q("SELECT count(*) FROM raw_extract_cache WHERE file_hash='dead-hash'") == 0
    assert q("SELECT count(*) FROM raw_extract_cache WHERE file_hash='live-hash'") == 1
    assert q("SELECT count(*) FROM review_queue WHERE id=?", s["orphan"]) == 0
    assert q("SELECT count(*) FROM review_queue WHERE id=?", s["kept"]) == 1
    left = json.loads(cat_conn.execute(
        "SELECT person_ids FROM promotion WHERE review_item_id=?", (s["promo"],)).fetchone()[0])
    assert left == [s["pid"]]
    assert not os.path.exists(gone_art) and os.path.exists(live_art)


def test_scan_leaves_tombstoned_owner_alone(cat_conn, cat_acc):
    wid = cat_conn.execute("INSERT INTO work (canonical_system) VALUES ('toh')").lastrowid
    cat_conn.execute("UPDATE work SET deleted_at = datetime('now') WHERE id=?", (wid,))
    item = cat_conn.execute("INSERT INTO review_queue (item_type, payload_json) "
                            "VALUES ('work_canonical', ?)", (json.dumps({"work_id": wid}),)).lastrowid
    cat_conn.commit()
    assert cat_acc.health.scan()["review_item_orphans"] == []   # id frozen → not a reuse hazard
    assert cat_conn.execute("SELECT count(*) FROM review_queue WHERE id=?", (item,)).fetchone()[0] == 1


def test_scan_is_read_gated(cat_acc):
    cat_acc.policy = DenyAll()
    with pytest.raises(Denied):
        cat_acc.health.scan()


def test_cover_art_orphans_public_scan(cat_conn, cat_acc):
    # the covers-only slice the cli cover sweep uses (Phase-4 conversion off raw SQL)
    eid = cat_conn.execute("INSERT INTO edition (title) VALUES ('E')").lastrowid
    cat_conn.commit()
    live, gone = _art(cat_acc, eid, 4242)
    orphans = cat_acc.health.cover_art_orphans()
    assert gone in orphans and live not in orphans


def test_dangling_ref_orphans_slice(cat_conn, cat_acc):
    # the reference-store-only slice the cli sweep_dangling_refs uses (Phase-4 conversion off raw SQL):
    # just review items + promotion arrays, NO hash-cache / cover-art keys.
    s = _seed(cat_conn)
    _art(cat_acc, s["eid"], 99999)            # a cover-art orphan the slice must IGNORE
    slice_ = cat_acc.health.dangling_ref_orphans()
    assert set(slice_) == {"review_item_orphans", "promotion_orphans"}
    assert [it["id"] for it in slice_["review_item_orphans"]] == [s["orphan"]]
    assert slice_["promotion_orphans"] == [
        {"review_item_id": s["promo"], "column": "person_ids", "removed": [88888]}]


def test_apply_scoped_report_scrubs_only_refs(cat_conn, cat_acc):
    # applying a report built from the slice (hash/cover empty) touches ONLY the two ref classes —
    # the dead hash-cache row and the orphaned cover-art file are left alone.
    s = _seed(cat_conn)
    _, gone_art = _art(cat_acc, s["eid"], 99999)
    slice_ = cat_acc.health.dangling_ref_orphans()
    res = cat_acc.health.apply({"hash_cache_orphans": [], "cover_art_orphans": [], **slice_})
    assert res == {"hash_caches_purged": 0, "review_items_dropped": 1,
                   "promotion_arrays_scrubbed": 1, "cover_art_trashed": 0}
    q = lambda sql, *a: cat_conn.execute(sql, a).fetchone()[0]
    assert q("SELECT count(*) FROM review_queue WHERE id=?", s["orphan"]) == 0
    assert q("SELECT count(*) FROM raw_extract_cache WHERE file_hash='dead-hash'") == 1   # untouched
    assert os.path.exists(gone_art)                                                       # untouched
