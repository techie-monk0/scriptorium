"""Config-driven exclusion rules (skip.is_excluded) + the purge of already-ingested
excluded files."""
import json

import pytest

from catalogue.access_api import system_access
from catalogue.db_store import init_db
from catalogue.services import skip
from catalogue.cli import exclude_purge


@pytest.fixture(autouse=True)
def _fresh_rules():
    skip.exclusion_rules.cache_clear()
    yield
    skip.exclusion_rules.cache_clear()


def test_default_annotated_rule():
    p = ("/Users/x/Library/01 Books - Dharma/00 Emptiness/"
         "00 LTK Illuminating the Intent ANNOTATED— Jinpa — VGC Commentary/"
         "Supplementary Materials/Note on p213.pdf")
    assert skip.is_excluded(file_path=p)
    assert skip.is_excluded(title="My Book (ANNOTATED)")
    assert not skip.is_excluded(file_path="/Users/x/Library/Dharma/Emptiness/A.pdf")
    assert not skip.is_excluded(title="A Clean Title")


def test_ops(monkeypatch):
    monkeypatch.setattr(skip, "exclusion_rules", lambda: (
        {"field": "path", "op": "starts_with", "value": "/private/tmp/"},
        {"field": "title", "op": "ends_with", "value": "(draft)"},
        {"field": "any", "op": "contains", "value": "ANNOTATED"},
    ))
    assert skip.is_excluded(file_path="/private/tmp/x.pdf")           # starts_with
    assert not skip.is_excluded(file_path="/home/x/private/tmp/x.pdf")
    assert skip.is_excluded(title="Chapter 1 (draft)")               # ends_with (case-insensitive)
    assert skip.is_excluded(file_path="/a/b ANNOTATED/c.pdf")        # contains
    assert not skip.is_excluded(file_path="/a/clean/c.pdf", title="clean")


def test_under_op_matches_subtree_not_siblings(monkeypatch):
    monkeypatch.setattr(skip, "exclusion_rules", lambda: (
        {"field": "path", "op": "under", "value": "/lib/Tantra"},
    ))
    assert skip.is_excluded(file_path="/lib/Tantra")                 # the folder itself
    assert skip.is_excluded(file_path="/lib/Tantra/deep/x.pdf")      # any descendant
    assert not skip.is_excluded(file_path="/lib/TantraNotes/x.pdf")  # sibling sharing a prefix
    assert not skip.is_excluded(file_path="/lib/Other/x.pdf")


@pytest.fixture
def vocab(tmp_path, monkeypatch):
    """A throwaway vocab.json wired to BOTH the read path (skip → db.db.VOCAB_PATH)
    and the write path (mount._write_vocab_value → mount.VOCAB_PATH)."""
    import catalogue.db_store.db as dbmod
    from catalogue.services import mount
    vp = tmp_path / "vocab.json"
    vp.write_text(json.dumps(
        {"_exclusions": [{"field": "any", "op": "contains", "value": "ANNOTATED"}]}, indent=2))
    monkeypatch.setattr(dbmod, "VOCAB_PATH", vp)
    monkeypatch.setattr(mount, "VOCAB_PATH", vp)
    return vp


def test_set_subdir_excluded_persists_and_toggles(vocab):
    skip.set_subdir_excluded("/lib/Tantra/", True)                  # trailing slash normalised away
    assert skip.subdir_excluded("/lib/Tantra")
    assert skip.under_excluded("/lib/Tantra/Restricted/x")
    assert not skip.subdir_excluded("/lib/Tantra/Restricted")       # only the exact folder is a rule
    assert not skip.under_excluded("/lib/TantraNotes")              # sibling prefix stays safe
    assert skip.is_excluded(file_path="/lib/Tantra/x.pdf")
    assert skip.is_excluded(title="x ANNOTATED")                    # default rule preserved
    assert {"field": "path", "op": "under", "value": "/lib/Tantra"} \
        in json.loads(vocab.read_text())["_exclusions"]

    skip.set_subdir_excluded("/lib/Tantra", False)                  # toggle back off
    assert not skip.under_excluded("/lib/Tantra/Restricted/x")
    assert skip.is_excluded(title="x ANNOTATED")                    # default still there
    assert all(r.get("op") != "under" for r in json.loads(vocab.read_text())["_exclusions"])


