"""Step-2 regression tests for extraction (§4.7, §4.8c step 1)."""
from __future__ import annotations

import unicodedata
import zipfile
from pathlib import Path

from catalogue.services.extract import extract


def _make_epub(path: Path, html_bodies: list[str]) -> None:
    """Minimal EPUB-shaped zip — enough for our extractor."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", "<container/>")
        for i, body in enumerate(html_bodies):
            z.writestr(
                f"OEBPS/ch{i}.xhtml",
                f"<html><body>{body}</body></html>",
            )


def _make_spine_epub(path: Path) -> None:
    """EPUB whose content files are written to the ZIP in SCRAMBLED order but whose
    OPF spine lists the true reading order (title page first, body later)."""
    opf = (
        '<package><manifest>'
        '<item id="body" href="body.xhtml"/>'
        '<item id="title" href="title.xhtml"/>'
        '<item id="copyr" href="copyright.xhtml"/>'
        '</manifest><spine>'
        '<itemref idref="title"/><itemref idref="copyr"/><itemref idref="body"/>'
        '</spine></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="content.opf"/>'
                   '</rootfiles></container>')
        z.writestr("content.opf", opf)
        # written body-first (scrambled), like a real mis-ordered EPUB
        z.writestr("body.xhtml", "<html><body><p>BODY chapter sixteen text</p></body></html>")
        z.writestr("title.xhtml", "<html><body><p>TITLEPAGE The Real Book Title</p></body></html>")
        z.writestr("copyright.xhtml", "<html><body><p>COPYRIGHT ISBN 978-1-61429-893-9</p></body></html>")


def test_epub_extracts_in_spine_reading_order(tmp_path):
    p = tmp_path / "scrambled.epub"
    _make_spine_epub(p)
    r = extract(p)
    assert r is not None
    # title page must come BEFORE the copyright page, which comes BEFORE the body —
    # not the ZIP (body-first) order.
    i_title = r.text.index("TITLEPAGE")
    i_copy = r.text.index("COPYRIGHT")
    i_body = r.text.index("BODY chapter")
    assert i_title < i_copy < i_body


def test_unknown_extension_returns_none(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    assert extract(p) is None


def test_epub_extracts_text(tmp_path):
    p = tmp_path / "book.epub"
    _make_epub(p, ["<p>The way of the bodhisattva.</p>", "<p>Chapter two.</p>"])
    r = extract(p)
    assert r is not None
    assert "way of the bodhisattva" in r.text
    assert "Chapter two" in r.text
    assert r.producer == "epub"
    assert r.is_image_only is False


def test_epub_strips_scripts_and_styles(tmp_path):
    p = tmp_path / "book.epub"
    _make_epub(p, [
        "<style>body { color: red; }</style>"
        "<script>alert('x');</script>"
        "<p>visible prose</p>"
    ])
    r = extract(p)
    assert r is not None
    assert "visible prose" in r.text
    assert "alert" not in r.text
    assert "color: red" not in r.text


def test_epub_skips_head_title_metadata(tmp_path):
    """A <head><title> is document metadata, NOT reading-order body text. EPUB
    publishers routinely leave a stale/templated <title> on the cover page (real
    case: a leftover "The Diamond Cutter Sutra" on a Chittamani Tara book). Since the
    cover is first in spine order, that string would otherwise become line 1 of the
    extraction and poison title recognition. It must be dropped; the real body title
    must survive."""
    p = tmp_path / "templated_cover.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr(
            "OEBPS/cover.xhtml",
            "<html><head><title>The Diamond Cutter Sutra</title>"
            '<meta name="x" content="y"/></head>'
            "<body><h1>The Secret Revelations of Chittamani Tara</h1></body></html>",
        )
    r = extract(p)
    assert r is not None
    assert "Diamond Cutter Sutra" not in r.text          # stale <title> dropped
    assert "Secret Revelations of Chittamani Tara" in r.text   # real body title kept


# ── §4.8c step 1: NFC normalization is the FIRST post-extraction step ─────
def test_epub_text_is_nfc_normalized(tmp_path):
    """OCR / hand-typed XHTML can emit decomposed combining marks
    (`a`+U+0304 instead of precomposed `ā`). The extractor must NFC the
    result so downstream matching and FTS see a single canonical form."""
    decomposed = "a" + "̄" + "rya"   # decomposed `ārya`
    precomposed = "ārya"
    assert decomposed != precomposed       # different byte strings…

    p = tmp_path / "book.epub"
    _make_epub(p, [f"<p>The word {decomposed} appears.</p>"])
    r = extract(p)
    assert r is not None
    assert precomposed in r.text            # …same NFC form after extract
    # And the raw decomposed sequence is gone:
    assert "ārya" not in r.text


def test_empty_epub_is_image_only(tmp_path):
    p = tmp_path / "empty.epub"
    _make_epub(p, [""])
    r = extract(p)
    assert r is not None
    assert r.is_image_only is True


def test_pdf_without_fitz_falls_back_to_image_only(tmp_path, monkeypatch):
    """If PyMuPDF isn't installed, PDF extraction must NOT crash — it
    returns image_only so the sweep keeps going and digitization handles it
    at Step 6 (§4.7 step 3)."""
    # Simulate "fitz not installed" even on machines where it is.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "fitz":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    p = tmp_path / "scan.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n/Producer (Tesseract)\n%%EOF")
    r = extract(p)
    assert r is not None
    assert r.is_image_only is True
    assert r.producer and "Tesseract" in r.producer
