"""Ingest-back (catalogue.webui.annotate_ingest) — the inverse of annotate_export: read a PDF's
embedded text marks back into the reader-state store (reader plan N6). Ink is NOT recoverable
(it's a flattened drawing), by design.
"""
from __future__ import annotations

import json
import pytest

from catalogue.db_store import reader_state as rs
from catalogue.webui import annotate_export, annotate_ingest

fitz = pytest.importorskip("fitz")


def _ann(**kw):
    base = dict(id="a", holding_id=1, kind=None, cfi_range=None, page=None, rect=None,
                color=None, note_text=None, ink=None, created_at=None, updated_at=None,
                deleted_at=None, rev=1)
    base.update(kw)
    return rs.Annotation(**base)


def _make_pdf(path, pages=1):
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page(width=400, height=600)
        pg.insert_text((40, 80), f"Page {i + 1} text to mark up.")
    doc.save(str(path)); doc.close()
    return path


def test_read_pdf_annotations_recovers_text_marks_not_ink(tmp_path):
    src = _make_pdf(tmp_path / "src.pdf")
    anns = [
        _ann(id="h", kind="highlight", page=1, rect=json.dumps([[0.1, 0.12, 0.4, 0.04]]), color="#ffd54a"),
        _ann(id="u", kind="underline", page=1, rect=json.dumps([[0.1, 0.2, 0.4, 0.03]])),
        _ann(id="n", kind="note", page=1, rect=json.dumps([0.5, 0.5]), note_text="see this"),
        _ann(id="k", kind="ink", page=1,
             ink=json.dumps({"strokes": [{"points": [[0.2, 0.2, 0.5], [0.5, 0.5, 0.7]], "width": 0.01}]})),
    ]
    out = tmp_path / "out.pdf"
    annotate_export.export_annotated(str(src), anns, out_path=str(out), mode="copy")

    got = annotate_ingest.read_pdf_annotations(str(out))
    assert sorted(g["kind"] for g in got) == ["highlight", "note", "underline"]   # ink NOT recovered
    note = next(g for g in got if g["kind"] == "note")
    assert note["note_text"] == "see this"
    hl = next(g for g in got if g["kind"] == "highlight")
    r0 = json.loads(hl["rect"])[0]
    assert abs(r0[0] - 0.1) < 0.03 and abs(r0[1] - 0.12) < 0.03    # round-trips to ~the stored rect


def test_ingest_into_store_applies_marks(tmp_path):
    src = _make_pdf(tmp_path / "src.pdf")
    anns = [_ann(id="h", kind="highlight", page=1, rect=json.dumps([[0.1, 0.12, 0.4, 0.04]]), color="#ffd54a")]
    out = tmp_path / "out.pdf"
    annotate_export.export_annotated(str(src), anns, out_path=str(out), mode="copy")

    store = rs.InMemoryReaderStateStore()
    store.ensure_schema()
    dicts = annotate_ingest.read_pdf_annotations(str(out))
    n = annotate_ingest.ingest_into_store(store, holding_id=7, dicts=dicts, content_hash="ch-x")
    assert n == 1
    rows = store.annotations_for_holding(7)
    assert len(rows) == 1 and rows[0].kind == "highlight" and rows[0].content_hash == "ch-x"
