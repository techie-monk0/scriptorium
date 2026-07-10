"""Signed-cookie session auth (the PWA default when CATALOGUE_AUTH_USER/PASS are set).

Unlike Basic, this keeps the PWA logged in across launches: a same-origin signed cookie with a
server-checked max-age, set by a `/login` form that lives entirely below the auth seam (the
provider registers its own routes — `create_app`/the route modules never learn the protocol).
"""
from __future__ import annotations

import pytest

from catalogue.webui.web import create_app


def _app(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOGUE_AUTH", raising=False)        # exercise the default selection
    monkeypatch.setenv("CATALOGUE_AUTH_USER", "me")
    monkeypatch.setenv("CATALOGUE_AUTH_PASS", "s3cret")
    app = create_app(tmp_path / "cookie.db")
    app.testing = True
    app.config["ISBN_LOOKUP"] = lambda _i: None
    return app


def test_cookie_is_the_default_provider(tmp_path, monkeypatch):
    assert _app(tmp_path, monkeypatch).config["AUTH_PROVIDER"].name == "cookie"


def test_login_flow_sets_a_session(tmp_path, monkeypatch):
    c = _app(tmp_path, monkeypatch).test_client()

    # Unauthenticated PAGE load → redirect to the form (no native dialog, no re-prompt loop).
    r = c.get("/app", headers={"Accept": "text/html"})
    assert r.status_code == 302 and "/login" in r.headers["Location"]

    # Unauthenticated API/asset fetch → 401, so the PWA's fetch()/SW reacts without a redirect.
    assert c.get("/api/v1/health", headers={"Sec-Fetch-Mode": "cors"}).status_code == 401

    # The form is reachable with no session; wrong creds don't grant one.
    assert c.get("/login").status_code == 200
    assert c.post("/login", data={"username": "me", "password": "nope"}).status_code == 401
    assert c.get("/api/v1/health", headers={"Sec-Fetch-Mode": "cors"}).status_code == 401

    # Correct creds → cookie set, redirected to the app; the session now opens everything.
    r = c.post("/login", data={"username": "me", "password": "s3cret", "next": "/app"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/app")
    assert c.get("/app", headers={"Accept": "text/html"}).status_code == 200
    assert c.get("/api/v1/health").status_code == 200      # cookie auto-attaches to fetches too

    # Logout clears it → gated again.
    c.get("/logout")
    assert c.get("/app", headers={"Accept": "text/html"}).status_code == 302


def test_open_redirect_is_refused(tmp_path, monkeypatch):
    """A crafted ?next= can't bounce the browser off-site after login."""
    c = _app(tmp_path, monkeypatch).test_client()
    r = c.post("/login", data={"username": "me", "password": "s3cret", "next": "//evil.example"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/app")   # fell back to /app


def test_forged_cookie_rejected(tmp_path, monkeypatch):
    c = _app(tmp_path, monkeypatch).test_client()
    c.set_cookie("lib_auth", "not.a.valid.signed.token")
    assert c.get("/app", headers={"Accept": "text/html"}).status_code == 302


def test_password_change_invalidates_old_sessions():
    """The signing key is derived from the password, so rotating it voids outstanding cookies
    with no separate secret to manage."""
    from itsdangerous import BadSignature
    from catalogue.webui.auth import CookieTokenAuth

    old, new = CookieTokenAuth("me", "old-pass"), CookieTokenAuth("me", "new-pass")
    token = old._signer.dumps("me")
    with pytest.raises(BadSignature):
        new._signer.loads(token)
