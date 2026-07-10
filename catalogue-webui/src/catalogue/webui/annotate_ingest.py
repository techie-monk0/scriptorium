"""Ingest a PDF's embedded annotations BACK into the reader-state store — the inverse of
`annotate_export`, for the "file is self-sufficient" property (reader plan N6).

Use case: a file carries marks (because it was exported/embedded earlier) but the DB has none of
them (a fresh machine, or after the holding's marks were lost). Reading the PDF's standard
annotations back recovers them so the structured store is rebuilt from the file.

Recoverable kinds (standard PDF annotations the export writes):
  * /Highlight  → highlight   (quad points → page-relative rects)
  * /Underline  → underline
  * /StrikeOut  → strikeout
  * /Text       → note        (sticky-note anchor + its text)

NOT recoverable: **ink**. The export draws handwriting as a FILLED VECTOR PATH (to preserve
per-point pressure), not a structured /Ink annotation — so it can't be parsed back into strokes.
Ink durability therefore rides on the synced DB / a sidecar, not the flattened PDF. (Documented
asymmetry; see `annotate_export`.)

Coordinates are page-relative 0..1, top-left origin — PyMuPDF's own convention — so mapping is a
straight divide by the page box, mirroring the export's multiply.

Pure + layered: `read_pdf_annotations(path)` reads a file and returns dicts; it touches neither the
DB nor Flask. `ingest_into_store(store, holding_id, dicts)` applies them through the reader-state
PORT (new client UUIDs, since the PDF doesn't carry ours). The caller owns the transaction.
"""
from __future__ import annotations

import json
import uuid

_KIND = {"Highlight": "highlight", "Underline": "underline",
         "StrikeOut": "strikeout", "Text": "note"}


def _color_hex(colors):
    """PyMuPDF annot.colors {'stroke': (r,g,b)} → '#rrggbb', or None."""
    rgb = (colors or {}).get("stroke")
    if not rgb:
        return None
    try:
        return "#" + "".join("%02x" % max(0, min(255, round(c * 255))) for c in rgb[:3])
    except Exception:
        return None


def _rects_from_vertices(verts, W, H):
    """Text-markup quad points (groups of 4) → page-relative [[x,y,w,h], …] (0..1, top-left)."""
    out = []
    if not verts:
        return out
    for i in range(0, len(verts) - 3, 4):
        quad = verts[i:i + 4]
        xs = [p[0] for p in quad]
        ys = [p[1] for p in quad]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        if W and H:
            out.append([x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H])
    return out


def read_pdf_annotations(pdf_path) -> "list[dict]":
    """Every recoverable annotation in `pdf_path` as a dict {kind, page (1-based), rect (JSON str),
    color, note_text}. Ink (a drawn path, not an annotation) is invisible here, by design."""
    import fitz
    doc = fitz.open(pdf_path)
    out: "list[dict]" = []
    try:
        for pno in range(doc.page_count):
            page = doc.load_page(pno)
            W, H = page.rect.width, page.rect.height
            for annot in (page.annots() or []):
                kind = _KIND.get(annot.type[1])
                if kind is None:
                    continue
                rec = {"kind": kind, "page": pno + 1, "color": _color_hex(annot.colors),
                       "rect": None, "note_text": None}
                if kind == "note":
                    r = annot.rect
                    rec["rect"] = json.dumps([r.x0 / W if W else 0.0, r.y0 / H if H else 0.0])
                    rec["note_text"] = (annot.info or {}).get("content") or None
                else:
                    rects = _rects_from_vertices(annot.vertices, W, H)
                    if not rects:
                        continue
                    rec["rect"] = json.dumps(rects)
                out.append(rec)
        return out
    finally:
        doc.close()


def ingest_into_store(store, holding_id, dicts, *, content_hash=None, now=None) -> int:
    """Apply read-back annotation dicts to a `ReaderStateStore` as fresh marks (new client UUIDs,
    since the PDF doesn't carry ours). Returns the count applied. Does NOT commit. Intended for the
    empty-DB case — it does not dedupe against existing rows."""
    from datetime import datetime, timezone
    ts = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for d in dicts:
        store.apply_annotation(
            id=str(uuid.uuid4()), holding_id=holding_id, kind=d.get("kind"),
            page=d.get("page"), rect=d.get("rect"), color=d.get("color"),
            note_text=d.get("note_text"), updated_at=ts, created_at=ts,
            content_hash=content_hash)
        n += 1
    return n