def test_prune_excluded_ingest_drops_pending_under_excluded(tmp_path, monkeypatch):
    from catalogue.services import reconcile
    db = init_db(tmp_path / "c.db")

    def _pending(path):
        return db.execute(
            "INSERT INTO review_queue (item_type, payload_json) VALUES ('ingest', ?)",
            (json.dumps({"kind": "new", "path": path}),)).lastrowid
    keep = _pending("/lib/Sutra/a.pdf")
    _pending("/lib/Tantra/secret.pdf")                              # under the excluded folder
    db.commit()
    monkeypatch.setattr(skip, "exclusion_rules", lambda: (
        {"field": "path", "op": "under", "value": "/lib/Tantra"},))

    assert reconcile.prune_excluded_ingest(db) == 1                 # only the Tantra one dropped
    left = [r[0] for r in db.execute(
        "SELECT id FROM review_queue WHERE item_type='ingest' AND status='pending'").fetchall()]
    assert left == [keep]


def _holding(db, eid, path):
    return db.execute("INSERT INTO holding (edition_id, form, file_path) "
                      "VALUES (?, 'electronic', ?)", (eid, path)).lastrowid


def test_purge_removes_excluded_and_keeps_clean(tmp_path):
    db = init_db(tmp_path / "c.db")
    # an ANNOTATED edition (will be removed) with its degenerate work
    e_ann = db.execute("INSERT INTO edition (title) VALUES ('Ann')").lastrowid
    _holding(db, e_ann, "/lib/00 Emptiness ANNOTATED/supp/note.pdf")
    w = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)",
               (e_ann, w))
    # a clean edition (kept)
    e_ok = db.execute("INSERT INTO edition (title) VALUES ('Clean')").lastrowid
    _holding(db, e_ok, "/lib/Emptiness/clean.pdf")
    db.commit()

    with system_access(tmp_path / "c.db") as acc:
        excl, del_eds = exclude_purge.plan(acc)
        assert [h[1] for h in excl] == [e_ann]
        assert del_eds == [e_ann]

        removed_works = exclude_purge.apply(acc, excl, del_eds)
        assert removed_works == 1                              # the orphaned degenerate work

    # Roots tombstone under the access-API's soft-delete (id frozen), holdings hard-delete.
    assert db.execute("SELECT COUNT(*) FROM edition WHERE deleted_at IS NULL"
                      ).fetchone()[0] == 1                     # clean survives
    assert db.execute("SELECT id FROM edition WHERE deleted_at IS NULL"
                      ).fetchone()[0] == e_ok
    assert db.execute("SELECT COUNT(*) FROM holding WHERE UPPER(file_path) LIKE '%ANNOTATED%'"
                      ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM work WHERE deleted_at IS NULL"
                      ).fetchone()[0] == 0                     # the orphaned work tombstoned


def test_purge_keeps_edition_with_a_clean_holding(tmp_path):
    db = init_db(tmp_path / "c.db")
    e = db.execute("INSERT INTO edition (title) VALUES ('Mixed')").lastrowid
    _holding(db, e, "/lib/x ANNOTATED/a.pdf")          # excluded
    _holding(db, e, "/lib/x/clean.pdf")                # clean → edition stays
    db.commit()
    with system_access(tmp_path / "c.db") as acc:
        excl, del_eds = exclude_purge.plan(acc)
        assert len(excl) == 1 and del_eds == []        # edition not fully excluded
        exclude_purge.apply(acc, excl, del_eds)
    assert db.execute("SELECT COUNT(*) FROM edition WHERE deleted_at IS NULL"
                      ).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM holding").fetchone()[0] == 1   # only the clean one
