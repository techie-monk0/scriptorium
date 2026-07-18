"""The authored-outline bake routes (`/holding/<id>/outlined.pdf` + `POST …/outlined`) and the
DB-backed `OutlineStore` adapter over the reader sync-of-record, exercised on a real PDF holding.
"""
from __future__ import annotations

import io
import sqlite3

import pytest

fitz = pytest.importorskip("fitz")

from catalogue.db_store import reader_state as rs
from catalogue.webui.outline_store import ReaderStateOutlineStore, outline_op_id


def _make_pdf(path, pages=5):
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"Page {i + 1}")
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def app_and_ids(tmp_path, monkeypatch):
    from catalogue.webui.web import create_app
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    db_path = tmp_path / "outline.db"
    app = create_app(db_path)
    pdf = _make_pdf(tmp_path / "book.pdf")
    conn = sqlite3.connect(db_path); conn.execute("PRAGMA foreign_keys = ON")
    eid = conn.execute("INSERT INTO edition (title) VALUES ('Book')").lastrowid
    hid = conn.execute("INSERT INTO holding (edition_id, form, file_path, text_status) "
                       "VALUES (?, 'electronic', ?, 'ocr_good')", (eid, str(pdf))).lastrowid
    conn.commit(); conn.close()
    return app, hid, db_path


def _seed_outline(db_path, hid, entries):
    conn = sqlite3.connect(db_path)
    ReaderStateOutlineStore(rs.SqliteReaderStateStore(conn)).set_outline(hid, entries)
    conn.commit(); conn.close()


# ── the DB-backed OutlineStore adapter ────────────────────────────────────────
def test_reader_state_outline_store_roundtrips(cat_conn):
    from catalogue.test_kit import seed_minimal
    seed_minimal(cat_conn)
    raw = rs.SqliteReaderStateStore(cat_conn); raw.ensure_schema(); cat_conn.commit()
    hid = cat_conn.execute("SELECT id FROM holding ORDER BY id LIMIT 1").fetchone()[0]

    store = ReaderStateOutlineStore(raw)
    assert store.get_outline(hid) == []
    store.set_outline(hid, [{"level": 1, "title": "A", "page": 1},
                            {"level": 2, "title": "B", "page": 3}])
    cat_conn.commit()
    assert store.get_outline(hid) == [{"level": 1, "title": "A", "page": 1},
                                      {"level": 2, "title": "B", "page": 3}]
    # wholesale replace (LWW), keyed by the stable per-copy id → one row
    store.set_outline(hid, [{"level": 1, "title": "Only", "page": 2}])
    cat_conn.commit()
    assert store.get_outline(hid) == [{"level": 1, "title": "Only", "page": 2}]
    assert raw.outline_for_holding(hid).id == outline_op_id(hid)


# ── the bake routes ───────────────────────────────────────────────────────────
def test_outlined_pdf_copy_bakes_toc(app_and_ids):
    app, hid, db_path = app_and_ids
    _seed_outline(db_path, hid, [{"level": 1, "title": "Chapter One", "page": 1},
                                 {"level": 1, "title": "Chapter Two", "page": 3}])
    r = app.test_client().get(f"/holding/{hid}/outlined.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert r.data[:5] == b"%PDF-"
    doc = fitz.open(stream=io.BytesIO(r.data), filetype="pdf")
    assert doc.get_toc() == [[1, "Chapter One", 1], [1, "Chapter Two", 3]]


def test_outlined_pdf_409_when_no_outline(app_and_ids):
    app, hid, db_path = app_and_ids
    assert app.test_client().get(f"/holding/{hid}/outlined.pdf").status_code == 409


def test_outlined_inplace_writes_into_original(app_and_ids):
    app, hid, db_path = app_and_ids
    _seed_outline(db_path, hid, [{"level": 1, "title": "Intro", "page": 1}])
    # localhost-only guard is satisfied by the test client's 127.0.0.1 remote_addr
    r = app.test_client().post(f"/holding/{hid}/outlined")
    assert r.status_code == 200 and r.get_json() == {"written": True}
    # the stored file now carries the outline
    conn = sqlite3.connect(db_path)
    fp = conn.execute("SELECT file_path FROM holding WHERE id = ?", (hid,)).fetchone()[0]
    conn.close()
    assert fitz.open(fp).get_toc() == [[1, "Intro", 1]]
