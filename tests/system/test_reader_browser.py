"""Real-browser smoke tests for the in-app reader (reader_module_plan.md Phase 1).

The HTTP-level tests in test_reader.py prove the plumbing (routes, wiring, byte streaming,
position round-trip) but never execute the JavaScript. These tests drive a real headless
Chromium via Playwright against a live Flask server, so they verify the extracted engine
(static/reader/reader-core.js + vendor.js) ACTUALLY RUNS:

  * PDF: pdf.js loads + paints a page onto a <canvas> in the continuous stack
  * EPUB: epub.js parses the archive + renders chapter text into its iframe
  * reading position: scrolling the PDF persists a later page to reading_position

They need a valid PDF (built with PyMuPDF) and a structurally valid EPUB (built here), not
the fake byte blobs the HTTP tests use, because the engines really parse them.

Skipped automatically where the Playwright Chromium build isn't installed (so a plain
`uv run pytest` stays green on machines without `playwright install chromium`).
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


# ── skip cleanly when the browser binary isn't present ───────────────────────
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


# ── real, parseable book files ───────────────────────────────────────────────
def _make_pdf(path: Path, pages: int = 6) -> Path:
    """A genuine multi-page PDF (pdf.js must render it; multiple pages so a scroll moves
    the tracked page). PyMuPDF is already a dependency (cover previews).

    Pages are deliberately wider-than-tall (800x500): at fit-to-width they end up SHORTER
    than the test viewport, so a page can exceed the 50%-visible threshold the reader's
    page tracker uses to decide the 'current' page. (A tall portrait page scaled to full
    width can be taller than a short window and never cross 50% — a real edge case, but not
    what this test is checking.)"""
    import fitz
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page(width=800, height=500)
        pg.insert_text((50, 120), f"Page {i + 1}", fontsize=64)
    doc.set_toc([[1, "Chapter A", 1], [1, "Chapter B", 4]])   # a real PDF outline for the TOC test
    doc.save(str(path))
    doc.close()
    return path


def _make_epub(path: Path) -> Path:
    """A realistic, deliberately-awkward EPUB 3: the nav doc lives in its OWN folder
    (OEBPS/nav/nav.xhtml) while chapters live in OEBPS/text/, so the TOC hrefs are
    '../text/chN.xhtml#hN' — a different directory than the OPF AND a fragment. This is the
    shape where a plain rendition.display(href) can't resolve the spine item and the contents
    link silently does nothing; it exercises the robust resolver. Keeps an NCX too."""
    container = (
        '<?xml version="1.0"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles>\n'
        '</container>\n'
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '    <dc:title>Test Book</dc:title><dc:language>en</dc:language>\n'
        '    <dc:identifier id="bookid">urn:uuid:reader-test-0001</dc:identifier>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        '    <item id="nav" href="nav/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>\n'
        '    <item id="ch2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>\n'
        '  </manifest>\n'
        '  <spine toc="ncx"><itemref idref="ch1"/><itemref idref="ch2"/></spine>\n'
        '</package>\n'
    )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        '<head><title>Contents</title></head><body>\n'
        '  <nav epub:type="toc" id="toc"><ol>\n'
        '    <li><a href="../text/ch1.xhtml#h1">Chapter One</a></li>\n'
        '    <li><a href="../text/ch2.xhtml#h2">Chapter Two</a></li>\n'
        '  </ol></nav>\n'
        '</body></html>\n'
    )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        '  <head><meta name="dtb:uid" content="urn:uuid:reader-test-0001"/></head>\n'
        '  <docTitle><text>Test Book</text></docTitle>\n'
        '  <navMap>\n'
        '    <navPoint id="np1" playOrder="1"><navLabel><text>Chapter One</text></navLabel>'
        '<content src="text/ch1.xhtml#h1"/></navPoint>\n'
        '    <navPoint id="np2" playOrder="2"><navLabel><text>Chapter Two</text></navLabel>'
        '<content src="text/ch2.xhtml#h2"/></navPoint>\n'
        '  </navMap>\n'
        '</ncx>\n'
    )
    chap = ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{t}</title></head>'
            '<body><h1 id="{a}">{t}</h1><p>{t} body text for the reader smoke test.</p></body></html>\n')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav/nav.xhtml", nav)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/text/ch1.xhtml", chap.format(t="Chapter One", a="h1"))
        z.writestr("OEBPS/text/ch2.xhtml", chap.format(t="Chapter Two", a="h2"))
    return path


# ── live server (real HTTP, served to the browser) ───────────────────────────
class _Live:
    def __init__(self, base, db_path, pdf_eid, pdf_hid, epub_eid, epub_hid):
        self.base = base
        self.db_path = db_path
        self.pdf_eid, self.pdf_hid = pdf_eid, pdf_hid
        self.epub_eid, self.epub_hid = epub_eid, epub_hid

    def locator_for(self, hid):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT locator FROM reading_position WHERE holding_id = ?", (hid,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def active_bookmarks(self, hid):
        """How many live (non-tombstoned) bookmarks the server holds for a copy."""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM bookmark WHERE holding_id = ? AND deleted_at IS NULL",
                (hid,)).fetchone()[0]
        finally:
            conn.close()

    def bookmark_labels(self, hid):
        """Labels of the live bookmarks for a copy (to check auto-naming + rename)."""
        conn = sqlite3.connect(self.db_path)
        try:
            return [r[0] for r in conn.execute(
                "SELECT label FROM bookmark WHERE holding_id = ? AND deleted_at IS NULL "
                "ORDER BY rev", (hid,)).fetchall()]
        finally:
            conn.close()

    def active_annotations(self, hid):
        """How many live (non-tombstoned) annotations the server holds for a copy."""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM annotation WHERE holding_id = ? AND deleted_at IS NULL",
                (hid,)).fetchone()[0]
        finally:
            conn.close()


@pytest.fixture
def live(tmp_path, monkeypatch):
    from werkzeug.serving import make_server

    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    db_path = tmp_path / "browser.db"
    app = create_app(db_path)

    pdf = _make_pdf(tmp_path / "book.pdf")
    epub = _make_epub(tmp_path / "book.epub")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pdf_eid = conn.execute("INSERT INTO edition (title) VALUES ('PDF Book')").lastrowid
    pdf_hid = conn.execute(
        "INSERT INTO holding (edition_id, form, file_path, text_status) "
        "VALUES (?, 'electronic', ?, 'ocr_good')", (pdf_eid, str(pdf))).lastrowid
    epub_eid = conn.execute("INSERT INTO edition (title) VALUES ('EPUB Book')").lastrowid
    epub_hid = conn.execute(
        "INSERT INTO holding (edition_id, form, file_path, text_status) "
        "VALUES (?, 'electronic', ?, 'ocr_good')", (epub_eid, str(epub))).lastrowid
    conn.commit(); conn.close()

    srv = make_server("127.0.0.1", 0, app, threaded=True)
    port = srv.server_port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield _Live(f"http://127.0.0.1:{port}", str(db_path),
                    pdf_eid, pdf_hid, epub_eid, epub_hid)
    finally:
        srv.shutdown(); t.join()


# ── the smoke tests ──────────────────────────────────────────────────────────
def test_pdf_renders_a_canvas(live, page):
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    # pdf.js paints each page into a <canvas> inside #pdfStack — its appearance proves the
    # engine loaded the worker, parsed the PDF, and rendered.
    page.wait_for_selector("#pdfStack canvas")
    assert page.locator("#pdfStack canvas").count() >= 1
    assert page.locator("body.pdf").count() == 1          # routed to the PDF engine
    assert page.locator("#viewer .msg").count() == 0      # no error fallback rendered
    # ...and it ACTUALLY PAINTED (not a blank canvas): the first page has text, so some pixels
    # must differ from the white corner. Guards the "blank page, selectable text" failure mode
    # (canvas render broken / starved) that a mere element-exists check would miss.
    page.wait_for_function(
        """() => {
            const c = document.querySelector('#pdfStack canvas');
            if (!c || !c.width || !c.height) return false;
            const ctx = c.getContext('2d');
            const d = ctx.getImageData(0, 0, c.width, c.height).data;
            for (let i = 0; i < d.length; i += 4)
                if (d[i] < 245 || d[i+1] < 245 || d[i+2] < 245) return true;  // a non-white pixel
            return false;
        }""")


def test_epub_renders_chapter_text(live, page):
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.epub_eid}/read")
    page.wait_for_selector("body.epub")                   # routed to the EPUB engine
    page.wait_for_selector("#viewer iframe")              # epub.js renders into an iframe
    # The chapter heading actually painted inside the epub.js iframe. Target the heading
    # specifically — "Chapter One" also appears in the body <p>, so a bare text match would
    # hit two elements (strict-mode violation).
    frame = page.frame_locator("#viewer iframe")
    frame.get_by_role("heading", name="Chapter One").wait_for()
    assert page.locator("#viewer .msg").count() == 0


def test_pdf_scroll_persists_a_later_page(live, page):
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")

    # Scroll the continuous page-stack to the bottom; the page tracker should mark a later
    # page current and the debounced save (1s) should POST it to reading_position.
    page.eval_on_selector("#viewer", "el => el.scrollTo(0, el.scrollHeight)")

    deadline = time.time() + 10
    locator = None
    while time.time() < deadline:
        locator = live.locator_for(live.pdf_hid)
        if locator and locator.isdigit() and int(locator) > 1:
            break
        time.sleep(0.4)
    assert locator and locator.isdigit() and int(locator) > 1, (
        f"expected a later page persisted, got {locator!r}")


def _is_sync_post(r):
    return "/sync/reader" in r.url and r.request.method == "POST"


def _arm_annotate(page):
    """Flip the reader out of pure reading mode into annotate mode (the tools are hidden until
    then) and wait for the overlay to be live."""
    page.click("#annotateBtn")
    page.wait_for_selector("body.annotate-ready")


def test_bookmark_add_list_jump_delete(live, page):
    """The full bookmark loop in a real browser: add → persisted to /sync/reader → shown in
    the panel → survives a reload (so it came from the server, not page state) → delete
    tombstones it server-side and empties the panel."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")

    # Add a bookmark at the current spot; wait for the sync POST to land.
    with page.expect_response(_is_sync_post):
        page.click("#bmAdd")
    assert live.active_bookmarks(live.pdf_hid) == 1            # persisted server-side

    # It appears in the bookmarks panel.
    page.click("#bmList")
    page.wait_for_selector("#bmPanel .bm-row")
    assert page.locator("#bmPanel .bm-row").count() == 1

    # Survives a reload — proof it's synced (the page reloads fresh and re-pulls it).
    page.reload()
    page.wait_for_selector("#pdfStack canvas")
    page.click("#bmList")
    page.wait_for_selector("#bmPanel .bm-row")

    # Delete → tombstoned server-side, panel empties.
    with page.expect_response(_is_sync_post):
        page.click("#bmPanel .bm-del")
    page.wait_for_selector("#bmPanel .bm-empty")
    assert live.active_bookmarks(live.pdf_hid) == 0


