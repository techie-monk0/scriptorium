"""Generic WebDAV fetch + local→remote mapping (catalogue/domain/webdav.py) and the
viewer serving real bytes for online-only placeholders.

All offline: a fake opener stands in for the network, so the path-mapping, auth, config
loading, caching and route wiring are pinned without touching a real server.
"""
from __future__ import annotations

import base64

import pytest

from catalogue.db_store import connect
from catalogue.services import webdav


def _opener(table):
    """Fake OpenerFn: serve bytes by full URL; raise like urllib for misses."""
    def op(req, timeout):
        if req.full_url in table:
            return table[req.full_url]
        import urllib.error
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    return op


# ── client ───────────────────────────────────────────────────────────────────
def test_client_quotes_path_and_sends_basic_auth():
    captured = {}
    def op(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return b"DATA"
    c = webdav.WebDAVClient("https://h.example.com/", user="u@e.com", password="pw", opener=op)
    out = c.fetch("Common documents/Book — x.pdf")
    assert out == b"DATA"
    assert captured["url"] == "https://h.example.com/Common%20documents/Book%20%E2%80%94%20x.pdf"
    assert captured["auth"] == "Basic " + base64.b64encode(b"u@e.com:pw").decode()


def test_client_fetch_raises_webdaverror_on_http_error():
    c = webdav.WebDAVClient("https://h.example.com", opener=_opener({}))
    with pytest.raises(webdav.WebDAVError):
        c.fetch("missing.pdf")


# ── mount mapping ──────────────────────────────────────────────────────────────
def test_mount_covers_and_remote_path():
    c = webdav.WebDAVClient("https://h", opener=_opener({}))
    m = webdav.Mount("/Users/x/kDrive 2", c)
    assert m.covers("/Users/x/kDrive 2/Common documents/a.pdf")
    assert not m.covers("/Users/x/other/a.pdf")
    assert m.remote_path_for("/Users/x/kDrive 2/Common documents/a.pdf") == "Common documents/a.pdf"
    assert m.remote_path_for("/elsewhere/a.pdf") is None


def test_mount_remote_root_prefix():
    c = webdav.WebDAVClient("https://h", opener=_opener({}))
    m = webdav.Mount("/local", c, remote_root="/dav/files")
    assert m.remote_path_for("/local/sub/a.pdf") == "dav/files/sub/a.pdf"


# ── config loading ──────────────────────────────────────────────────────────────
def test_load_mounts_from_settings_file(tmp_path, monkeypatch):
    for k in ("KDRIVE_WEBDAV_URL", "KDRIVE_WEBDAV_USER", "KDRIVE_WEBDAV_PASS", "KDRIVE_LOCAL_ROOT"):
        monkeypatch.delenv(k, raising=False)
    s = tmp_path / ".kdrive_settings"
    s.write_text('export KDRIVE_WEBDAV_URL="https://abc.connect.kdrive.infomaniak.com"\n'
                 'export KDRIVE_WEBDAV_USER="me@e.com"\n'
                 'export KDRIVE_WEBDAV_PASS="secret"\n'
                 'export KDRIVE_LOCAL_ROOT="/Users/x/kDrive 2"\n')
    mounts = webdav.load_mounts(settings_path=str(s))
    assert len(mounts) == 1 and mounts[0].name == "kdrive"
    assert mounts[0].local_root == "/Users/x/kDrive 2"
    assert mounts[0].client.base_url == "https://abc.connect.kdrive.infomaniak.com"


def test_load_mounts_empty_when_unconfigured(tmp_path, monkeypatch):
    for k in ("KDRIVE_WEBDAV_URL", "KDRIVE_LOCAL_ROOT"):
        monkeypatch.delenv(k, raising=False)
    from catalogue.services import apikeys
    monkeypatch.setattr(apikeys, "file_values", lambda: {})   # ignore repo's real secrets
    assert webdav.load_mounts(settings_path=str(tmp_path / "nope")) == []


def test_env_overrides_settings_file(tmp_path, monkeypatch):
    s = tmp_path / ".kdrive_settings"
    s.write_text('export KDRIVE_WEBDAV_URL="https://file.example"\nexport KDRIVE_LOCAL_ROOT="/r"\n')
    monkeypatch.setenv("KDRIVE_WEBDAV_URL", "https://env.example")
    m = webdav.load_mounts(settings_path=str(s))[0]
    assert m.client.base_url == "https://env.example"


# ── fetch_local / fetch_to_cache ─────────────────────────────────────────────────
def _mount(local_root, table):
    return webdav.Mount(local_root, webdav.WebDAVClient("https://h", opener=_opener(table)))


def test_fetch_local_uses_covering_mount():
    url = "https://h/sub/a.pdf"
    m = _mount("/lib", {url: b"%PDF real"})
    assert webdav.fetch_local("/lib/sub/a.pdf", mounts=[m]) == b"%PDF real"
    assert webdav.fetch_local("/outside/a.pdf", mounts=[m]) is None      # no mount covers
    assert webdav.fetch_local("/lib/missing.pdf", mounts=[m]) is None    # fetch 404 → None


def test_fetch_to_cache_writes_once_and_reuses(tmp_path):
    url = "https://h/sub/a.pdf"
    m = _mount("/lib", {url: b"%PDF bytes here"})
    cache = str(tmp_path / "cache")
    p1 = webdav.fetch_to_cache("/lib/sub/a.pdf", cache, mounts=[m])
    assert p1 and open(p1, "rb").read() == b"%PDF bytes here"
    # second call: served from cache even if the mount would now 404
    m2 = _mount("/lib", {})
    p2 = webdav.fetch_to_cache("/lib/sub/a.pdf", cache, mounts=[m2])
    assert p2 == p1


# ── viewer route serves WebDAV bytes for a placeholder ───────────────────────────
def test_holding_file_serves_webdav_bytes_for_placeholder(tmp_path, monkeypatch):
    from catalogue.webui.web import create_app
    from catalogue.services import webdav as wd
    app = create_app(tmp_path / "web.db"); app.testing = True
    # local file is an all-zero placeholder → is_online_only True (fallback)
    ph = tmp_path / "ph.pdf"; ph.write_bytes(b"\x00" * 8192)
    conn = connect(app.config["DB_PATH"])
    eid = conn.execute("INSERT INTO edition (title) VALUES ('X')").lastrowid
    hid = conn.execute("INSERT INTO holding (edition_id, form, file_path) "
                       "VALUES (?, 'electronic', ?)", (eid, str(ph))).lastrowid
    conn.commit(); conn.close()
    real = tmp_path / "real_from_dav.pdf"; real.write_bytes(b"%PDF-1.7 the real bytes")
    monkeypatch.setattr(wd, "fetch_to_cache", lambda path, cache, **k: str(real))
    with app.test_client() as c:
        r = c.get(f"/holding/{hid}/file")
    assert r.status_code == 200 and r.data == b"%PDF-1.7 the real bytes"
