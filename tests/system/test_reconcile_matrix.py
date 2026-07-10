"""End-to-end reconcile matrix through the REAL pipeline (scan_dir → extract →
content_fingerprint → classify): {move, delete, annotate, re-OCR} ×
{text-PDF, image-only-PDF, text-EPUB}. Uses real files on disk."""
from __future__ import annotations

import zipfile
import pytest

from catalogue.db_store import init_db
from catalogue.services import reconcile

fitz = pytest.importorskip("fitz")

LONG = "avalokiteshvara prajnaparamita heart sutra emptiness form feeling " * 4


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "cat.db")
    yield conn
    conn.close()


# ── builders ──────────────────────────────────────────────────────────────────
def make_text_pdf(p):
    d = fitz.open(); pg = d.new_page(); pg.insert_text((72, 72), LONG)
    d.save(str(p)); d.close()


def make_image_pdf(p):                       # a page with a drawing but NO text
    d = fitz.open(); pg = d.new_page(); pg.draw_rect(fitz.Rect(50, 50, 300, 300))
    d.save(str(p)); d.close()


def make_text_epub(p):
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr("OEBPS/ch1.xhtml", f"<html><body><p>{LONG}</p></body></html>")


BUILDERS = {"pdf_text": (make_text_pdf, ".pdf"),
            "pdf_image": (make_image_pdf, ".pdf"),
            "epub": (make_text_epub, ".epub")}


def _ingest(db, lib):
    reconcile.reconcile(db, reconcile.scan_dir(db, [str(lib)]))   # 'new' → enqueued
    for (iid,) in db.execute(
        "SELECT id FROM review_queue WHERE item_type='ingest' AND status='pending'"
    ).fetchall():
        reconcile.apply_decision(db, iid, "distinct")


def _disp(db, lib, path):
    plan = reconcile.classify(db, reconcile.scan_dir(db, [str(lib)]))
    return next((d for d in plan if d["path"] == str(path)), None)


# ── matrix ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["pdf_text", "pdf_image", "epub"])
def test_move_repoints(db, tmp_path, kind):
    build, ext = BUILDERS[kind]
    lib = tmp_path / "lib"; (lib / "sub").mkdir(parents=True)
    f = lib / f"book{ext}"; build(f)
    _ingest(db, lib)
    # the holding's content_hash basis is text for text-layer files, bytes for image-only
    ch = db.execute("SELECT content_hash FROM holding").fetchone()[0]
    assert ch.startswith("t:" if kind != "pdf_image" else "b:")

    moved = lib / "sub" / f"book{ext}"
    f.rename(moved)
    d = _disp(db, lib, moved)
    assert d and d["kind"] == "moved"


@pytest.mark.parametrize("kind", ["pdf_text", "pdf_image", "epub"])
def test_delete_flags_missing(db, tmp_path, kind):
    build, ext = BUILDERS[kind]
    lib = tmp_path / "lib"; lib.mkdir()
    f = lib / f"book{ext}"; build(f)
    _ingest(db, lib)
    f.unlink()
    d = _disp(db, lib, f)                     # disposition keyed on the holding's old path
    assert d and d["kind"] == "missing"


def test_annotate_text_pdf_is_annotated(db, tmp_path):
    lib = tmp_path / "lib"; lib.mkdir()
    f = lib / "book.pdf"; make_text_pdf(f)
    _ingest(db, lib)
    # add a highlight (new bytes, page text unchanged) via incremental save
    d0 = fitz.open(str(f)); d0[0].add_highlight_annot(fitz.Rect(70, 68, 300, 82))
    d0.save(str(f), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP); d0.close()
    d = _disp(db, lib, f)
    assert d and d["kind"] == "annotated"     # text layer identical → not a content change


def test_image_pdf_byte_change_is_content_changed(db, tmp_path):
    # An image-only file has no text to compare, so any byte change reads as a
    # content change (the documented asymmetry).
    lib = tmp_path / "lib"; lib.mkdir()
    f = lib / "scan.pdf"; make_image_pdf(f)
    _ingest(db, lib)
    d0 = fitz.open(str(f)); d0[0].draw_circle(fitz.Point(150, 150), 40)
    d0.save(str(f), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP); d0.close()
    d = _disp(db, lib, f)
    assert d and d["kind"] == "content_changed"


@pytest.mark.parametrize("mutate,expected", [
    # bookmark/outline lives in the doc catalog, not page text → invisible
    (lambda d: d.set_toc([[1, "My Bookmark", 1]]), "annotated"),
    # highlight is an annotation object with no text payload → invisible
    (lambda d: d[0].add_highlight_annot(fitz.Rect(70, 68, 300, 82)), "annotated"),
    # typed FreeText note IS an annotation — stripped from the fingerprint, so it
    # too is ignored for identity (not treated as a content change).
    (lambda d: d[0].add_freetext_annot(fitz.Rect(100, 300, 400, 360), "TYPED NOTE"),
     "annotated"),
])
def test_pdf_bookmark_vs_annotation_vs_typed(db, tmp_path, mutate, expected):
    lib = tmp_path / "lib"; lib.mkdir()
    f = lib / "book.pdf"; make_text_pdf(f)
    _ingest(db, lib)
    d0 = fitz.open(str(f)); mutate(d0)
    d0.save(str(f), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP); d0.close()
    d = _disp(db, lib, f)
    assert d and d["kind"] == expected


def test_reocr_image_pdf_to_text_is_content_changed(db, tmp_path):
    # Re-OCR: an image-only scan gains a text layer (same path) → content change.
    lib = tmp_path / "lib"; lib.mkdir()
    f = lib / "scan.pdf"; make_image_pdf(f)
    _ingest(db, lib)
    make_text_pdf(f)                          # replace in place with a text-layer version
    d = _disp(db, lib, f)
    assert d and d["kind"] == "content_changed"
