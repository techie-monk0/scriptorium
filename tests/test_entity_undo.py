"""Reversible delete/merge for editions and works (entity_undo), reusing the shared
undo journal. Hermetic."""
import pytest

from catalogue.db_store import init_db
from catalogue.services import contributor_undo as undo, entity_undo as EU


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "c.db")
    yield conn
    conn.close()


def _edition(db, title, *, isbn=None):
    eid = db.execute("INSERT INTO edition (title, isbn) VALUES (?, ?)", (title, isbn)).lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
               (eid, f"/{title}.pdf"))
    return eid


def _work(db, english="A Work"):
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, ?, 'english', ?)", (wid, english, english.lower()))
    return wid


def _counts(db):
    return (db.execute("SELECT COUNT(*) FROM edition").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM holding").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM work").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM edition_work").fetchone()[0])


def test_delete_edition_blocked_when_cited_by_a_tool(db):
    # Stability S1: an edition BuddhistLLM depends on can't be hard-deleted — blocked cleanly,
    # before any destructive work, so the row + files stay put.
    from catalogue.access_api import external_deps as X
    eid = _edition(db, "Cited")
    pub = db.execute("SELECT pub_id FROM edition WHERE id=?", (eid,)).fetchone()[0]
    X.claim(db, pub_id=pub, tool="buddhistllm")
    res = EU.delete_edition(db, eid)
    assert res["status"] == "blocked" and "buddhistllm" in res["reason"].lower()
    assert db.execute("SELECT COUNT(*) FROM edition WHERE id=?", (eid,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM holding WHERE edition_id=?", (eid,)).fetchone()[0] == 1


def test_merge_of_a_cited_edition_forwards_instead_of_deleting(db):
    # The live services merge path (entity_undo → writes.merge) forwards a cited loser rather than
    # hard-deleting it, so its token still resolves — to the winner.
    from catalogue.access_api import external_deps as X
    dup = _edition(db, "Dup"); into = _edition(db, "Into")
    dpub = db.execute("SELECT pub_id FROM edition WHERE id=?", (dup,)).fetchone()[0]
    ipub = db.execute("SELECT pub_id FROM edition WHERE id=?", (into,)).fetchone()[0]
    X.claim(db, pub_id=dpub, tool="buddhistllm")
    res = EU.merge_editions(db, dup, into)
    assert res["status"] == "merged"
    assert db.execute("SELECT COUNT(*) FROM edition WHERE id=?", (dup,)).fetchone()[0] == 1  # tombstoned, not gone
    r = X.resolve(db, dpub)
    assert r.status == "superseded" and r.canonical == ipub


def test_delete_edition_is_reversible(db):
    eid = _edition(db, "Junk Scan")
    w = _work(db); db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) "
                              "VALUES (?, ?, 1)", (eid, w))
    before = _counts(db)
    res = EU.delete_edition(db, eid)
    assert res["status"] == "deleted"
    assert db.execute("SELECT COUNT(*) FROM edition WHERE id=?", (eid,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM holding WHERE edition_id=?", (eid,)).fetchone()[0] == 0
    undo.apply_undo(db, res["undo_token"])
    assert _counts(db) == before                                  # edition + holding + link back


def test_delete_edition_moves_holding_file_to_trash(db, tmp_path, monkeypatch):
    """Deleting an edition ALWAYS moves its holding's file (holding.file_path) into the
    Trash folder — recoverable, not unlinked or left behind. No opt-in any more."""
    trash = tmp_path / "trash"
    monkeypatch.setattr("catalogue.services.mount.trash_dir", lambda: str(trash))
    src = tmp_path / "02 Il Signor Rigoni.pdf"
    src.write_bytes(b"%PDF stub")
    eid = db.execute("INSERT INTO edition (title) VALUES ('Rigoni')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
               (eid, str(src)))

    res = EU.delete_edition(db, eid)                       # no remove_files flag — always moves
    assert res["status"] == "deleted" and res["files_moved"] == 1
    assert not src.exists()                                # gone from the library
    assert (trash / src.name).read_bytes() == b"%PDF stub"  # waiting in Trash


def test_delete_edition_keeps_file_shared_with_another_edition(db, tmp_path, monkeypatch):
    """A file a DIFFERENT edition's holding still references is NOT trashed (dedup/merge can leave
    two editions pointing at one file) — only files no surviving holding references move to Trash."""
    trash = tmp_path / "trash"
    monkeypatch.setattr("catalogue.services.mount.trash_dir", lambda: str(trash))
    shared = tmp_path / "shared.pdf"; shared.write_bytes(b"shared")
    e1 = db.execute("INSERT INTO edition (title) VALUES ('E1')").lastrowid
    e2 = db.execute("INSERT INTO edition (title) VALUES ('E2')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
               (e1, str(shared)))
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
               (e2, str(shared)))
    db.commit()

    res = EU.delete_edition(db, e1)               # e2 still references the file
    assert res["files_moved"] == 0
    assert shared.exists()                        # kept — sibling edition needs it
    res2 = EU.delete_edition(db, e2)              # now nothing references it → trashed
    assert res2["files_moved"] == 1
    assert not shared.exists() and (trash / shared.name).exists()


def test_delete_edition_moves_both_source_and_archival(db, tmp_path, monkeypatch):
    """Both the source file_path and the archival PDF move to Trash (deduped, NULLs dropped)."""
    trash = tmp_path / "trash"
    monkeypatch.setattr("catalogue.services.mount.trash_dir", lambda: str(trash))
    src = tmp_path / "book.pdf"; src.write_bytes(b"src")
    arch = tmp_path / "book.archival.pdf"; arch.write_bytes(b"arch")
    eid = db.execute("INSERT INTO edition (title) VALUES ('Two Files')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path, archival_pdf_path) "
               "VALUES (?, 'electronic', ?, ?)", (eid, str(src), str(arch)))

    res = EU.delete_edition(db, eid)
    assert res["files_moved"] == 2
    assert not src.exists() and not arch.exists()
    assert (trash / src.name).exists() and (trash / arch.name).exists()


def test_merge_editions_is_reversible(db):
    dup = _edition(db, "Dup Copy", isbn="111")
    into = _edition(db, "Canonical", isbn="111")
    w = _work(db); db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) "
                              "VALUES (?, ?, 1)", (dup, w))
    before = _counts(db)
    res = EU.merge_editions(db, dup, into)
    assert res["status"] == "merged"
    assert db.execute("SELECT COUNT(*) FROM edition WHERE id=?", (dup,)).fetchone()[0] == 0
    # dup's holding + work-link moved to the survivor
    assert db.execute("SELECT COUNT(*) FROM holding WHERE edition_id=?", (into,)).fetchone()[0] == 2
    assert db.execute("SELECT edition_id FROM edition_work WHERE work_id=?", (w,)).fetchone()[0] == into
    undo.apply_undo(db, res["undo_token"])
    assert _counts(db) == before
    assert db.execute("SELECT edition_id FROM edition_work WHERE work_id=?", (w,)).fetchone()[0] == dup


