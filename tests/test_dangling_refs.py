"""Non-FK reference cleanup on entity delete + the id-reuse guards.

When an edition/work/person is deleted, the FK cascade can't reach references the database
can't see: cover-art FILES keyed by id, and ids embedded in `review_queue.payload_json` /
`promotion` JSON. Because SQLite recycles primary keys, a leftover pending review item (or
cover file) gets inherited by whoever next takes the freed id. These tests pin the purge on
delete (dangling_refs + covers.purge_edition_art) and the validate-at-consume guards that
backstop them (verify.person_identity_ok)."""
import json

import pytest

from catalogue.db_store import init_db
from catalogue.services import (contributor_edit, covers, dangling_refs,
                                entity_undo as EU, person_work, verify)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "c.db")
    yield conn
    conn.close()


def _edition(db, title="A Book"):
    eid = db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    hid = db.execute("INSERT INTO holding (edition_id, form, file_path) "
                     "VALUES (?, 'electronic', ?)", (eid, f"/{title}.pdf")).lastrowid
    return eid, hid


def _work(db, english="A Work"):
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, ?, 'english', ?)", (wid, english, english.lower()))
    return wid


def _person(db, name="Jane Doe"):
    return db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid


def _queue(db, item_type, payload):
    return db.execute("INSERT INTO review_queue (item_type, payload_json) VALUES (?, ?)",
                      (item_type, json.dumps(payload))).lastrowid


def _pending(db):
    return db.execute("SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0]


# ── review_queue / promotion purge on delete ─────────────────────────────────────
def test_delete_edition_purges_pending_items_by_edition_and_holding(db):
    eid, hid = _edition(db)
    _queue(db, "edition_metadata", {"edition_id": eid, "work_id": 999})
    _queue(db, "book_toc_pattern", {"holding_id": hid})
    keep = _queue(db, "edition_metadata", {"edition_id": eid + 12345})   # unrelated
    assert _pending(db) == 3
    EU.delete_edition(db, eid)
    assert _pending(db) == 1
    assert db.execute("SELECT id FROM review_queue WHERE id=?", (keep,)).fetchone()


def test_holding_id_not_confused_by_substring(db):
    # holding_id 5 must not also drop an item keyed on holding_id 51 (loose-match guard).
    eid, hid = _edition(db)                      # hid is some int
    other = _queue(db, "book_toc_pattern", {"holding_id": int(f"{hid}9")})
    EU.delete_edition(db, eid)
    assert db.execute("SELECT id FROM review_queue WHERE id=?", (other,)).fetchone()


def test_delete_work_purges_only_work_owned_items(db):
    """A work delete scrubs the items it OWNS (work_canonical/work_authorship) but LEAVES an
    edition-owned title_proposal that merely carries a SECONDARY work_id — the ~254-item over-purge
    fix (registry.REVIEW_ITEM_OWNERS), now matched on the legacy delete path too."""
    wid = _work(db)
    # carrier edition holds wid AND another work, so wid isn't anyone's SOLE work (guard)
    e2, _ = _edition(db, "Carrier")
    other = _work(db, "Other Work")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (e2, wid))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,2)", (e2, other))
    owned = _queue(db, "work_canonical", {"work_id": wid, "candidate_id": "toh1"})
    secondary = _queue(db, "title_proposal", {"edition_id": e2, "work_id": wid, "title": "New"})
    res = EU.delete_work(db, wid)
    assert res["status"] == "deleted"
    assert db.execute("SELECT 1 FROM review_queue WHERE id=?", (owned,)).fetchone() is None
    assert db.execute("SELECT 1 FROM review_queue WHERE id=?", (secondary,)).fetchone() is not None


def test_soft_delete_person_keeps_pending_authority_item(db):
    """A curated delete now TOMBSTONES the person (id frozen), so its refs can never
    dangle and must NOT be purged — a pending authority item rides the tombstone and
    would reappear on restore. (Hard-delete purging lives in sweep_dangling_refs, below.)"""
    pid = _person(db)
    _queue(db, "person_authority", {"person_id": pid, "candidate_id": "P1"})
    contributor_edit.apply_delete(db, pid)
    assert _pending(db) == 1


def test_soft_delete_person_keeps_promotion_person_ids(db):
    """Same: a promotion's person_ids array keeps the soft-deleted id (frozen, no dangle)."""
    pid = _person(db)
    keep = _person(db, "Other")
    rid = _queue(db, "ingest", {"path": "/x.pdf"})
    db.execute("INSERT INTO promotion (review_item_id, holding_id, work_ids, person_ids) "
               "VALUES (?, NULL, '[]', ?)", (rid, json.dumps([pid, keep])))
    contributor_edit.apply_delete(db, pid)
    left = json.loads(db.execute(
        "SELECT person_ids FROM promotion WHERE review_item_id=?", (rid,)).fetchone()[0])
    assert left == [pid, keep]


