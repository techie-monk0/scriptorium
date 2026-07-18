"""Export a PDF with the reader's annotations baked in — the third-party-tool path
(reader_module_plan.md Phase 7).

Our annotations are a synced DB-of-record (catalogue.db_store.reader_state), NOT embedded in the
file, so GoodReader / Acrobat / Apple Books never see them. This module flattens those records into
a PDF that any viewer renders, mapping each `kind` to a standard PDF construct:

  * highlight  → /Highlight annotation   (over the stored page+rect quads)
  * underline  → /Underline annotation
  * strikeout  → /StrikeOut annotation
  * note       → /Text (sticky-note) annotation at the stored anchor, carrying note_text
  * ink        → a FILLED VECTOR PATH drawn from the SAME perfect-freehand outline the reader
                 paints, so the handwriting looks IDENTICAL (pressure/width preserved). It is a
                 page drawing, not a separately-editable PDF /Ink annotation — the user's choice
                 of "faithful look" over "editable in Acrobat" (PDF /Ink can't carry per-point
                 pressure).

EPUB annotations are out of scope here (EPUB has no universal embedded-annotation standard — see
the deferred EPUB plan). Coordinates in the store are page-relative (0..1, top-left origin), which
is exactly PyMuPDF's coordinate convention, so mapping is a straight multiply by the page box.

Pure + layered: `export_annotated(src, annotations, …)` takes the source path + annotation DTOs and
writes a file; it reads neither the DB nor Flask. The route gathers inputs (file via the file-source
layer, marks via the reader-state store) and calls this. `mode='copy'` writes a NEW file (the
original is never touched — no per-stroke rewrite of a large PDF); `mode='inplace'` saves back into
the original incrementally (explicit opt-in).
"""
from __future__ import annotations

import json


def _color_rgb(hexstr, default=(1.0, 0.83, 0.29)):
    """'#rrggbb' → (r,g,b) floats 0..1 for PyMuPDF; falls back to a soft yellow."""
    s = (hexstr or "").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except Exception:
        return default


def _quads_from_rect_json(rect_json, W, H):
    """The stored highlight/underline rects JSON ([[x,y,w,h],…], 0..1) → page-point fitz.Rects."""
    import fitz
    out = []
    try:
        for r in json.loads(rect_json):
            x, y, w, h = r[0] * W, r[1] * H, r[2] * W, r[3] * H
            out.append(fitz.Rect(x, y, x + w, y + h))
    except Exception:
        pass
    return out


def _draw_ink(page, ann, W, H):
    """Draw one ink annotation as a filled vector path — the SAME perfect-freehand outline the
    web reader renders, so the exported handwriting is visually identical. Points are page-relative
    [x,y,pressure]; width is a fraction of page width (matches the reader's storage)."""
    from perfect_freehand import get_stroke
    try:
        data = json.loads(ann.ink or "{}")
    except Exception:
        return
    for st in (data.get("strokes") or []):
        pts = st.get("points") or []
        if len(pts) < 2:
            continue
        marker = st.get("mode") == "marker"
        size = (st.get("width") or 0.004) * W * (2.4 if marker else 1.0)
        # get_stroke on page-point coords → a closed outline polygon, same options as the reader.
        sample = [[p[0] * W, p[1] * H, (p[2] if len(p) > 2 else 0.5)] for p in pts]
        outline = get_stroke(sample, size=size, thinning=(0.2 if marker else 0.6),
                             smoothing=0.6, streamline=0.5)
        if len(outline) < 3:
            continue
        rgb = _color_rgb(st.get("color"), default=(0.13, 0.13, 0.13))
        shape = page.new_shape()
        shape.draw_polyline([(x, y) for x, y in outline])
        shape.finish(color=None, fill=rgb, fill_opacity=(0.4 if marker else 1.0), closePath=True)
        shape.commit()


class AnnotationFlatten:
    """A `PdfMutation` (`pdf_mutation.PdfMutation`) that bakes reader annotations into the PDF as
    standard constructs. Only PDF-anchored kinds are applied (page is not None); EPUB/cfi-only marks
    are skipped. This is the annotation feature's slice of the shared PDF-writing mechanism — the
    open/save/copy-vs-in-place envelope lives in `write_pdf`, so this owns only the drawing."""

    def __init__(self, annotations):
        self.annotations = annotations

    def apply(self, doc):
        import fitz
        npages = doc.page_count
        for ann in self.annotations:
            if ann.page is None:
                continue                          # EPUB/cfi-only — not exportable to PDF
            idx = int(ann.page) - 1               # stored page is 1-based
            if idx < 0 or idx >= npages:
                continue
            page = doc.load_page(idx)
            W, H = page.rect.width, page.rect.height
            kind = ann.kind
            if kind == "ink":
                _draw_ink(page, ann, W, H)
            elif kind == "note":
                try:
                    pt = json.loads(ann.rect or "[0.5,0.5]")
                    p = fitz.Point((pt[0] or 0.5) * W, (pt[1] or 0.5) * H)
                    a = page.add_text_annot(p, ann.note_text or "")
                    a.update()
                except Exception:
                    pass
            elif kind in ("highlight", "underline", "strikeout"):
                quads = _quads_from_rect_json(ann.rect, W, H)
                if not quads:
                    continue
                add = {"highlight": page.add_highlight_annot,
                       "underline": page.add_underline_annot,
                       "strikeout": page.add_strikeout_annot}[kind]
                try:
                    a = add(quads)
                    if a is not None and kind == "highlight":
                        a.set_colors(stroke=_color_rgb(ann.color)); a.update()
                except Exception:
                    pass


def export_annotated(src_path, annotations, *, out_path=None, mode="copy"):
    """Write `src_path` (a PDF) with `annotations` (reader_state.Annotation DTOs) baked in.

    mode='copy'    → write to `out_path` (required), leaving the original untouched. Returns out_path.
    mode='inplace' → save the marks back into `src_path` itself (incremental). Returns src_path.

    Only PDF-anchored kinds are applied (page is not None); EPUB/cfi-only marks are skipped. Delegates
    the open/apply/save to the shared `pdf_mutation.write_pdf`, passing annotation-flattening as one
    mutation — so this shares the exact copy/in-place envelope with outline authoring (and can be
    composed with it in a single save).
    """
    from catalogue.webui.pdf_mutation import write_pdf
    return write_pdf(src_path, [AnnotationFlatten(annotations)], out_path=out_path, mode=mode)


def has_pdf_annotations(annotations) -> bool:
    """Whether any mark would actually land in a PDF (PDF-anchored kind) — lets the route 400/skip
    an empty export cleanly."""
    return any(a.page is not None and a.kind in ("highlight", "underline", "strikeout", "note", "ink")
               for a in annotations)
