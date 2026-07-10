"""POST /edition/<id>/delete and /holding/<id>/delete — cascade order,
work preservation, and the uniform move-files-to-Trash behavior (single == bulk)."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _seed_edition_with_everything(seed, tmp_path: Path):
    """Edition + a work attached via edition_work + a holding with an
    archival PDF on disk + an FTS row. Returns (edition_id, holding_id,
    work_id, archival_path)."""
    seed("INSERT INTO edition (title, isbn) VALUES ('The Way', '9780861711765')")
    seed("INSERT INTO work (original_language) VALUES ('sa')")
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (1, 'Bodhicaryāvatāra', 'iast', 'bodhicaryavatara')")
    seed("INSERT INTO edition_work (edition_id, work_id, sequence) "
         "VALUES (1, 1, 1)")

    archival = tmp_path / "archival" / "the-way.pdfa.pdf"
    archival.parent.mkdir(parents=True, exist_ok=True)
    archival.write_bytes(b"%PDF-1.4 stub")

    seed("INSERT INTO holding "
         "(edition_id, form, file_path, text_status, archival_pdf_path) "
         "VALUES (1, 'electronic', '/orig/the-way.pdf', 'ocr_good', ?)",
         (str(archival),))
    seed("INSERT INTO edition_text (edition_id, page, content) "
         "VALUES (1, 1, 'Bodhicaryāvatāra opening')")
    return 1, 1, 1, archival


def test_delete_edition_cascades_dependents_keeps_work(app_env, seed, tmp_path, monkeypatch):
    c, app, _ = app_env
    trash = tmp_path / "trash"
    monkeypatch.setattr("catalogue.services.mount.trash_dir", lambda: str(trash))
    eid, hid, wid, archival = _seed_edition_with_everything(seed, tmp_path)

    r = c.post(f"/edition/{eid}/delete")
    assert r.status_code in (302, 303)

    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("SELECT count(*) FROM edition       WHERE id=?", (eid,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM holding       WHERE id=?", (hid,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM edition_work  WHERE edition_id=?", (eid,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM edition_text  WHERE edition_id=?", (eid,)).fetchone()[0] == 0
    # Work is preserved — it can live in other editions (§2).
    assert conn.execute("SELECT count(*) FROM work          WHERE id=?", (wid,)).fetchone()[0] == 1
    conn.close()
    # Files always move to Trash now (no opt-in): the archival PDF is gone from disk, in Trash.
    assert not archival.exists()
    assert (trash / archival.name).exists()


def test_delete_edition_fts_mirror_updates(app_env, seed, tmp_path, monkeypatch):
    """The AFTER DELETE trigger on edition_text must clear the FTS row."""
    c, app, _ = app_env
    monkeypatch.setattr("catalogue.services.mount.trash_dir", lambda: str(tmp_path / "trash"))
    _seed_edition_with_everything(seed, tmp_path)

    conn = sqlite3.connect(app.config["DB_PATH"])
    before = conn.execute(
        "SELECT count(*) FROM edition_text_fts "
        "WHERE edition_text_fts MATCH 'Bodhicaryāvatāra'"
    ).fetchone()[0]
    conn.close()
    assert before == 1

    c.post("/edition/1/delete")

    conn = sqlite3.connect(app.config["DB_PATH"])
    after = conn.execute(
        "SELECT count(*) FROM edition_text_fts "
        "WHERE edition_text_fts MATCH 'Bodhicaryāvatāra'"
    ).fetchone()[0]
    conn.close()
    assert after == 0


def test_delete_edition_moves_archival_pdf_to_trash(app_env, seed, tmp_path, monkeypatch):
    c, _, _ = app_env
    trash = tmp_path / "trash"
    monkeypatch.setattr("catalogue.services.mount.trash_dir", lambda: str(trash))
    _, _, _, archival = _seed_edition_with_everything(seed, tmp_path)
    assert archival.exists()
    r = c.post("/edition/1/delete")          # no checkbox — delete always moves files to Trash
    assert r.status_code in (302, 303)
    # Moved, not unlinked: gone from its original path, now in the Trash folder.
    assert not archival.exists()
    assert (trash / archival.name).exists()


def test_single_and_bulk_delete_both_move_files_to_trash(app_env, seed, tmp_path, monkeypatch):
    """The user-facing invariant: single delete and bulk delete behave IDENTICALLY —
    both cascade the rows (reversibly) and move the holding files to Trash."""
    c, _, _ = app_env
    trash = tmp_path / "trash"
    monkeypatch.setattr("catalogue.services.mount.trash_dir", lambda: str(trash))
    f1 = tmp_path / "one.pdf"; f1.write_bytes(b"one")
    f2 = tmp_path / "two.pdf"; f2.write_bytes(b"two")
    seed("INSERT INTO edition (title) VALUES ('One')")
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (1, 'electronic', ?)", (str(f1),))
    seed("INSERT INTO edition (title) VALUES ('Two')")
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (2, 'electronic', ?)", (str(f2),))

    c.post("/edition/1/delete")                                   # single
    c.post("/works/detect/bulk-delete-editions", json={"ids": [2]})  # bulk

    for f in (f1, f2):                                            # identical outcome
        assert not f.exists() and (trash / f.name).exists()


def test_delete_edition_404_when_missing(app_env):
    c, _, _ = app_env
    assert c.post("/edition/9999/delete").status_code == 404


def test_delete_holding_keeps_edition_and_work(app_env, seed, tmp_path, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setattr("catalogue.services.mount.trash_dir", lambda: str(tmp_path / "trash"))
    _seed_edition_with_everything(seed, tmp_path)

    r = c.post("/holding/1/delete")
    assert r.status_code in (302, 303)
    # Redirect targets the surviving edition detail.
    assert r.headers["Location"].endswith("/edition/1")

    conn = sqlite3.connect(app.config["DB_PATH"])
    assert conn.execute("SELECT count(*) FROM holding WHERE id=1").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM edition WHERE id=1").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM work    WHERE id=1").fetchone()[0] == 1
    conn.close()


def test_delete_holding_moves_files_to_trash(app_env, seed, tmp_path, monkeypatch):
    c, _, _ = app_env
    trash = tmp_path / "trash"
    monkeypatch.setattr("catalogue.services.mount.trash_dir", lambda: str(trash))
    _, _, _, archival = _seed_edition_with_everything(seed, tmp_path)
    c.post("/holding/1/delete")              # no checkbox — deleting a copy moves its file(s) to Trash
    assert not archival.exists()
    assert (trash / archival.name).exists()


def test_delete_holding_404_when_missing(app_env):
    c, _, _ = app_env
    assert c.post("/holding/9999/delete").status_code == 404