def _scroll_pdf_to_a_later_page(live, page):
    """Scroll the continuous stack down and wait for the tracked page to advance past 1."""
    page.eval_on_selector("#viewer", "el => el.scrollTo(0, el.scrollHeight)")
    deadline = time.time() + 10
    while time.time() < deadline:
        loc = live.locator_for(live.pdf_hid)
        if loc and loc.isdigit() and int(loc) > 1:
            return int(loc)
        time.sleep(0.4)
    raise AssertionError("page never advanced past 1")


def test_pdf_bookmark_label_is_the_real_page_not_page_1(live, page):
    """Regression: the page tracker used a >0.5-visible threshold that never fired for tall
    pages, so every bookmark was labelled 'Page 1'. It must reflect the actual page."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")
    _scroll_pdf_to_a_later_page(live, page)

    with page.expect_response(_is_sync_post):
        page.click("#bmAdd")
    labels = live.bookmark_labels(live.pdf_hid)
    assert len(labels) == 1
    assert labels[0].startswith("Page ") and labels[0] != "Page 1", labels[0]


def test_pdf_bookmark_is_deduped_at_the_same_spot(live, page):
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")

    with page.expect_response(_is_sync_post):
        page.click("#bmAdd")
    assert live.active_bookmarks(live.pdf_hid) == 1
    # Click again at the SAME spot — no second bookmark is created.
    page.click("#bmAdd")
    page.wait_for_timeout(700)
    assert live.active_bookmarks(live.pdf_hid) == 1


def test_bookmark_rename(live, page):
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")
    with page.expect_response(_is_sync_post):
        page.click("#bmAdd")

    page.click("#bmList")
    page.wait_for_selector("#bmPanel .bm-row")
    page.click("#bmPanel .bm-edit")
    page.wait_for_selector("#bmPanel .bm-edit-input")
    page.fill("#bmPanel .bm-edit-input", "Key passage")
    with page.expect_response(_is_sync_post):
        page.press("#bmPanel .bm-edit-input", "Enter")
    page.wait_for_function("document.querySelector('#bmPanel .bm-go') "
                           "&& document.querySelector('#bmPanel .bm-go').textContent === 'Key passage'")
    assert live.bookmark_labels(live.pdf_hid) == ["Key passage"]


def test_epub_bookmark_label_reflects_location_not_zero_percent(live, page):
    """Regression: EPUB bookmarks were always '0%' because epub.js percentage is 0 until
    locations are generated. After navigating into the book, the label must reflect the
    current page's location (a real % or the chapter), never a bare '0%'."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.epub_eid}/read")
    page.wait_for_selector("#viewer iframe")
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter One").wait_for()

    # Move into chapter two, then bookmark there.
    page.click("#pgNext")
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter Two").wait_for()
    with page.expect_response(_is_sync_post):
        page.click("#bmAdd")

    labels = live.bookmark_labels(live.epub_hid)
    assert len(labels) == 1
    assert labels[0] not in ("0%", "Bookmark", "Page 1"), labels[0]


