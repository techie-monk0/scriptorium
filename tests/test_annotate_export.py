"""Export-annotated-PDF service (catalogue.webui.annotate_export) + its routes — the third-party
-tool path (reader_module_plan.md §7). Proves the reader's synced marks bake into a PDF that any
viewer renders: text marks as standard annotations, handwriting as a faithful filled vector path,
EPUB/cfi-only marks skipped. Both modes: a safe copy and write-into-original.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from catalogue.db_store import reader_state as rs
from catalogue.webui import annotate_export

fitz = pytest.importorskip("fitz")


def _ann(**kw):
    base = dict(id="a", holding_id=1, kind=None, cfi_range=None, page=None, rect=None,
                color=None, note_text=None, ink=None, created_at=None, updated_at=None,
                deleted_at=None, rev=1)
    base.update(kw)
    return rs.Annotation(**base)


def _make_pdf(path, pages=2):
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page(width=400, height=600)
        pg.insert_text((40, 80), f"Page {i + 1} text to mark up.")
    doc.save(str(path)); doc.close()
    return path


def test_has_pdf_annotations_ignores_epub_only(tmp_path):
    epub_only = _ann(kind="highlight", cfi_range="epubcfi(/6/4)")   # no page → not PDF
    pdf_mark = _ann(kind="ink", page=1, ink='{"strokes":[]}')
    assert annotate_export.has_pdf_annotations([epub_only]) is False
    assert annotate_export.has_pdf_annotations([epub_only, pdf_mark]) is True


def test_export_copy_bakes_text_marks_and_ink(tmp_path):
    src = _make_pdf(tmp_path / "src.pdf")
    anns = [
        _ann(id="h", kind="highlight", page=1, rect=json.dumps([[0.1, 0.12, 0.4, 0.04]]), color="#ffd54a"),
        _ann(id="u", kind="underline", page=1, rect=json.dumps([[0.1, 0.2, 0.4, 0.03]]), color="#90caf9"),
        _ann(id="n", kind="note", page=1, rect=json.dumps([0.5, 0.5]), note_text="see this"),
        _ann(id="k", kind="ink", page=2,
             ink=json.dumps({"strokes": [{"points": [[0.2, 0.2, 0.5], [0.5, 0.5, 0.7], [0.7, 0.3, 0.5]],
                                          "width": 0.01, "color": "#222"}]})),
        _ann(id="e", kind="highlight", cfi_range="epubcfi(/6/4)"),   # EPUB-only → skipped
    ]
    out = tmp_path / "out.pdf"
    src_before = src.read_bytes()
    annotate_export.export_annotated(str(src), anns, out_path=str(out), mode="copy")
    assert src.read_bytes() == src_before          # copy mode leaves the original untouched
    assert out.exists()

    doc = fitz.open(str(out))
    try:
        p1, p2 = doc.load_page(0), doc.load_page(1)
        kinds = sorted(a.type[1] for a in p1.annots())          # type = (n, "Highlight"/…)
        assert "Highlight" in kinds and "Underline" in kinds and "Text" in kinds
        assert len(p2.get_drawings()) >= 1                      # the ink filled-vector path
    finally:
        doc.close()


def test_export_inplace_modifies_original(tmp_path):
    src = _make_pdf(tmp_path / "src.pdf")
    before = len(list(fitz.open(str(src)).load_page(0).annots()))
    anns = [_ann(kind="highlight", page=1, rect=json.dumps([[0.1, 0.1, 0.3, 0.05]]), color="#ffd54a")]
    annotate_export.export_annotated(str(src), anns, mode="inplace")
    after = len(list(fitz.open(str(src)).load_page(0).annots()))
    assert after == before + 1


def test_export_skips_out_of_range_pages(tmp_path):
    src = _make_pdf(tmp_path / "src.pdf", pages=1)
    anns = [_ann(kind="highlight", page=9, rect=json.dumps([[0.1, 0.1, 0.3, 0.05]]))]
    out = tmp_path / "out.pdf"
    annotate_export.export_annotated(str(src), anns, out_path=str(out), mode="copy")
    assert len(list(fitz.open(str(out)).load_page(0).annots())) == 0   # page 9 doesn't exist


# ── the HTTP routes ───────────────────────────────────────────────────────────
@pytest.fixture
def app_and_ids(tmp_path, monkeypatch):
    from catalogue.webui.web import create_app
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    db_path = tmp_path / "exp.db"
    app = create_app(db_path)
    pdf = _make_pdf(tmp_path / "book.pdf")
    conn = sqlite3.connect(db_path); conn.execute("PRAGMA foreign_keys = ON")
    eid = conn.execute("INSERT INTO edition (title) VALUES ('Book')").lastrowid
    hid = conn.execute("INSERT INTO holding (edition_id, form, file_path, text_status) "
                       "VALUES (?, 'electronic', ?, 'ocr_good')", (eid, str(pdf))).lastrowid
    conn.commit(); conn.close()
    return app, eid, hid, db_path


def test_route_export_copy_returns_pdf(app_and_ids):
    app, eid, hid, db_path = app_and_ids
    conn = sqlite3.connect(db_path)
    store = rs.SqliteReaderStateStore(conn)
    store.apply_annotation(id="k", holding_id=hid, kind="ink", page=1,
                           ink=json.dumps({"strokes": [{"points": [[0.2, 0.2, 0.5], [0.6, 0.6, 0.6]],
                                                        "width": 0.01, "color": "#222"}]}),
                           updated_at="2026-06-27T10:00:00Z")
    conn.commit(); conn.close()

    c = app.test_client()
    r = c.get(f"/holding/{hid}/annotated.pdf")
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.data[:5] == b"%PDF-"


def test_route_export_409_when_no_marks(app_and_ids):
    app, eid, hid, db_path = app_and_ids
    c = app.test_client()
    assert c.get(f"/holding/{hid}/annotated.pdf").status_code == 409   # nothing to bake in


def test_route_inplace_writes(app_and_ids):
    app, eid, hid, db_path = app_and_ids
    conn = sqlite3.connect(db_path)
    rs.SqliteReaderStateStore(conn).apply_annotation(
        id="h", holding_id=hid, kind="highlight", page=1,
        rect=json.dumps([[0.1, 0.1, 0.3, 0.05]]), color="#ffd54a", updated_at="2026-06-27T10:00:00Z")
    conn.commit(); conn.close()

    c = app.test_client()
    r = c.post(f"/holding/{hid}/annotated")        # test client requests look local (127.0.0.1)
    assert r.status_code == 200 and r.get_json()["written"] is True