# ── sweep_dangling_refs backfill (legacy debris reconcile) ───────────────────────
def test_sweep_drops_items_whose_owner_is_gone(db):
    """A work-owned item pointing at an ABSENT (legacy hard-deleted) work id is dangling and swept;
    one whose owner still exists is kept. Dry run touches nothing."""
    rid = _queue(db, "work_canonical", {"work_id": 4242})       # owner never existed → dangling
    live = _work(db)
    keep = _queue(db, "work_canonical", {"work_id": live})       # owner present → kept
    report = dangling_refs.sweep_dangling_refs(db, apply=False)
    assert [it["id"] for it in report["review_items_dangling"]] == [rid]
    assert _pending(db) == 2                                     # dry run mutated nothing
    dangling_refs.sweep_dangling_refs(db, apply=True)
    assert db.execute("SELECT 1 FROM review_queue WHERE id=?", (rid,)).fetchone() is None
    assert db.execute("SELECT 1 FROM review_queue WHERE id=?", (keep,)).fetchone()


def test_sweep_leaves_tombstoned_owner_alone(db):
    """A soft-deleted (tombstoned) root still has its row → its id is frozen → NOT a reuse hazard,
    so the sweep leaves its pending item for a possible restore."""
    wid = _work(db)
    db.execute("UPDATE work SET deleted_at = datetime('now') WHERE id=?", (wid,))
    rid = _queue(db, "work_canonical", {"work_id": wid})
    report = dangling_refs.sweep_dangling_refs(db, apply=True)
    assert report["review_items_dangling"] == []
    assert db.execute("SELECT 1 FROM review_queue WHERE id=?", (rid,)).fetchone()


def test_sweep_scrubs_promotion_ids_for_gone_entities(db):
    live = _person(db)
    rid = _queue(db, "ingest", {"path": "/x.pdf"})
    db.execute("INSERT INTO promotion (review_item_id, holding_id, work_ids, person_ids) "
               "VALUES (?, NULL, '[]', ?)", (rid, json.dumps([live, 9999])))
    report = dangling_refs.sweep_dangling_refs(db, apply=True)
    left = json.loads(db.execute(
        "SELECT person_ids FROM promotion WHERE review_item_id=?", (rid,)).fetchone()[0])
    assert left == [live]
    assert any(sc["column"] == "person_ids" and sc["removed"] == [9999]
               for sc in report["promotion_arrays_scrubbed"])


# ── cover-art purge on delete ────────────────────────────────────────────────────
def test_delete_edition_busts_cover_art(db, tmp_path):
    eid, _ = _edition(db)
    cache, pinned = str(tmp_path / "cache"), str(tmp_path / "pin")
    jpeg = b"\xff\xd8\xff" + b"x" * 50
    covers.write_cache(cache, f"e{eid}", jpeg)
    covers.write_cache(cache, f"spine-e{eid}", jpeg)
    covers.write_cache(pinned, f"e{eid}", jpeg)
    assert covers.cached_path(cache, f"e{eid}")
    EU.delete_edition(db, eid, cover_cache=cache, cover_pinned=pinned)
    assert covers.cached_path(cache, f"e{eid}") is None
    assert covers.cached_path(cache, f"spine-e{eid}") is None
    assert covers.cached_path(pinned, f"e{eid}") is None


def test_purge_edition_art_handles_missing_dirs(db):
    covers.purge_edition_art(None, None, 1)              # no dirs → no error
    covers.purge_edition_art("/no/such/dir", None, 1)   # absent dir → no error


# ── validate-at-consume id-reuse guard ───────────────────────────────────────────
def test_person_identity_ok_matches_name_and_alias(db):
    pid = _person(db, "Chögyam Trungpa")
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, 'Trungpa Rinpoche', 'other', 'trungpa rinpoche')", (pid,))
    assert verify.person_identity_ok(db, pid, "Chögyam Trungpa")     # primary name
    assert verify.person_identity_ok(db, pid, "Trungpa Rinpoche")    # an alias
    assert verify.person_identity_ok(db, pid, "")                    # blank → can't check, allow
    assert not verify.person_identity_ok(db, pid, "Someone Else")    # recycled id
    assert not verify.person_identity_ok(db, pid + 99, "Chögyam Trungpa")  # gone


def test_accept_person_authority_refuses_recycled_id(db):
    pid = _person(db, "Original Name")
    item = _queue(db, "person_authority",
                  {"person_id": pid, "person_name": "Original Name", "candidate_id": "P42"})
    # simulate id-reuse: the row at `pid` is now a DIFFERENT person
    db.execute("UPDATE person SET primary_name='Totally Different' WHERE id=?", (pid,))
    assert verify.accept_person_authority(db, item, commit=False) is False
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None
    # status untouched (still actionable once corrected)
    assert _pending(db) == 1


def test_accept_person_authority_binds_when_identity_matches(db):
    pid = _person(db, "Stable Name")
    item = _queue(db, "person_authority",
                  {"person_id": pid, "person_name": "Stable Name", "candidate_id": "P7"})
    assert verify.accept_person_authority(db, item, commit=False) is True
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] == "P7"