def test_pdf_toc_lists_outline(live, page):
    """The PDF's embedded outline (getOutline) shows in the contents panel."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")
    page.click("#tocBtn")
    page.wait_for_selector("#tocPanel .toc-item")
    items = page.locator("#tocPanel .toc-item")
    assert items.count() == 2
    assert [items.nth(i).inner_text() for i in range(items.count())] == ["Chapter A", "Chapter B"]

    # Clicking an entry must actually navigate to its page (Chapter B → page 4).
    page.locator("#tocPanel .toc-item", has_text="Chapter B").click()
    deadline = time.time() + 8
    landed = None
    while time.time() < deadline:
        landed = live.locator_for(live.pdf_hid)
        if landed and landed.isdigit() and int(landed) >= 3:
            break
        time.sleep(0.3)
    assert landed and landed.isdigit() and int(landed) >= 3, f"PDF TOC did not navigate (page={landed})"


def test_epub_toc_navigates(live, page):
    """The EPUB nav lists chapters; clicking one navigates the reader to it."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.epub_eid}/read")
    page.wait_for_selector("#viewer iframe")
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter One").wait_for()

    page.click("#tocBtn")
    page.wait_for_selector("#tocPanel .toc-item")
    page.locator("#tocPanel .toc-item", has_text="Chapter Two").click()
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter Two").wait_for()


