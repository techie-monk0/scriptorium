"""kDrive online-only placeholder detection + the file routes' graceful handling.

When the library is online-only in kDrive, files on disk are zero-content placeholders;
the viewer must detect that and show a message instead of streaming a blank PDF.
"""
from __future__ import annotations

import os

import pytest

from catalogue.db_store import connect
from catalogue.services import cloudsync
from catalogue.webui.web import create_app


def test_is_online_only_zero_content_fallback(tmp_path):
    ph = tmp_path / "ph.pdf"; ph.write_bytes(b"\x00" * 8192)        # placeholder = zeros
    assert cloudsync.is_online_only(str(ph)) is True
    real = tmp_path / "real.pdf"; real.write_bytes(b"%PDF-1.7" + b"\x00" * 200)
    assert cloudsync.is_online_only(str(real)) is False            # real head bytes
    (tmp_path / "empty.pdf").write_bytes(b"")
    assert cloudsync.is_online_only(str(tmp_path / "empty.pdf")) is False
    assert cloudsync.is_online_only(str(tmp_path / "missing.pdf")) is False
    assert cloudsync.is_online_only(None) is False


def test_is_online_only_status_xattr(tmp_path):
    f = tmp_path / "x.pdf"; f.write_bytes(b"%PDF real content here")
    try:
        os.setxattr(str(f), cloudsync.STATUS_XATTR, b"O\n")        # marked online-only
    except (OSError, AttributeError):
        pytest.skip("xattr unsupported on this platform/fs")
    assert cloudsync.is_online_only(str(f)) is True                # xattr wins over content
    os.setxattr(str(f), cloudsync.STATUS_XATTR, b"H\n")            # hydrated
    assert cloudsync.is_online_only(str(f)) is False


def test_is_fully_local_normal_vs_sparse(tmp_path):
    # A normally-written file has blocks covering its size → fully local.
    real = tmp_path / "real.pdf"; real.write_bytes(b"%PDF-1.7" + b"x" * 200_000)
    assert cloudsync.is_fully_local(str(real)) is True
    # A sparse file (large logical size, ~no allocated blocks) mimics an on-demand placeholder.
    sparse = tmp_path / "sparse.pdf"
    with open(sparse, "wb") as f:
        f.truncate(2_000_000)                              # 2MB logical, ~0 blocks allocated
    st = os.stat(sparse)
    if getattr(st, "st_blocks", 0) * 512 >= st.st_size * 0.5:
        pytest.skip("filesystem doesn't make truncate() sparse (e.g. some CI tmpfs)")
    assert cloudsync.is_fully_local(str(sparse)) is False
    # Empty / missing / no-st_blocks → err toward True (serve directly, never penalise).
    (tmp_path / "empty.pdf").write_bytes(b"")
    assert cloudsync.is_fully_local(str(tmp_path / "empty.pdf")) is True
    assert cloudsync.is_fully_local(str(tmp_path / "missing.pdf")) is True


def test_copy_to_cache_streams_local_bytes_and_reuses(tmp_path):
    from catalogue.services import webdav
    src = tmp_path / "book.pdf"; src.write_bytes(b"%PDF-" + b"A" * 5000)
    cache = tmp_path / "cache"
    out = webdav.copy_to_cache(str(src), str(cache))
    assert out and os.path.exists(out)
    assert open(out, "rb").read() == src.read_bytes()      # full, faithful copy
    # Second call reuses the cached copy (doesn't re-read the source).
    assert webdav.copy_to_cache(str(src), str(cache)) == out


def test_resolve_copies_on_demand_file_but_not_local_one(tmp_path, monkeypatch):
    """resolve_path: an already-local file is served DIRECTLY (no redundant copy); an on-demand /
    partially-hydrated file (is_fully_local False, not a zero-placeholder) is pulled to the cache
    once and that cache path is served — the kDrive paging-stall fix."""
    from catalogue.services import bookfile, cloudsync as cs
    src = tmp_path / "book.pdf"; src.write_bytes(b"%PDF-" + b"Z" * 4000)
    svc = bookfile.BookFileService(str(tmp_path / "cache"))

    # Fully local → original path, no cache file made.
    monkeypatch.setattr(cs, "is_fully_local", lambda p: True)
    res = svc.resolve_path(str(src))
    assert res.path == str(src)

    # Not fully local → a cache copy is served instead (different path, same bytes).
    monkeypatch.setattr(cs, "is_fully_local", lambda p: False)
    res2 = svc.resolve_path(str(src))
    assert res2.path != str(src) and open(res2.path, "rb").read() == src.read_bytes()


def test_request_download_is_safe_noop_until_pinned_value_known(tmp_path):
    f = tmp_path / "x.pdf"; f.write_bytes(b"\x00" * 16)
    assert cloudsync.PINSTATE_PINNED is None                       # not yet decoded
    assert cloudsync.request_download(str(f)) is False             # no-op, no raise


def _holding(app, path):
    conn = connect(app.config["DB_PATH"])
    eid = conn.execute("INSERT INTO edition (title) VALUES ('X')").lastrowid
    hid = conn.execute("INSERT INTO holding (edition_id, form, file_path) "
                       "VALUES (?, 'electronic', ?)", (eid, str(path))).lastrowid
    conn.commit(); conn.close()
    return hid


def test_route_online_only_returns_202_message(tmp_path):
    app = create_app(tmp_path / "web.db"); app.testing = True
    ph = tmp_path / "placeholder.pdf"; ph.write_bytes(b"\x00" * 8192)
    hid = _holding(app, ph)
    with app.test_client() as c:
        r = c.get(f"/holding/{hid}/file")
    assert r.status_code == 202
    assert b"online-only in kDrive" in r.data                      # the guidance page


def test_route_real_file_is_served(tmp_path):
    app = create_app(tmp_path / "web.db"); app.testing = True
    real = tmp_path / "real.pdf"; real.write_bytes(b"%PDF-1.7\n" + b"x" * 500)
    hid = _holding(app, real)
    with app.test_client() as c:
        r = c.get(f"/holding/{hid}/file")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")


def test_holding_read_renders_inapp_epub_reader(tmp_path):
    # The EPUB open icon now links to /holding/<id>/read → the in-app epub.js reader
    # (loads /holding/<id>/file, which is WebDAV-backed for online-only files).
    app = create_app(tmp_path / "web.db"); app.testing = True
    hid = _holding(app, tmp_path / "Some Book.epub")
    with app.test_client() as c:
        r = c.get(f"/holding/{hid}/read")
    assert r.status_code == 200
    assert b'ext: "epub"' in r.data                       # routed to the epub.js path (ReaderCore.mount)
    assert (f"const HID = {hid}".encode()) in r.data       # reader bound to this holding


def test_open_route_online_only_returns_202(tmp_path):
    app = create_app(tmp_path / "web.db"); app.testing = True
    ph = tmp_path / "placeholder.epub"; ph.write_bytes(b"\x00" * 8192)
    hid = _holding(app, ph)
    with app.test_client() as c:
        r = c.post(f"/holding/{hid}/open")
    assert r.status_code == 202 and r.get_json()["online_only"] is True
