"""System test — the reconcile UI: scan, auto-apply moves, enqueue + resolve new files."""
from __future__ import annotations

from catalogue.services.sweep import _hash_file
from pathlib import Path


def _seed_holding(seed, *, path, fhash, title="A"):
    eid = seed("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    seed("INSERT INTO holding (edition_id, form, file_path, file_hash, text_status) "
         "VALUES (?, 'electronic', ?, ?, 'ocr_good')", (eid, path, fhash))
    return eid


def test_reconcile_page_loads(app_env, seed):
    c, _, _ = app_env
    assert b"Reconcile" in c.get("/reconcile").data


def test_scan_auto_repoints_move_and_enqueues_new(app_env, seed, tmp_path):
    c, _, _ = app_env
    lib = tmp_path / "lib"; lib.mkdir()
    moved = lib / "moved.pdf"; moved.write_bytes(b"%PDF-1.4 book one bytes")
    new = lib / "brand_new.pdf"; new.write_bytes(b"%PDF-1.4 a totally different book")
    # holding for the first file recorded at an OLD path → should repoint to lib/.
    _seed_holding(seed, path="/old/moved.pdf", fhash=_hash_file(moved, 3, 0.1))

    c.post("/reconcile/run", data={"roots": str(lib)})

    # moved → auto-repointed
    import sqlite3
    db = sqlite3.connect(c.application.config["DB_PATH"])
    assert db.execute("SELECT file_path FROM holding WHERE file_path=?", (str(moved),)).fetchone()
    # new file → an ingest review item is pending
    n = db.execute("SELECT COUNT(*) FROM review_queue WHERE item_type='ingest' AND status='pending'").fetchone()[0]
    assert n == 1
    page = c.get("/reconcile").data
    assert b"New file" in page and b"brand_new.pdf" in page


def test_open_proposed_file_link(app_env, seed, tmp_path):
    c, _, _ = app_env
    lib = tmp_path / "lib"; lib.mkdir()
    f = lib / "openme.pdf"; f.write_bytes(b"%PDF-1.4 open me in the viewer")
    c.post("/reconcile/run", data={"roots": str(lib)})

    # the filename on the Scan page is a link to the viewer
    page = c.get("/reconcile").data
    assert b"/reconcile/file?path=" in page
    # the link streams the actual bytes for a pending-referenced path...
    r = c.get("/reconcile/file", query_string={"path": str(f)})
    assert r.status_code == 200 and r.data == f.read_bytes()
    # ...but refuses a path no pending item references (no arbitrary-file read)
    assert c.get("/reconcile/file", query_string={"path": "/etc/passwd"}).status_code == 403