def test_pdf_theme_cycles_and_persists(live, page):
    """The ◐ button cycles light → sepia → dark; the PDF stack gets a tint/invert filter and
    the choice survives a reload (localStorage)."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")
    body_theme = lambda: page.evaluate("document.body.getAttribute('data-theme')")
    stack_filter = lambda: page.evaluate("document.getElementById('pdfStack').style.filter")

    assert body_theme() == "light" and stack_filter() == ""
    page.click("#themeBtn")
    assert body_theme() == "sepia" and "sepia" in stack_filter()
    page.click("#themeBtn")
    assert body_theme() == "dark" and "invert" in stack_filter()

    page.reload()
    page.wait_for_selector("#pdfStack canvas")
    assert body_theme() == "dark" and "invert" in stack_filter()


def test_pdf_in_book_search(live, page):
    """Search the PDF text layer; a hit lists its page and jumps there."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")
    page.click("#searchBtn")
    page.fill("#searchPanel .search-input", "Page 4")
    page.press("#searchPanel .search-input", "Enter")
    page.wait_for_selector("#searchPanel .search-results .toc-item")
    items = page.locator("#searchPanel .search-results .toc-item")
    assert items.count() >= 1
    assert "p.4" in items.first.inner_text()


def test_epub_in_book_search(live, page):
    """Search across the EPUB spine; the phrase in both chapters yields hits."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.epub_eid}/read")
    page.wait_for_selector("#viewer iframe")
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter One").wait_for()
    page.click("#searchBtn")
    page.fill("#searchPanel .search-input", "smoke test")
    page.press("#searchPanel .search-input", "Enter")
    page.wait_for_selector("#searchPanel .search-results .toc-item")
    assert page.locator("#searchPanel .search-results .toc-item").count() >= 1


def test_epub_highlight_create_and_persist(live, page):
    """Select EPUB text → colour popup → a highlight is created, persisted to /sync/reader,
    and still there after a reload (synced, not page state)."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.epub_eid}/read")
    page.wait_for_selector("#viewer iframe")
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter One").wait_for()

    # Arm annotate mode, activate the Highlight tool, then select text → the colour picker.
    _arm_annotate(page)
    page.click("#hlBtn")
    page.frame_locator("#viewer iframe").locator("p").first.select_text()
    page.wait_for_selector("#hlPopup .hl-swatch")
    with page.expect_response(_is_sync_post):
        page.locator("#hlPopup .hl-swatch").first.click()
    assert live.active_annotations(live.epub_hid) == 1

    page.reload()
    page.wait_for_selector("#viewer iframe")
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter One").wait_for()
    assert live.active_annotations(live.epub_hid) == 1   # came back from the server


