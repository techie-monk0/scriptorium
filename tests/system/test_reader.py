"""End-to-end tests for the in-app reader (reader_module_plan.md Phase 1).

The reader engine was extracted from reader.html's inline <script> into the standalone,
adapter-driven modules static/reader/vendor.js + static/reader/reader-core.js, with
reader.html reduced to a thin web shell that wires Flask-backed adapters into
ReaderCore.mount(). These tests exercise that whole contract end-to-end through the Flask
test client — the repo's "e2e" layer (there is no browser harness):

  * the standalone modules are served and expose the expected globals
  * /edition/<id>/read and /holding/<id>/read render the shell wired to ReaderCore,
    routed to the right engine (pdf vs epub) and bound to the right holding
  * the byte source the reader depends on works: PDF URL-streaming (incl. HTTP Range,
    which is what keeps a 50M PDF from loading whole) and a valid EPUB zip archive
  * the reading-position round-trip the reader saves/restores (PDF page · EPUB CFI)
  * the read-only (viewer) gate suppresses the download affordance (CAN_DL=false)

A node --check gate (skipped when node is absent) guards the extracted JS against syntax
regressions — the real risk when lifting a hand-tuned engine into a new module.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from catalogue.webui.web import create_app


# ── helpers ──────────────────────────────────────────────────────────────────
def _edition_with_file(seed, path: Path, *, title: str = "A Book") -> tuple[int, int]:
    """Seed an edition + an electronic holding whose file_path is `path`. The file's
    extension drives which engine the reader routes to (pdf vs epub)."""
    eid = seed("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    hid = seed(
        "INSERT INTO holding (edition_id, form, file_path, text_status) "
        "VALUES (?, 'electronic', ?, 'ocr_good')",
        (eid, str(path)),
    ).lastrowid
    return eid, hid


def _write_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.7\n" + b"x" * 800)
    return path


def _write_epub(path: Path) -> Path:
    """A real (minimal but valid) EPUB zip — what the reader's epubData() fetch expects."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("OEBPS/ch0.xhtml", "<html><body><p>Chapter one.</p></body></html>")
    return path


# ── the standalone modules ───────────────────────────────────────────────────
def test_reader_modules_are_served_and_export_globals(app_env):
    c, _, _ = app_env
    rc = c.get("/static/reader/reader-core.js")
    rv = c.get("/static/reader/vendor.js")
    assert rc.status_code == 200 and rv.status_code == 200
    # reader-core publishes the engine entry point used by every shell.
    assert b"window.ReaderCore" in rc.data and b"function mount" in rc.data
    # vendor publishes the shared pdf.js/epub.js loaders.
    assert b"window.ReaderVendor" in rv.data
    assert b"ensurePdfjs" in rv.data and b"ensureEpub" in rv.data


