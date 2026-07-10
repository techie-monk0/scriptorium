"""Access logging that shows the REAL client IP (CATALOGUE_ACCESS_LOG). Behind the Cloudflare
tunnel every request arrives from 127.0.0.1, so the server can optionally emit its own access
line carrying CF-Connecting-IP / the first X-Forwarded-For hop — enough to tell an external
scraper apart from your own PWA. Off by default (no line, werkzeug's default log untouched)."""
from __future__ import annotations

from catalogue.webui.web import create_app


def _app(tmp_path):
    app = create_app(tmp_path / "al.db")
    app.testing = True
    return app


def test_logs_cf_connecting_ip_when_enabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CATALOGUE_ACCESS_LOG", "1")
    monkeypatch.setenv("CATALOGUE_ALLOW_OPEN", "1")          # run open so the request reaches 200
    monkeypatch.delenv("CATALOGUE_AUTH_USER", raising=False)
    monkeypatch.delenv("CATALOGUE_AUTH_PASS", raising=False)
    c = _app(tmp_path).test_client()
    c.get("/api/v1/health", headers={"CF-Connecting-IP": "203.0.113.9"})
    err = capsys.readouterr().err
    assert "203.0.113.9" in err                             # the true client IP, not 127.0.0.1
    assert '"GET /api/v1/health HTTP/1.1"' in err


def test_falls_back_to_first_forwarded_hop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CATALOGUE_ACCESS_LOG", "1")
    monkeypatch.setenv("CATALOGUE_ALLOW_OPEN", "1")
    monkeypatch.delenv("CATALOGUE_AUTH_USER", raising=False)
    monkeypatch.delenv("CATALOGUE_AUTH_PASS", raising=False)
    c = _app(tmp_path).test_client()
    c.get("/api/v1/health", headers={"X-Forwarded-For": "198.51.100.7, 172.16.0.1"})
    assert "198.51.100.7" in capsys.readouterr().err        # first hop = the origin client


def test_no_access_line_when_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CATALOGUE_ACCESS_LOG", raising=False)
    monkeypatch.setenv("CATALOGUE_ALLOW_OPEN", "1")
    c = _app(tmp_path).test_client()
    c.get("/api/v1/health", headers={"CF-Connecting-IP": "203.0.113.9"})
    assert "203.0.113.9" not in capsys.readouterr().err     # hook not installed → no custom line
