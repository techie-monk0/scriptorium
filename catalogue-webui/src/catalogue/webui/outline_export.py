"""Author a table-of-contents outline INTO a PDF — the persistent-bookmarks feature.

A holding's PDF may have a wrong outline, or none. This bakes an authored outline (the reader's
"Contents" the user edits) into the file via PyMuPDF `set_toc`, so *any* viewer — Preview, Acrobat,
Apple Books — shows it, not just our reader. It is the outline sibling of `annotate_export`: both
persist a synced overlay-of-record into the file through the SAME shared mechanism
(`pdf_mutation.write_pdf`), so the copy/in-place/save envelope is not re-derived and the two compose
(write the outline and flatten annotations in one save).

Where the authored entries are *stored* is a separate seam (`outline_store.OutlineStore`): today they
live as an overlay record synced like bookmarks (immutable file, offline + multi-device for free);
this `OutlineWrite` mutation only bakes a given list into the bytes. Because the store is an
abstraction, its implementation can change (overlay → read straight from the file's own outline, or a
different backend) without touching this writer or the reader that authors against it.

### Technical details

`OutlineWrite(entries)` is a `pdf_mutation.PdfMutation`. Each entry is normalised to PyMuPDF's
`[level, title, page]` rows (`level >= 1`, `page` 1-based, clamped to the document's page range) and
written with `doc.set_toc(...)`, which replaces the document outline wholesale. Entries may be
`(level, title, page)` tuples, mappings with those keys, or any object exposing `.level/.title/.page`
(e.g. `services.toc.TOCEntry`), so the caller isn't coupled to one entry type.
"""
from __future__ import annotations

from typing import Iterable


def _entry_fields(entry):
    """(level, title, page) from a tuple/list, a mapping, or a `.level/.title/.page` object.

    Missing level → 1; missing/None page → 1 (top-of-book). Titles stringify; blank titles are kept
    (the caller/validator decides), but pages < 1 clamp to 1.
    """
    if isinstance(entry, (tuple, list)):
        level = entry[0] if len(entry) > 0 else 1
        title = entry[1] if len(entry) > 1 else ""
        page = entry[2] if len(entry) > 2 else 1
    elif isinstance(entry, dict):
        level, title, page = entry.get("level", 1), entry.get("title", ""), entry.get("page", 1)
    else:
        level = getattr(entry, "level", 1)
        title = getattr(entry, "title", "")
        page = getattr(entry, "page", 1)
    try:
        level = max(1, int(level or 1))
    except (TypeError, ValueError):
        level = 1
    try:
        page = int(page) if page is not None else 1
    except (TypeError, ValueError):
        page = 1
    return level, ("" if title is None else str(title)), max(1, page)


class OutlineWrite:
    """A `PdfMutation` that writes an authored outline into the PDF (replaces `doc.set_toc`)."""

    def __init__(self, entries: Iterable):
        self.entries = list(entries)

    def apply(self, doc):
        npages = max(1, doc.page_count)
        toc = []
        for entry in self.entries:
            level, title, page = _entry_fields(entry)
            toc.append([level, title, min(page, npages)])   # clamp into the real page range
        doc.set_toc(toc)   # [] clears the outline — an explicit, valid state


def export_with_outline(src_path, entries, *, out_path=None, mode="copy"):
    """Convenience: bake `entries` as the PDF's outline via the shared writer.

    mode='copy'    → new file at `out_path` (original untouched). Returns out_path.
    mode='inplace' → write the outline back into `src_path`. Returns src_path.
    """
    from catalogue.webui.pdf_mutation import write_pdf
    return write_pdf(src_path, [OutlineWrite(entries)], out_path=out_path, mode=mode)
