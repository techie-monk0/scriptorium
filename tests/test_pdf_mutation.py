"""The shared PDF-writing mechanism (`pdf_mutation.write_pdf`) + the outline writer
(`outline_export.OutlineWrite`), exercised on real PyMuPDF documents so the bytes actually round-trip.
"""
from __future__ import annotations

import pytest

fitz = pytest.importorskip("fitz")

from catalogue.webui import outline_export
from catalogue.webui.outline_store import InMemoryOutlineStore, OutlineStore
from catalogue.webui.pdf_mutation import PdfMutation, write_pdf


def _make_pdf(path, pages=5):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1}")
    doc.save(str(path))
    doc.close()


def _toc(path):
    doc = fitz.open(str(path))
    try:
        return doc.get_toc()
    finally:
        doc.close()


def test_outline_write_copy_bakes_toc_into_a_new_file(tmp_path):
    src = tmp_path / "src.pdf"
    out = tmp_path / "out.pdf"
    _make_pdf(src)
    entries = [(1, "Chapter One", 1), (2, "Section 1.1", 2), (1, "Chapter Two", 4)]

    result = outline_export.export_with_outline(str(src), entries, out_path=str(out), mode="copy")

    assert result == str(out)
    assert _toc(src) == [], "copy mode leaves the original untouched"
    assert _toc(out) == [[1, "Chapter One", 1], [2, "Section 1.1", 2], [1, "Chapter Two", 4]]


def test_outline_write_inplace_writes_back(tmp_path):
    src = tmp_path / "src.pdf"
    _make_pdf(src)
    result = outline_export.export_with_outline(str(src), [(1, "Intro", 1)], mode="inplace")
    assert result == str(src)
    assert _toc(src) == [[1, "Intro", 1]]


def test_outline_entries_accept_tuple_dict_and_object(tmp_path):
    src = tmp_path / "src.pdf"
    out = tmp_path / "out.pdf"
    _make_pdf(src)

    class Entry:
        def __init__(self, level, title, page):
            self.level, self.title, self.page = level, title, page

    entries = [(1, "A", 1), {"level": 1, "title": "B", "page": 2}, Entry(1, "C", 3)]
    outline_export.export_with_outline(str(src), entries, out_path=str(out), mode="copy")
    assert _toc(out) == [[1, "A", 1], [1, "B", 2], [1, "C", 3]]


def test_outline_page_clamped_into_range(tmp_path):
    src = tmp_path / "src.pdf"
    out = tmp_path / "out.pdf"
    _make_pdf(src, pages=3)
    outline_export.export_with_outline(str(src), [(1, "Way past end", 99), (1, "Zeroth", 0)],
                                       out_path=str(out), mode="copy")
    assert _toc(out) == [[1, "Way past end", 3], [1, "Zeroth", 1]]


def test_write_pdf_rejects_bad_mode_and_missing_out(tmp_path):
    src = tmp_path / "src.pdf"
    _make_pdf(src)
    with pytest.raises(ValueError):
        write_pdf(str(src), [], mode="bogus")
    with pytest.raises(ValueError):
        write_pdf(str(src), [], mode="copy")  # copy needs out_path


def test_mutations_compose_in_one_save(tmp_path):
    """Two PdfMutations (outline + a marker mutation) applied in one write — proves the shared
    mechanism composes features into a single save."""
    src = tmp_path / "src.pdf"
    out = tmp_path / "out.pdf"
    _make_pdf(src)

    class StampFirstPage:
        def apply(self, doc):
            doc.load_page(0).insert_text((72, 144), "STAMPED")

    assert isinstance(outline_export.OutlineWrite([]), PdfMutation)  # structural conformance
    write_pdf(str(src), [outline_export.OutlineWrite([(1, "Top", 1)]), StampFirstPage()],
              out_path=str(out), mode="copy")

    assert _toc(out) == [[1, "Top", 1]]
    assert "STAMPED" in fitz.open(str(out)).load_page(0).get_text()


def test_outline_store_roundtrips_and_feeds_the_writer(tmp_path):
    """The store's entries feed `export_with_outline` directly (same dict shape), proving the
    authoring-storage seam and the bake mechanism line up."""
    store = InMemoryOutlineStore()
    assert isinstance(store, OutlineStore)          # structural conformance to the port
    assert store.get_outline(7) == []               # empty by default

    store.set_outline(7, [{"level": 1, "title": "Preface", "page": 1},
                          {"level": 1, "title": "Body", "page": 3}])
    src = tmp_path / "src.pdf"; out = tmp_path / "out.pdf"
    _make_pdf(src)
    outline_export.export_with_outline(str(src), store.get_outline(7), out_path=str(out), mode="copy")
    assert _toc(out) == [[1, "Preface", 1], [1, "Body", 3]]
