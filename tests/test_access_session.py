"""Session / unit-of-work — multi-aggregate atomicity (reorg Phase 3).

A Session stages several planned Impacts into ONE RW transaction: one commit makes them
all-or-nothing, and the filesystem effects each op deferred run once, after that commit. See
entity_api_model.md §5.
"""
from __future__ import annotations

import pytest

from catalogue.access_api import system_access
from catalogue.contracts import IntegrityViolation, Ref
from catalogue.db_store import init_db


def _seed(tmp_path):
    f = tmp_path / "book.pdf"
    f.write_text("pdf bytes")
    db = tmp_path / "t.db"
    c = init_db(db)
    eid = c.execute("INSERT INTO edition (title, isbn) VALUES ('Bk', '111')").lastrowid
    subj = c.execute("INSERT INTO subject (name) VALUES ('Madhyamaka')").lastrowid
    hid = c.execute(
        "INSERT INTO holding (edition_id, file_path, file_hash, content_hash, text_status) "
        "VALUES (?, ?, 'fh1', 't:abc', 'ocr_poor')", (eid, str(f))).lastrowid
    c.commit()
    c.close()
    return dict(db=db, eid=eid, subj=subj, hid=hid, f=f)


def test_two_ops_commit_atomically(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        with acc.session() as sess:
            sess.stage(acc.subjects.writes, acc.subjects.writes.plan_delete(Ref("subject", s["subj"])))
            sess.stage(acc.holdings.writes,
                       acc.holdings.writes.plan_set_text_status(Ref("holding", s["hid"]), "ocr_good"))
        # both took effect after the single commit
        assert acc.subjects.reads.get(s["subj"]) is None
        assert acc.holdings.reads.get(s["hid"]).text_status == "ocr_good"


def test_rollback_undoes_prior_staged_op(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        with pytest.raises(IntegrityViolation):
            with acc.session() as sess:
                # a valid op, then one that fails to stage (un-appliable: missing target)
                sess.stage(acc.subjects.writes, acc.subjects.writes.plan_delete(Ref("subject", s["subj"])))
                sess.stage(acc.subjects.writes, acc.subjects.writes.plan_delete(Ref("subject", 99999)))
        # the whole transaction rolled back — the valid delete did NOT persist
        assert acc.subjects.reads.get(s["subj"]) is not None


def test_combined_impact_and_deferred_file_trash(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        with acc.session() as sess:
            sess.stage(acc.subjects.writes, acc.subjects.writes.plan_delete(Ref("subject", s["subj"])))
            sess.stage(acc.holdings.writes, acc.holdings.writes.plan_delete(Ref("holding", s["hid"])))
            combined = sess.impact()
            assert combined.op == "session"
            assert any(op.op == "trash" and op.path == str(s["f"]) for op in combined.file_ops)
            assert s["f"].exists()                       # file NOT trashed until the session commits
        # after commit: file trashed (recoverable), holding gone
        assert not s["f"].exists() and (acc.trash_dir / s["f"].name).exists()
        assert acc.holdings.reads.get(s["hid"]) is None