def test_content_changed_links_catalogued_file(app_env, seed, tmp_path):
    # A file edited in place (same path, new bytes) shows BOTH the new-file link
    # and a link to the catalogued holding/edition it may replace.
    c, _, _ = app_env
    lib = tmp_path / "lib"; lib.mkdir()
    f = lib / "edited.pdf"; f.write_bytes(b"%PDF-1.4 brand new text layer")
    eid = _seed_holding(seed, path=str(f), fhash="OLDHASH", title="Edited Book")

    c.post("/reconcile/run", data={"roots": str(lib)})
    page = c.get("/reconcile").data.decode()
    import sqlite3
    db = sqlite3.connect(c.application.config["DB_PATH"])
    hid = db.execute("SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]
    assert "catalogued file it may replace" in page
    assert f"/holding/{hid}/file" in page
    assert f"/edition/{eid}" in page


def test_scan_page_flags_broken_links_at_top_in_red(app_env, seed, tmp_path):
    """Broken links (file gone on disk; edition with no holding) are surfaced on the
    Scan page — AT THE TOP, in red — without needing a full walk."""
    c, _, _ = app_env
    lib = tmp_path / "lib"; lib.mkdir()            # parent exists → gone file is 'missing'
    gone_eid = _seed_holding(seed, path=str(lib / "deleted.pdf"), fhash="g", title="Was Here")
    orphan = seed("INSERT INTO edition (title) VALUES ('No File Edition')").lastrowid
    page = c.get("/reconcile").data.decode()
    assert "Broken links" in page and 'class="broken-box"' in page
    assert "broken-hd" in page                                      # red header
    assert f"/edition/{gone_eid}" in page and "deleted.pdf" in page  # gone-file holding
    assert f"/edition/{orphan}" in page and "No File Edition" in page  # orphan edition
    # at the very top — before the scan form
    assert page.find('class="broken-box"') < page.find('action="/reconcile/run"')


def test_broken_holding_flagged_on_display(app_env, seed, tmp_path):
    """A holding whose file is gone shows a broken-link error on the copy card and a
    greyed '⚠ file not found' marker in Edition Basics (not a live open link)."""
    c, _, _ = app_env
    lib = tmp_path / "lib"; lib.mkdir()
    eid = _seed_holding(seed, path=str(lib / "gone.pdf"), fhash="g", title="Broken Book")
    import sqlite3
    hid = sqlite3.connect(c.application.config["DB_PATH"]).execute(
        "SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]
    card = c.get(f"/holding/{hid}/card").data.decode()
    assert "Broken link" in card and "gone.pdf" in card
    summary = c.get(f"/edition/{eid}/works-summary").data.decode()
    assert "⚠ file not found" in summary


def test_resolve_new_file_as_distinct_book(app_env, seed, tmp_path):
    c, _, _ = app_env
    lib = tmp_path / "lib"; lib.mkdir()
    (lib / "fresh.pdf").write_bytes(b"%PDF-1.4 fresh content here")
    c.post("/reconcile/run", data={"roots": str(lib)})
    import sqlite3
    db = sqlite3.connect(c.application.config["DB_PATH"])
    iid = db.execute("SELECT id FROM review_queue WHERE item_type='ingest' AND status='pending'").fetchone()[0]

    c.post(f"/reconcile/{iid}/apply", data={"action": "distinct"})

    db = sqlite3.connect(c.application.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM holding WHERE file_path=?",
                      (str(lib / "fresh.pdf"),)).fetchone()[0] == 1
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (iid,)).fetchone()[0] == "resolved"


def test_scan_page_has_bulk_select_bar(app_env, seed, tmp_path):
    c, _, _ = app_env
    lib = tmp_path / "lib"; lib.mkdir()
    (lib / "one.pdf").write_bytes(b"%PDF-1.4 book one content")
    (lib / "two.pdf").write_bytes(b"%PDF-1.4 book two content")
    c.post("/reconcile/run", data={"roots": str(lib)})
    page = c.get("/reconcile").data.decode()
    assert 'id="rc-bulk"' in page and 'id="rc-all"' in page      # the bulk bar + select-all
    assert 'class="rc-check' in page and 'data-kind=' in page    # per-item checkboxes carry kind


def test_bulk_ignore_resolves_all_selected(app_env, seed, tmp_path):
    c, _, _ = app_env
    lib = tmp_path / "lib"; lib.mkdir()
    (lib / "a.pdf").write_bytes(b"%PDF-1.4 alpha")
    (lib / "b.pdf").write_bytes(b"%PDF-1.4 bravo")
    c.post("/reconcile/run", data={"roots": str(lib)})
    import sqlite3
    db = sqlite3.connect(c.application.config["DB_PATH"])
    ids = [r[0] for r in db.execute(
        "SELECT id FROM review_queue WHERE item_type='ingest' AND status='pending'")]
    assert len(ids) == 2

    c.post("/reconcile/bulk", data={"action": "ignore", "item_id": ids})

    db = sqlite3.connect(c.application.config["DB_PATH"])
    pend = db.execute("SELECT COUNT(*) FROM review_queue WHERE item_type='ingest' "
                      "AND status='pending'").fetchone()[0]
    assert pend == 0                                              # both resolved
    assert db.execute("SELECT COUNT(*) FROM ingest_ignore").fetchone()[0] == 2


def test_bulk_rejects_unknown_action(app_env, seed, tmp_path):
    c, _, _ = app_env
    lib = tmp_path / "lib"; lib.mkdir()
    (lib / "x.pdf").write_bytes(b"%PDF-1.4 xray")
    c.post("/reconcile/run", data={"roots": str(lib)})
    import sqlite3
    db = sqlite3.connect(c.application.config["DB_PATH"])
    iid = db.execute("SELECT id FROM review_queue WHERE item_type='ingest'").fetchone()[0]
    # replace/add_copy need a per-item target → not allowed in bulk
    assert c.post("/reconcile/bulk", data={"action": "replace", "item_id": [iid]}).status_code == 400
    assert c.post("/reconcile/bulk", data={"action": "ignore"}).status_code == 400   # no ids
