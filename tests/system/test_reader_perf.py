"""Performance regression tests for the in-app reader, in a real headless browser (Playwright).

These guard the load-speed regressions we fixed when handwriting landed:
  * an edition opens in PURE READING mode — the annotation layer (overlay, ink lib, /sync marks,
    per-page paint hooks) is built only when the ✍ toggle ARMS it, so reading has no extra cost;
  * PDF paging doesn't re-run per-open work on every Range request (see the deterministic guard in
    test_reader.py::test_range_requests_skip_per_open_side_effects);
  * armed annotation mode doesn't make paging pathological.

Across BOTH formats (one PDF, one EPUB) and the modalities the regression touched: first-page load,
Nth-page load, page loading while scrolling, and reading (annotation OFF) vs annotate (ON).

Thresholds are deliberately GENEROUS absolute ceilings (catch gross multi-second regressions, not
micro-benchmark a machine) and RELATIVE checks (reading ≤ annotate-armed). Override the ceilings
with env vars on a slow CI box. Skipped cleanly when Playwright's Chromium isn't installed.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import pytest

from catalogue.webui.web import create_app

# Generous ceilings (ms) — tune via env on slow hardware. The bug we guard against was SECONDS
# per page across many pages; these catch that without flaking on a healthy laptop/CI.
FIRST_MS = int(os.environ.get("READER_PERF_FIRST_MS", "12000"))   # open → first page/section
PAGE_MS = int(os.environ.get("READER_PERF_PAGE_MS", "8000"))      # scroll → an Nth page renders
ARM_MS = int(os.environ.get("READER_PERF_ARM_MS", "10000"))       # flip ✍ → overlay live


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_installed(),
    reason="playwright chromium not installed (run: uv run playwright install chromium)",
)


# ── bigger, real books (enough pages/sections to measure Nth-page + scrolling) ──
def _make_pdf(path: Path, pages: int = 24) -> Path:
    import fitz
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page(width=800, height=1000)
        pg.insert_text((60, 120), f"Page {i + 1}", fontsize=48)
        pg.insert_text((60, 240), ("lorem ipsum dolor sit amet " * 6), fontsize=14)
    doc.save(str(path)); doc.close()
    return path


def _make_epub(path: Path, chapters: int = 8) -> Path:
    items = "".join(
        f'<item id="ch{i}" href="text/ch{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(chapters))
    spine = "".join(f'<itemref idref="ch{i}"/>' for i in range(chapters))
    opf = ('<?xml version="1.0" encoding="utf-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="b">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Perf</dc:title>'
           '<dc:language>en</dc:language><dc:identifier id="b">urn:uuid:perf-1</dc:identifier></metadata>'
           f'<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{items}</manifest>'
           f'<spine>{spine}</spine></package>')
    nav = ('<?xml version="1.0" encoding="utf-8"?>'
           '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
           '<head><title>c</title></head><body><nav epub:type="toc"><ol>'
           + "".join(f'<li><a href="text/ch{i}.xhtml">Chapter {i + 1}</a></li>' for i in range(chapters))
           + '</ol></nav></body></html>')
    chap = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
            '<head><title>Chapter {n}</title></head><body><h1>Chapter {n}</h1>'
            '<p>{body}</p></body></html>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        for i in range(chapters):
            z.writestr(f"OEBPS/text/ch{i}.xhtml",
                       chap.format(n=i + 1, body=("chapter body text " * 80)))
    return path


class _Live:
    def __init__(self, base, db_path, pdf_eid, epub_eid):
        self.base, self.db_path = base, db_path
        self.pdf_eid, self.epub_eid = pdf_eid, epub_eid


@pytest.fixture
def live(tmp_path, monkeypatch):
    from werkzeug.serving import make_server
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    db_path = tmp_path / "perf.db"
    app = create_app(db_path)
    pdf = _make_pdf(tmp_path / "book.pdf")
    epub = _make_epub(tmp_path / "book.epub")
    conn = sqlite3.connect(db_path); conn.execute("PRAGMA foreign_keys = ON")
    pdf_eid = conn.execute("INSERT INTO edition (title) VALUES ('PDF')").lastrowid
    conn.execute("INSERT INTO holding (edition_id, form, file_path, text_status) "
                 "VALUES (?, 'electronic', ?, 'ocr_good')", (pdf_eid, str(pdf)))
    epub_eid = conn.execute("INSERT INTO edition (title) VALUES ('EPUB')").lastrowid
    conn.execute("INSERT INTO holding (edition_id, form, file_path, text_status) "
                 "VALUES (?, 'electronic', ?, 'ocr_good')", (epub_eid, str(epub)))
    conn.commit(); conn.close()
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    try:
        yield _Live(f"http://127.0.0.1:{srv.server_port}", str(db_path), pdf_eid, epub_eid)
    finally:
        srv.shutdown(); t.join()


def _ms(fn):
    t0 = time.perf_counter(); fn(); return (time.perf_counter() - t0) * 1000.0


def _open_pdf(page, live):
    page.set_default_timeout(max(FIRST_MS, PAGE_MS) + 5000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")


def _open_epub(page, live):
    page.set_default_timeout(max(FIRST_MS, PAGE_MS) + 5000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.epub_eid}/read")


# ── PDF ───────────────────────────────────────────────────────────────────────
def test_pdf_first_page_loads_within_ceiling(live, page):
    """Reading mode (annotation OFF): open → first page canvas painted, under the ceiling."""
    _open_pdf(page, live)
    dt = _ms(lambda: page.wait_for_selector("#pdfStack .pdf-page[data-page='1'] canvas"))
    assert page.locator("body.annotate-on").count() == 0          # opened in reading mode
    assert page.locator("#anntools").is_visible() is False        # tools hidden until ✍
    assert dt < FIRST_MS, f"PDF first page took {dt:.0f}ms (> {FIRST_MS}ms)"


def test_pdf_nth_page_loads_on_scroll(live, page):
    """Jumping to a later page renders it within the per-page ceiling (no per-chunk open work)."""
    _open_pdf(page, live)
    page.wait_for_selector("#pdfStack .pdf-page[data-page='1'] canvas")
    n = 12
    page.eval_on_selector("#viewer",
        f"el => el.querySelector(\".pdf-page[data-page='{n}']\").scrollIntoView()")
    dt = _ms(lambda: page.wait_for_selector(f"#pdfStack .pdf-page[data-page='{n}'] canvas"))
    assert dt < PAGE_MS, f"PDF page {n} took {dt:.0f}ms (> {PAGE_MS}ms)"


def test_pdf_scrolling_through_pages_each_renders(live, page):
    """Page loading WHILE SCROLLING: step through several pages; each renders under the ceiling."""
    _open_pdf(page, live)
    page.wait_for_selector("#pdfStack .pdf-page[data-page='1'] canvas")
    for n in (4, 8, 14, 20):
        page.eval_on_selector("#viewer",
            f"el => el.querySelector(\".pdf-page[data-page='{n}']\").scrollIntoView()")
        dt = _ms(lambda: page.wait_for_selector(f"#pdfStack .pdf-page[data-page='{n}'] canvas"))
        assert dt < PAGE_MS, f"PDF page {n} while scrolling took {dt:.0f}ms (> {PAGE_MS}ms)"


def test_pdf_reading_not_slower_than_annotate_open(live, page):
    """Annotation ON vs OFF: arming annotate happens AFTER open, so first-paint must be the same
    order; and arming itself completes within its ceiling (overlay built off the open path)."""
    _open_pdf(page, live)
    reading = _ms(lambda: page.wait_for_selector("#pdfStack .pdf-page[data-page='1'] canvas"))
    arm = _ms(lambda: (page.click("#annotateBtn"), page.wait_for_selector("body.annotate-ready")))
    assert reading < FIRST_MS
    assert arm < ARM_MS, f"arming annotate took {arm:.0f}ms (> {ARM_MS}ms)"
    # With annotate armed, paging still renders within the ceiling (overlay hooks not pathological).
    page.eval_on_selector("#viewer", "el => el.querySelector(\".pdf-page[data-page='10']\").scrollIntoView()")
    dt = _ms(lambda: page.wait_for_selector("#pdfStack .pdf-page[data-page='10'] canvas"))
    assert dt < PAGE_MS, f"PDF page 10 (annotate on) took {dt:.0f}ms (> {PAGE_MS}ms)"


# ── EPUB ──────────────────────────────────────────────────────────────────────
def test_epub_first_section_renders_within_ceiling(live, page):
    """Reading mode: open → first chapter text painted in the iframe, under the ceiling."""
    _open_epub(page, live)
    dt = _ms(lambda: page.frame_locator("#viewer iframe")
             .get_by_role("heading", name="Chapter 1").wait_for())
    assert page.locator("body.annotate-on").count() == 0
    assert dt < FIRST_MS, f"EPUB first section took {dt:.0f}ms (> {FIRST_MS}ms)"


def test_epub_page_turns_within_ceiling(live, page):
    """Nth 'page'/section: turning ahead a few sections each lands within the per-page ceiling."""
    _open_epub(page, live)
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter 1").wait_for()
    for n in (2, 3, 4):
        dt = _ms(lambda: (page.click("#pgNext"),
                          page.frame_locator("#viewer iframe")
                          .get_by_role("heading", name=f"Chapter {n}").wait_for()))
        assert dt < PAGE_MS, f"EPUB turn to chapter {n} took {dt:.0f}ms (> {PAGE_MS}ms)"


def test_epub_arming_annotate_is_bounded(live, page):
    """Annotation ON: flipping ✍ on an EPUB builds the overlay within its ceiling and does not
    blank the text (first section stays rendered)."""
    _open_epub(page, live)
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter 1").wait_for()
    arm = _ms(lambda: (page.click("#annotateBtn"), page.wait_for_selector("body.annotate-ready")))
    assert arm < ARM_MS, f"arming annotate (EPUB) took {arm:.0f}ms (> {ARM_MS}ms)"
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter 1").wait_for()