def test_delete_work_blocked_when_sole_work_of_an_edition(db):
    w = _work(db, "Only Work")
    e = _edition(db, "Book")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (e, w))
    res = EU.delete_work(db, w)                                   # refused — e has no other work
    assert "error" in res and res["blocking_editions"][0]["id"] == e
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (w,)).fetchone()[0] == 1   # not deleted


def test_delete_work_ok_when_edition_has_other_works_and_reversible(db):
    w = _work(db, "Shared Work")
    e = _edition(db, "Anthology")
    other = _work(db, "Other")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (e, w))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 2)", (e, other))
    res = EU.delete_work(db, w)
    assert res["status"] == "deleted"                            # e still has `other`
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE edition_id=? AND work_id=?",
                      (e, w)).fetchone()[0] == 0                 # edition link removed
    undo.apply_undo(db, res["undo_token"])
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE edition_id=? AND work_id=?",
                      (e, w)).fetchone()[0] == 1                 # link restored


def test_delete_work_is_reversible(db):
    w = _work(db, "Lonely Work")
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('A')").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (w, pid))
    res = EU.delete_work(db, w)                                   # no editions reference it → ok
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (w,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM work_alias WHERE work_id=?", (w,)).fetchone()[0] == 0
    undo.apply_undo(db, res["undo_token"])
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (w,)).fetchone()[0] == 1
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=?", (w,)).fetchone()[0] == pid


def test_merge_works_is_reversible(db):
    dup, into = _work(db, "Root Verses"), _work(db, "Fundamental Wisdom")
    e = _edition(db, "Book")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (e, dup))
    res = EU.merge_works(db, dup, into)
    assert res["status"] == "merged"
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (dup,)).fetchone()[0] == 0
    assert db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (e,)).fetchone()[0] == into
    undo.apply_undo(db, res["undo_token"])
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (dup,)).fetchone()[0] == 1   # loser back
    assert db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (e,)).fetchone()[0] == dup


def test_merge_edition_into_itself_refused(db):
    e = _edition(db, "X")
    assert "error" in EU.merge_editions(db, e, e)
