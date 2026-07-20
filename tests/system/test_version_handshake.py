"""System tests for the app-version handshake + staleness gate (through real HTTP).

The server advertises its build + staleness; pages are stamped with it; and a server detected as
running stale CODE refuses to serve HTML pages (so a stale server can't hand a client a broken page)
while keeping the API/health/version/static routes open so clients can still detect + recover.
"""
from __future__ import annotations

import pytest

from catalogue.webui import app_version
from catalogue.webui.web import create_app


# ── the handshake surface ─────────────────────────────────────────────────────
def test_version_endpoint(app_env):
    client, _app, _tmp = app_env
    r = client.get("/version")
    assert r.status_code == 200
    body = r.get_json()
    assert isinstance(body["app_build"], str) and body["app_build"]
    assert body["server_stale"] is False


def test_health_carries_build_and_staleness(app_env):
    client, _app, _tmp = app_env
    body = client.get("/api/v1/health").get_json()
    assert body["ok"] is True and body["api"] == 1          # existing fields intact
    assert body["app_build"] == app_version.build()
    assert body["server_stale"] is False


def test_page_stamps_build_and_loads_the_helper(app_env):
    """Every base page carries window.APP_BUILD and the app-version.js helper."""
    client, _app, _tmp = app_env
    html = client.get("/").get_data(as_text=True)
    assert "window.APP_BUILD" in html
    assert app_version.build() in html                      # the actual build value is stamped
    assert "js/app-version.js" in html


def test_pwa_shell_stamps_build_and_loads_the_helper(app_env):
    client, _app, _tmp = app_env
    html = client.get("/app").get_data(as_text=True)
    assert "window.APP_BUILD" in html
    assert "js/app-version.js" in html


# ── the staleness gate ────────────────────────────────────────────────────────
@pytest.fixture
def make_stale(monkeypatch):
    """Make the running server look like it's behind its own code on disk (a restart is pending),
    by pointing the startup code fingerprint at a value the live tree can't match."""
    monkeypatch.setattr(app_version.DEFAULT, "_startup_code", "0" * 12)
    monkeypatch.setattr(app_version.DEFAULT, "_cache", None)
    assert app_version.is_stale() is True


def test_stale_server_blocks_html_pages(app_env, make_stale):
    client, _app, _tmp = app_env
    r = client.get("/")
    assert r.status_code == 503
    assert "older code" in r.get_data(as_text=True)
    assert r.headers.get("Retry-After") == "5"


def test_stale_server_keeps_handshake_and_static_open(app_env, make_stale):
    """Clients must still reach these while stale — to DETECT the condition and to recover."""
    client, _app, _tmp = app_env
    v = client.get("/version")
    assert v.status_code == 200 and v.get_json()["server_stale"] is True
    h = client.get("/api/v1/health")
    assert h.status_code == 200 and h.get_json()["server_stale"] is True
    # A static asset (the helper itself) is not blocked.
    assert client.get("/static/js/app-version.js").status_code == 200


def test_allow_stale_env_disables_the_block(tmp_path, monkeypatch):
    """CATALOGUE_ALLOW_STALE=1 (live-editing dev) opts out of the block even when stale."""
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CATALOGUE_ALLOW_STALE", "1")
    app = create_app(tmp_path / "stale.db")
    app.testing = True
    monkeypatch.setattr(app_version.DEFAULT, "_startup_code", "0" * 12)
    monkeypatch.setattr(app_version.DEFAULT, "_cache", None)
    with app.test_client() as c:
        assert c.get("/").status_code == 200                # served despite being stale
