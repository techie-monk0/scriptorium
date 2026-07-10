"""System tests — opening a holding's book file from the review detail pane.

PDFs stream inline (browser viewer); the OS-reader launch for EPUB is darwin-only
and intentionally not invoked here (it would spawn Books/Preview). We assert the
streaming path and the 404 guards through HTTP.
"""
from __future__ import annotations


def _holding_with_file(seed, path, *, title="A Book"):
    eid = seed("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    return seed(
        "INSERT INTO holding (edition_id, form, file_path, text_status) "
        "VALUES (?, 'electronic', ?, 'ocr_good')", (eid, str(path)),
    ).lastrowid


def test_file_streams_inline(app_env, seed, tmp_path):
    c, _, _ = app_env
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-1.4 fake pdf bytes")
    hid = _holding_with_file(seed, f)

    r = c.get(f"/holding/{hid}/file")
    assert r.status_code == 200
    assert b"%PDF-1.4" in r.data


def test_missing_holding_file_is_404(app_env, seed):
    c, _, _ = app_env
    assert c.get("/holding/424242/file").status_code == 404


def test_library_page_uses_shared_master_detail(app_env, seed, tmp_path):
    # /library (Browse) reuses the same book-browser module as Review: left list,
    # right detail, click-the-title-to-open. Pin that wiring. (The standalone
    # /holdings list was removed in the dashboard redesign — see DELETIONS.md.)
    c, _, _ = app_env
    pdf = tmp_path / "a.pdf"; pdf.write_bytes(b"%PDF-1.4")
    hid = _holding_with_file(seed, pdf, title="My Book")
    page = c.get("/library").data
    assert b'class="master"' in page and b"detail-src" in page   # shared shell
    assert b"openNative" in page                                  # shared open JS
    assert f'/holding/{hid}/file'.encode() in page                # 📖 icon opens the PDF inline
    assert b"My Book" in page
    # The title is now a link to the EDITION page; the 📖 icon (iconbtn) opens the file.
    import sqlite3
    conn = sqlite3.connect(c.application.config["DB_PATH"])
    eid = conn.execute("SELECT id FROM edition WHERE title='My Book'").fetchone()[0]
    assert f'href="/edition/{eid}"'.encode() in page              # title → edition page
    assert b"edition-title" in page and b"bookopen iconbtn" in page
    # Search · clear · Help sit on one row (lib-btns); the browser's own topbar
    # Help is suppressed so there's exactly one Help control on Browse.
    assert b"lib-btns" in page
    assert page.count(b"? Help") == 1
    assert b'<div class="bb-topbar">' not in page   # topbar div suppressed (CSS class still defined)


def test_edition_record_shows_inline_in_library(app_env, seed, tmp_path):
    # The detail pane embeds a lazy placeholder that fetches the edition record
    # fragment, so the record shows in-pane rather than on another page.
    c, _, _ = app_env
    f = tmp_path / "b.pdf"; f.write_bytes(b"%PDF-1.4")
    _holding_with_file(seed, f, title="Inline Edition")
    page = c.get("/library").data
    assert b"edition-card" in page and b"data-card-url" in page
    assert b"fillCards" in page

    # The fragment renders the editable record without page chrome (no <nav>).
    import sqlite3
    conn = sqlite3.connect(c.application.config["DB_PATH"])
    eid = conn.execute("SELECT id FROM edition WHERE title='Inline Edition'").fetchone()[0]
    card = c.get(f"/edition/{eid}/card")
    assert card.status_code == 200
    assert b"Contained works" in card.data and b"<nav>" not in card.data
    # Full edition page still works and reuses the same card body.
    assert c.get(f"/edition/{eid}").status_code == 200
    assert c.get("/edition/999999/card").status_code == 404


def test_edition_links_carry_open_icon(app_env, seed, tmp_path):
    """REGRESSION: an edition-record link should always travel with a 📖↗ open-in-viewer
    icon (the edition_link macro). /editions/structure lists every edition — each row
    must link the record AND offer the open control for that edition's file. This is the
    recurring "link to the edition but no way to open the book" gap."""
    c, _, _ = app_env
    pdf = tmp_path / "s.pdf"; pdf.write_bytes(b"%PDF-1.4")
    hid = _holding_with_file(seed, pdf, title="Structured Book")
    import sqlite3
    conn = sqlite3.connect(c.application.config["DB_PATH"])
    eid = conn.execute("SELECT edition_id FROM holding WHERE id=?", (hid,)).fetchone()[0]
    page = c.get("/editions/structure").data.decode()
    assert f'href="/edition/{eid}"' in page             # the record link
    assert f"/holding/{hid}/file" in page               # paired open-in-viewer control
    assert "📖↗" in page


def test_holding_with_no_file_on_disk_is_404(app_env, seed, tmp_path):
    c, _, _ = app_env
    # path recorded but the file does not exist → 404, not a 500.
    hid = _holding_with_file(seed, tmp_path / "gone.epub")
    assert c.get(f"/holding/{hid}/file").status_code == 404
    assert c.post(f"/holding/{hid}/open").status_code == 404