def test_pdf_highlight_create_and_persist(live, page):
    """Drag-select PDF text (over the text layer) → colour popup → a {page,rect} highlight is
    created, drawn as an overlay box, persisted to /sync/reader, and back after a reload."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")
    span = page.locator("#pdfStack .pdf-page .textLayer span").first
    span.wait_for()
    box = span.bounding_box()

    # Arm annotate mode, activate Highlight, then drag-select the span → selection + mouseup.
    _arm_annotate(page)
    page.click("#hlBtn")
    page.mouse.move(box["x"] + 1, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] - 1, box["y"] + box["height"] / 2, steps=6)
    page.mouse.up()

    page.wait_for_selector("#hlPopup .hl-swatch")
    with page.expect_response(_is_sync_post):
        page.locator("#hlPopup .hl-swatch").first.click()
    assert live.active_annotations(live.pdf_hid) == 1
    page.wait_for_selector("#pdfStack .pdf-hl")              # overlay box drawn

    page.reload()                                           # reopens in reading mode
    page.wait_for_selector("#pdfStack canvas")
    _arm_annotate(page)                                     # re-enter annotate → marks re-render
    page.wait_for_selector("#pdfStack .pdf-hl")             # re-rendered from the server
    assert live.active_annotations(live.pdf_hid) == 1


def test_pdf_underline_by_drag_creates_text_underline(live, page):
    """The Underline tool: dragging a band under a text span hit-tests the covered text and
    stores a {page,rect} underline (NOT a freehand stroke) — the 'underline by pencil annotates
    as text underlining' requirement. Drawn as a .pdf-ul line, persisted, back after a reload."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")
    span = page.locator("#pdfStack .pdf-page .textLayer span").first
    span.wait_for()
    box = span.bounding_box()

    _arm_annotate(page)
    page.click("#ulBtn")
    # Drag a band along the span's vertical centre → covers the span's text.
    page.mouse.move(box["x"] + 1, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] - 1, box["y"] + box["height"] / 2, steps=6)
    with page.expect_response(_is_sync_post):
        page.mouse.up()
    page.wait_for_selector("#pdfStack .pdf-ul")             # underline line drawn
    assert live.active_annotations(live.pdf_hid) == 1

    page.reload()
    page.wait_for_selector("#pdfStack canvas")
    _arm_annotate(page)
    page.wait_for_selector("#pdfStack .pdf-ul")            # re-rendered from the server
    assert live.active_annotations(live.pdf_hid) == 1