def test_reader_js_modules_parse(app_env, tmp_path):
    """node --check the served JS so a syntax regression in the extracted engine fails CI.
    Skipped where node isn't installed (e.g. minimal CI image)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    c, _, _ = app_env
    for name in ("vendor.js", "reader-core.js"):
        f = tmp_path / name
        f.write_bytes(c.get(f"/static/reader/{name}").data)
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name} failed node --check:\n{r.stderr}"


# ── the reader shell wiring (the heart of the extraction) ────────────────────
def test_edition_read_renders_shell_wired_to_core_pdf(app_env, seed, tmp_path):
    c, _, _ = app_env
    eid, hid = _edition_with_file(seed, _write_pdf(tmp_path / "book.pdf"))
    r = c.get(f"/edition/{eid}/read")
    assert r.status_code == 200
    body = r.data
    # The page no longer carries the engine inline — it loads the standalone modules…
    assert b'src="/static/reader/vendor.js"' in body
    assert b'src="/static/reader/reader-core.js"' in body
    # …and drives them through ReaderCore.mount, routed to the PDF engine, bound to this copy.
    assert b"ReaderCore.mount(" in body
    assert b'ext: "pdf"' in body
    assert f"const HID = {hid}".encode() in body
    # The Flask-backed adapters the shell supplies.
    assert b"/holding/${HID}/file" in body
    assert b"/holding/${HID}/position" in body


def test_pdf_adapter_hands_pdfjs_whole_file_bytes_not_a_range_url(app_env, seed, tmp_path):
    """REGRESSION GUARD (scanned-PDF stall): the web shell must fetch the whole PDF as one
    stream and hand pdf.js {data}, NOT a {url}. A {url} puts pdf.js in HTTP range/auto-fetch
    mode, whose round-trips stall a non-linearized (scanned) PDF over the tunnel — the bar sits
    at 'Downloading… X/X' forever. The template must use streamWholeFile → {data} (same
    single-stream transport EPUB uses) and never reintroduce the range-mode source."""
    c, _, _ = app_env
    eid, _ = _edition_with_file(seed, _write_pdf(tmp_path / "book.pdf"))
    body = c.get(f"/edition/{eid}/read").data
    assert b"streamWholeFile" in body                       # the single-stream whole-file fetch
    assert b"pdfSource: async" in body and b"data: await streamWholeFile" in body
    # The old range-mode source must be gone (this is what stalled scanned PDFs).
    assert b"rangeChunkSize" not in body
    assert b"url: FILE_URL" not in body


def test_edition_read_routes_epub_engine(app_env, seed, tmp_path):
    c, _, _ = app_env
    eid, _ = _edition_with_file(seed, _write_epub(tmp_path / "book.epub"))
    r = c.get(f"/edition/{eid}/read")
    assert r.status_code == 200
    assert b'ext: "epub"' in r.data


def test_holding_read_binds_specific_holding(app_env, seed, tmp_path):
    c, _, _ = app_env
    _, hid = _edition_with_file(seed, _write_epub(tmp_path / "book.epub"))
    r = c.get(f"/holding/{hid}/read")
    assert r.status_code == 200
    assert f"const HID = {hid}".encode() in r.data
    assert b'ext: "epub"' in r.data


def test_edition_read_without_readable_copy_redirects_to_detail(app_env, seed):
    """An edition with no copy that has a file → the reader has nothing to open and
    bounces to the detail page rather than rendering an empty shell."""
    c, _, _ = app_env
    eid = seed("INSERT INTO edition (title) VALUES ('Physical only')").lastrowid
    r = c.get(f"/edition/{eid}/read")
    assert r.status_code == 302
    assert f"/edition/{eid}" in r.headers["Location"]


def test_edition_read_404_for_missing_edition(app_env):
    c, _, _ = app_env
    assert c.get("/edition/999999/read").status_code == 404


# ── the byte source the reader depends on ────────────────────────────────────
def test_pdf_file_streams_with_range_support(app_env, seed, tmp_path):
    """The /file route must still honour HTTP Range (send_file(conditional=True)): the raw
    download link and other clients rely on it, even though the in-app reader now fetches the
    whole file in one stream rather than range-paging it."""
    c, _, _ = app_env
    _, hid = _edition_with_file(seed, _write_pdf(tmp_path / "book.pdf"))
    full = c.get(f"/holding/{hid}/file")
    assert full.status_code == 200 and full.data.startswith(b"%PDF")

    part = c.get(f"/holding/{hid}/file", headers={"Range": "bytes=0-3"})
    assert part.status_code == 206
    assert part.data == b"%PDF"
    assert part.headers["Content-Range"].startswith("bytes 0-3/")


def test_range_requests_skip_per_open_side_effects(app_env, seed, tmp_path):
    """PERF REGRESSION GUARD: a PDF viewer streams a file with MANY Range requests (one per
    chunk/page). Those continuation fetches must NOT re-run the per-open side effects
    (mark-opened DB write + cover refresh) — doing so on every chunk made paging crawl. Only the
    first, full (non-Range) request stamps `last_opened`; Range requests leave it untouched."""
    import sqlite3
    c, app, _ = app_env
    _, hid = _edition_with_file(seed, _write_pdf(tmp_path / "book.pdf"))

    def last_opened():
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            return conn.execute("SELECT last_opened FROM holding WHERE id=?", (hid,)).fetchone()[0]
        finally:
            conn.close()

    # Range request BEFORE any full open → must not stamp last_opened.
    c.get(f"/holding/{hid}/file", headers={"Range": "bytes=0-3"})
    assert last_opened() is None

    # A full open stamps it...
    c.get(f"/holding/{hid}/file")
    opened = last_opened()
    assert opened is not None

    # ...and subsequent Range (paging) requests don't re-stamp / re-do open work.
    c.get(f"/holding/{hid}/file", headers={"Range": "bytes=4-7"})
    assert last_opened() == opened


def test_range_burst_reuses_cached_resolution(app_env, seed, tmp_path, monkeypatch):
    """PERF REGRESSION GUARD: resolving a holding to a path is expensive (on macOS it spawns an
    `xattr` subprocess + builds a DB Access). A PDF's burst of Range requests must reuse a cached
    resolution, not re-resolve per chunk — that per-range cost (stacked on one keep-alive
    connection) is what made remote/Cloudflare paging crawl."""
    c, app, _ = app_env
    _, hid = _edition_with_file(seed, _write_pdf(tmp_path / "book.pdf"))
    svc = app.config["BOOK_FILES"]
    calls = []
    orig = svc.resolve
    monkeypatch.setattr(svc, "resolve", lambda db, h: (calls.append(h), orig(db, h))[1])

    c.get(f"/holding/{hid}/file")                          # first → one real resolve (caches it)
    for _ in range(6):                                     # the chunk burst...
        c.get(f"/holding/{hid}/file", headers={"Range": "bytes=0-3"})
    assert calls == [hid], f"expected 1 resolve, got {len(calls)} (range requests re-resolved)"


def test_perflog_emits_file_serving_diagnostics(app_env, seed, tmp_path, capsys):
    """With perf tracing on, the file route emits [PERF] lines that locate a slow load: the request
    + range, the resolve span, the decisive byte-range read timing, and the per-request total. (A
    fast server total here while the browser still crawls ⇒ the time is in the network path.)"""
    from catalogue.services import perf
    c, _, _ = app_env
    _, hid = _edition_with_file(seed, _write_pdf(tmp_path / "book.pdf"))
    perf.enable(True)
    try:
        r = c.get(f"/holding/{hid}/file", headers={"Range": "bytes=0-9"})
    finally:
        perf.enable(False)
    assert r.status_code == 206
    err = capsys.readouterr().err
    assert "[PERF]" in err
    assert f"/holding/{hid}/file" in err and "range=bytes=0-9" in err
    assert "byte-range read" in err                        # the decisive local-read timing
    assert "→ 206" in err                                  # per-request total line


def test_epub_file_is_served_as_valid_zip(app_env, seed, tmp_path):
    """The reader fetches EPUB bytes (epubData) and hands the ArrayBuffer to epub.js as an
    archive. Verify the route streams a structurally valid zip with the epub mimetype entry."""
    c, _, _ = app_env
    _, hid = _edition_with_file(seed, _write_epub(tmp_path / "book.epub"))
    r = c.get(f"/holding/{hid}/file")
    assert r.status_code == 200
    buf = io.BytesIO(r.data)
    assert zipfile.is_zipfile(buf)
    assert "mimetype" in zipfile.ZipFile(buf).namelist()


def test_file_404_when_bytes_missing(app_env, seed, tmp_path):
    c, _, _ = app_env
    _, hid = _edition_with_file(seed, tmp_path / "never_written.pdf")  # path has no file
    assert c.get(f"/holding/{hid}/file").status_code == 404


# ── the reading-position round-trip (resume + cross-device sync) ─────────────
def test_position_round_trip_pdf_page(app_env, seed, tmp_path):
    """PDF saves the current page number as the opaque locator + a 0..1 fraction."""
    c, _, _ = app_env
    _, hid = _edition_with_file(seed, _write_pdf(tmp_path / "book.pdf"))
    assert c.get(f"/holding/{hid}/position").get_json() == {}  # nothing yet

    assert c.post(f"/holding/{hid}/position", json={"locator": "42", "fraction": 0.5}).status_code == 204
    assert c.get(f"/holding/{hid}/position").get_json() == {"locator": "42", "fraction": 0.5}


def test_position_round_trip_epub_cfi(app_env, seed, tmp_path):
    """EPUB saves an epub.js CFI as the locator."""
    c, _, _ = app_env
    _, hid = _edition_with_file(seed, _write_epub(tmp_path / "book.epub"))
    cfi = "epubcfi(/6/4[chap01]!/4/2/2[p1]:0)"
    assert c.post(f"/holding/{hid}/position", json={"locator": cfi, "fraction": None}).status_code == 204
    got = c.get(f"/holding/{hid}/position").get_json()
    assert got["locator"] == cfi


def test_position_upsert_overwrites_previous(app_env, seed, tmp_path):
    c, _, _ = app_env
    _, hid = _edition_with_file(seed, _write_pdf(tmp_path / "book.pdf"))
    c.post(f"/holding/{hid}/position", json={"locator": "3", "fraction": 0.1})
    c.post(f"/holding/{hid}/position", json={"locator": "9", "fraction": 0.3})
    assert c.get(f"/holding/{hid}/position").get_json() == {"locator": "9", "fraction": 0.3}


def test_position_missing_locator_is_noop(app_env, seed, tmp_path):
    c, _, _ = app_env
    _, hid = _edition_with_file(seed, _write_pdf(tmp_path / "book.pdf"))
    assert c.post(f"/holding/{hid}/position", json={"fraction": 0.5}).status_code == 204
    assert c.get(f"/holding/{hid}/position").get_json() == {}  # nothing was stored


# ── read-only (viewer) gate: the shell must suppress the download affordance ──
def _login(client, user: str, pw: str) -> None:
    r = client.post("/login", data={"username": user, "password": pw, "next": "/"})
    assert r.status_code == 302


def test_viewer_reader_suppresses_download_link(tmp_path, monkeypatch):
    """A read-only viewer reads inline but cannot download — the shell must render
    CAN_DL=false so ReaderCore gets downloadUrl=null and shows no 'Download instead' link."""
    monkeypatch.setenv("CATALOGUE_AUTH_USER", "owner")
    monkeypatch.setenv("CATALOGUE_AUTH_PASS", "owner-pass")
    monkeypatch.setenv("CATALOGUE_VIEWER_USER", "friend")
    monkeypatch.setenv("CATALOGUE_VIEWER_PASS", "friend-pass")
    app = create_app(tmp_path / "viewer.db")
    app.testing = True

    pdf = _write_pdf(tmp_path / "book.pdf")
    import sqlite3
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute("PRAGMA foreign_keys = ON")
    eid = conn.execute("INSERT INTO edition (title) VALUES ('Gated')").lastrowid
    conn.execute("INSERT INTO holding (edition_id, form, file_path, text_status) "
                 "VALUES (?, 'electronic', ?, 'ocr_good')", (eid, str(pdf)))
    conn.commit(); conn.close()

    with app.test_client() as c:
        _login(c, "friend", "friend-pass")
        r = c.get(f"/edition/{eid}/read")
        assert r.status_code == 200
        assert b"const CAN_DL = false" in r.data           # read-only → no download
        assert b"downloadUrl: CAN_DL ? FILE_URL : null" in r.data

    # …and the editor (open-access default) gets CAN_DL=true.
    with app.test_client() as c:
        _login(c, "owner", "owner-pass")
        r = c.get(f"/edition/{eid}/read")
        assert b"const CAN_DL = true" in r.data
