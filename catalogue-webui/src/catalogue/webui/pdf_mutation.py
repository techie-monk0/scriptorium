"""The shared "write into a holding's PDF" mechanism.

Several features persist a change into the *same* underlying PDF: baking the reader's annotations in
(`annotate_export`), authoring a table-of-contents outline (`outline_export`), and — later — an
OCR'd text layer replacing the original. They all share one delicate envelope: open the file, apply
the change, and save it either as a NEW copy (the original untouched — the safe default) or in place
(incremental, an explicit opt-in). That envelope lives here, once, so no feature re-derives the
copy/in-place/save semantics and they compose (write an outline AND flatten annotations in one save).

The abstraction is `PdfMutation` — one persistent edit applied to an open PyMuPDF document. A feature
supplies *what* to change; this module owns *how* it is opened and persisted. Because callers depend
on the `PdfMutation` seam and the `write_pdf` executor rather than on any feature's internals, an
implementation can be swapped (or a new PDF-writing feature added) without touching the executor or
the other features.

Layered/pure: `write_pdf(src, mutations, …)` takes a source path + mutation objects + a mode; it
reads neither the DB nor Flask. Routes gather inputs (file via the file-source layer, records via the
relevant store) and call it.

### Technical details

`write_pdf(src_path, mutations, *, out_path=None, mode="copy") -> str`:
  * `mode="copy"`    → save to `out_path` (required) with `garbage=3, deflate=True`; returns `out_path`.
  * `mode="inplace"` → `doc.save(src_path, incremental=True, encryption=PDF_ENCRYPT_KEEP)`; returns
    `src_path`. Incremental append keeps the save cheap and preserves the original bytes/signature —
    important because a holding's text-fingerprint identity must survive the edit.
Mutations run in list order against the one open `fitz.Document`, then a single save persists them all.
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class PdfMutation(Protocol):
    """One persistent edit applied to an open PyMuPDF document (`fitz.Document`).

    Implementations own only *what* to change (draw annotations, set the outline, replace a page's
    text layer, …); opening, saving, copy-vs-in-place, and error/encryption handling belong to
    `write_pdf`. This is the abstraction layer the PDF-writing features depend on.
    """

    def apply(self, doc) -> None: ...


def write_pdf(src_path, mutations, *, out_path=None, mode="copy"):
    """Apply `mutations` (in order) to the PDF at `src_path` and persist per `mode`.

    mode='copy'    → write to `out_path` (required), leaving the original untouched. Returns out_path.
    mode='inplace' → save the edits back into `src_path` itself (incremental). Returns src_path.

    Raises ValueError on a bad mode / a missing out_path in copy mode. The caller owns choosing and
    serving the result.
    """
    import fitz

    if mode not in ("copy", "inplace"):
        raise ValueError("mode must be 'copy' or 'inplace'")
    if mode == "copy" and not out_path:
        raise ValueError("copy mode needs an out_path")

    doc = fitz.open(src_path)
    try:
        for mutation in mutations:
            mutation.apply(doc)
        if mode == "inplace":
            doc.save(src_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
            return src_path
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        doc.save(out_path, garbage=3, deflate=True)
        return out_path
    finally:
        doc.close()