def test_pdf_ink_freehand_create_and_persist(live, page):
    """The Draw tool: a pointer stroke over a page is captured as freehand ink, rendered into a
    per-page SVG, persisted to /sync/reader as {page, ink}, and back after a reload."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")
    pageBox = page.locator("#pdfStack .pdf-page").first.bounding_box()

    _arm_annotate(page)
    page.click("#inkBtn")
    cx, cy = pageBox["x"] + pageBox["width"] / 2, pageBox["y"] + pageBox["height"] / 2
    page.mouse.move(cx - 40, cy)
    page.mouse.down()
    for dx in range(-40, 41, 8):                            # a short squiggle
        page.mouse.move(cx + dx, cy + (10 if (dx // 8) % 2 else -10), steps=1)
    with page.expect_response(_is_sync_post):
        page.mouse.up()
    page.wait_for_selector("#pdfStack svg.pdf-ink path")    # ink rendered
    assert live.active_annotations(live.pdf_hid) == 1

    # N0 e2e: captured ink carries the per-point timestamp (4th slot, ms from stroke start) that
    # online HWR (MyScript) needs — additive `[x, y, pressure, t]`, render unaffected.
    import json
    ink_json = sqlite3.connect(live.db_path).execute(
        "SELECT ink FROM annotation WHERE holding_id = ? AND kind = 'ink'",
        (live.pdf_hid,)).fetchone()[0]
    pts = json.loads(ink_json)["strokes"][0]["points"]
    assert pts and all(len(p) == 4 for p in pts), "every point is [x, y, pressure, t]"
    assert all(isinstance(p[3], (int, float)) and p[3] >= 0 for p in pts), "t is non-negative ms"

    page.reload()
    page.wait_for_selector("#pdfStack canvas")
    _arm_annotate(page)
    page.wait_for_selector("#pdfStack svg.pdf-ink path")    # re-rendered from the server
    assert live.active_annotations(live.pdf_hid) == 1


def test_pdf_annotation_list_lists_and_jumps(live, page):
    """The annotation-list panel (▦) lists created marks; a row removes via the adapter."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")
    # make one ink mark
    pageBox = page.locator("#pdfStack .pdf-page").first.bounding_box()
    cx, cy = pageBox["x"] + pageBox["width"] / 2, pageBox["y"] + pageBox["height"] / 2
    _arm_annotate(page)
    page.click("#inkBtn")
    page.mouse.move(cx - 20, cy); page.mouse.down()
    page.mouse.move(cx + 20, cy, steps=4)
    with page.expect_response(_is_sync_post):
        page.mouse.up()
    page.click("#inkBtn")                                   # toggle back to select
    page.click("#annBtn")
    page.wait_for_selector("#annPanel .bm-row")
    assert page.locator("#annPanel .bm-row").count() == 1
    with page.expect_response(_is_sync_post):
        page.click("#annPanel .bm-del")
    assert live.active_annotations(live.pdf_hid) == 0


def test_pdf_export_annotated_download(live, page):
    """After making an ink mark, the Export button offers a 'download annotated copy' that streams
    a real PDF (the third-party-tool path) — proves the toolbar wiring + the export route."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.pdf_eid}/read")
    page.wait_for_selector("#pdfStack canvas")
    pageBox = page.locator("#pdfStack .pdf-page").first.bounding_box()
    cx, cy = pageBox["x"] + pageBox["width"] / 2, pageBox["y"] + pageBox["height"] / 2
    _arm_annotate(page)
    page.click("#inkBtn")
    page.mouse.move(cx - 20, cy); page.mouse.down()
    page.mouse.move(cx + 20, cy, steps=4)
    with page.expect_response(_is_sync_post):
        page.mouse.up()

    page.click("#exportBtn")
    page.wait_for_selector("#exportMenu #exportCopy")
    with page.expect_download() as dl:
        page.click("#exportCopy")
    path = dl.value.path()
    with open(path, "rb") as fh:
        assert fh.read(5) == b"%PDF-"               # a real PDF came back


def test_epub_underline_create_and_persist(live, page):
    """EPUB Underline tool: select text → epub.js native underline persisted to /sync/reader."""
    page.set_default_timeout(20000)
    page.set_viewport_size({"width": 1100, "height": 1000})
    page.goto(f"{live.base}/edition/{live.epub_eid}/read")
    page.wait_for_selector("#viewer iframe")
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter One").wait_for()

    _arm_annotate(page)
    page.click("#ulBtn")
    with page.expect_response(_is_sync_post):
        page.frame_locator("#viewer iframe").locator("p").first.select_text()
    assert live.active_annotations(live.epub_hid) == 1

    page.reload()
    page.wait_for_selector("#viewer iframe")
    page.frame_locator("#viewer iframe").get_by_role("heading", name="Chapter One").wait_for()
    assert live.active_annotations(live.epub_hid) == 1
