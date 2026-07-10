"""Optional HTTP Basic Auth gate (CATALOGUE_AUTH_USER/PASS) — for exposing the app through a
public tunnel. When set it gates EVERY route uniformly, which is what makes it work for the
offline PWA (the browser caches the credential and auto-attaches it to fetch()/sync calls).
With NO auth configured the app now fail-closes (default-deny) unless CATALOGUE_ALLOW_OPEN=1
is set — so a careless launch can't serve the catalogue open behind the tunnel. See
web.create_app / auth.provider_from_env.
"""
from __future__ import annotations

import base64

import pytest

from catalogue.webui.web import create_app


def _app(tmp_path):
    app = create_app(tmp_path / "auth.db")
    app.testing = True
    app.config["ISBN_LOOKUP"] = lambda _i: None
    return app


def test_open_requires_explicit_optin(tmp_path, monkeypatch):
    """Default-DENY: with no credentials configured, the app refuses to start rather than
    silently serving open — UNLESS the operator explicitly opts in with CATALOGUE_ALLOW_OPEN=1
    (localhost dev). This is the structural guarantee that the public tunnel can never front an
    unauthenticated backend, no matter how the server was launched."""
    monkeypatch.delenv("CATALOGUE_AUTH_USER", raising=False)
    monkeypatch.delenv("CATALOGUE_AUTH_PASS", raising=False)

    # No opt-in → hard refusal (SystemExit out of provider_from_env, raised in create_app).
    monkeypatch.delenv("CATALOGUE_ALLOW_OPEN", raising=False)
    with pytest.raises(SystemExit):
        _app(tmp_path)

    # Explicit localhost opt-in → open again (dev convenience, never the public path).
    monkeypatch.setenv("CATALOGUE_ALLOW_OPEN", "1")
    assert _app(tmp_path).test_client().get("/api/v1/health").status_code == 200


def test_gate_blocks_and_allows(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGUE_AUTH", "basic")          # cookie is the default now; opt into Basic
    monkeypatch.setenv("CATALOGUE_AUTH_USER", "me")
    monkeypatch.setenv("CATALOGUE_AUTH_PASS", "s3cret")
    c = _app(tmp_path).test_client()

    # No credentials → 401 with a Basic challenge (so the browser/PWA prompts).
    r = c.get("/api/v1/health")
    assert r.status_code == 401
    assert "Basic" in r.headers.get("WWW-Authenticate", "")

    tok = lambda u, p: {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
    assert c.get("/api/v1/health", headers=tok("me", "s3cret")).status_code == 200   # correct
    assert c.get("/api/v1/health", headers=tok("me", "nope")).status_code == 401       # wrong pass
    assert c.get("/app", headers=tok("me", "s3cret")).status_code == 200               # gates pages too
    assert c.get("/app").status_code == 401                                            # …and blocks them


def _viewer_app(tmp_path, monkeypatch, *, pw="s3cret", vpw="guest-pass"):
    """Basic gate with BOTH an editor and a read-only viewer credential."""
    monkeypatch.setenv("CATALOGUE_AUTH", "basic")
    monkeypatch.setenv("CATALOGUE_AUTH_USER", "me")
    monkeypatch.setenv("CATALOGUE_AUTH_PASS", pw)
    monkeypatch.setenv("CATALOGUE_VIEWER_USER", "friend")
    monkeypatch.setenv("CATALOGUE_VIEWER_PASS", vpw)
    return _app(tmp_path).test_client()


_TOK = lambda u, p: {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_basic_viewer_reads_but_cannot_write(tmp_path, monkeypatch):
    """The optional second credential is READ-ONLY under Basic too: it authenticates and
    reads, but every mutating method is rejected with 403 (the editor's is not)."""
    c = _viewer_app(tmp_path, monkeypatch)

    # Viewer: reads (200) + advertised as read-only, but POST/DELETE → 403.
    assert c.get("/app", headers=_TOK("friend", "guest-pass")).status_code == 200
    h = c.get("/api/v1/health", headers=_TOK("friend", "guest-pass")).get_json()
    assert h["role"] == "viewer" and h["can_edit"] is False and h["can_download"] is False
    assert c.post("/api/v1/capture", json={}, headers=_TOK("friend", "guest-pass")).status_code == 403
    assert c.delete("/anything", headers=_TOK("friend", "guest-pass")).status_code == 403

    # Editor: full access — a POST reaches routing (not 403).
    he = c.get("/api/v1/health", headers=_TOK("me", "s3cret")).get_json()
    assert he["role"] == "editor" and he["can_edit"] is True
    assert c.post("/works/detect/1/reviewed", json={"value": "1"},
                  headers=_TOK("me", "s3cret")).status_code != 403

    # A wrong viewer password is still rejected (401), not silently allowed.
    assert c.get("/api/v1/health", headers=_TOK("friend", "nope")).status_code == 401


def test_basic_viewer_non_ascii_passphrase(tmp_path, monkeypatch):
    """Regression: a non-ASCII credential must compare on UTF-8 bytes, not raise/500."""
    c = _viewer_app(tmp_path, monkeypatch, pw="café-€", vpw="naïve-ü")
    assert c.get("/api/v1/health", headers=_TOK("friend", "naïve-ü")).get_json()["role"] == "viewer"
    assert c.get("/api/v1/health", headers=_TOK("me", "café-€")).get_json()["role"] == "editor"
    assert c.get("/api/v1/health", headers=_TOK("friend", "wrong")).status_code == 401


def test_provider_seam_is_swappable(tmp_path, monkeypatch):
    """The auth PROTOCOL lives behind a provider seam — a custom provider gates the whole app
    without any change to routes/create_app, proving the layer is replaceable."""
    from flask import Response, request
    from catalogue.webui import auth

    class HeaderTokenAuth(auth.AuthProvider):           # e.g. a future bearer-token protocol
        name = "header-token"; gates = True
        def check(self):
            return None if request.headers.get("X-Token") == "open-sesame" \
                else Response("nope", 401)

    monkeypatch.delenv("CATALOGUE_AUTH_USER", raising=False)
    monkeypatch.delenv("CATALOGUE_AUTH_PASS", raising=False)
    app = create_app(tmp_path / "seam.db"); app.testing = True
    auth.install(app, HeaderTokenAuth())                # inject — no env, no code change elsewhere
    c = app.test_client()
    assert c.get("/api/v1/health").status_code == 401
    assert c.get("/api/v1/health", headers={"X-Token": "open-sesame"}).status_code == 200
    assert app.config["AUTH_PROVIDER"].name == "header-token"
