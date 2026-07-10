"""Read-only "viewer" tier — a second credential a guest (e.g. a friend) can use to
READ the catalogue without being able to review/edit/delete anything.

The server-side boundary has two layers:
  • WRITE — every write in this app is a POST, so a viewer is allowed only GET/HEAD/OPTIONS;
    any other method is 403, covering all current and future write routes with no bookkeeping.
  • READ SCOPE — a guest sees only the browse-and-read surface; every other GET (review /
    detect / picker / staging / reconcile / capture / settings / integrity …) is DEFAULT-DENIED
    via auth._VIEWER_GET_ALLOW. A page navigation bounces to "/"; a fetch gets 403.
The editor credential keeps full access. Capabilities are advertised on /api/v1/health so every
client (web, PWA, native iOS) inherits the same behavior; the gate enforces it regardless.
"""
from __future__ import annotations

from catalogue.webui.web import create_app


def _app(tmp_path, monkeypatch, *, viewer=True):
    monkeypatch.delenv("CATALOGUE_AUTH", raising=False)
    monkeypatch.setenv("CATALOGUE_AUTH_USER", "owner")
    monkeypatch.setenv("CATALOGUE_AUTH_PASS", "owner-pass")
    if viewer:
        monkeypatch.setenv("CATALOGUE_VIEWER_USER", "friend")
        monkeypatch.setenv("CATALOGUE_VIEWER_PASS", "friend-pass")
    else:
        monkeypatch.delenv("CATALOGUE_VIEWER_USER", raising=False)
        monkeypatch.delenv("CATALOGUE_VIEWER_PASS", raising=False)
    app = create_app(tmp_path / "viewer.db")
    app.testing = True
    app.config["ISBN_LOOKUP"] = lambda _i: None
    return app


def _login(client, user, pw):
    r = client.post("/login", data={"username": user, "password": pw, "next": "/app"})
    assert r.status_code == 302, (user, r.status_code)


def test_viewer_credential_logs_in_and_reads(tmp_path, monkeypatch):
    c = _app(tmp_path, monkeypatch).test_client()
    _login(c, "friend", "friend-pass")
    # Reads work.
    assert c.get("/app", headers={"Accept": "text/html"}).status_code == 200
    h = c.get("/api/v1/health").get_json()
    assert h["role"] == "viewer"
    assert h["can_edit"] is False and h["can_download"] is False


def test_viewer_cannot_write(tmp_path, monkeypatch):
    c = _app(tmp_path, monkeypatch).test_client()
    _login(c, "friend", "friend-pass")
    # Any mutating method is refused (a POST to a real write route, and even to a
    # nonexistent path — the read-only gate runs before routing).
    assert c.post("/works/detect/1/reviewed", json={"value": "1"}).status_code == 403
    assert c.post("/api/v1/capture", json={}).status_code == 403
    assert c.delete("/anything").status_code == 403


_HTML = {"Accept": "text/html", "Sec-Fetch-Mode": "navigate"}


def test_viewer_reads_browse_surface(tmp_path, monkeypatch):
    """The browse-and-read pages stay open to a guest."""
    c = _app(tmp_path, monkeypatch).test_client()
    _login(c, "friend", "friend-pass")
    for path in ("/", "/search", "/text", "/by-author"):
        assert c.get(path, headers=_HTML).status_code == 200, path


def test_viewer_cannot_open_editor_pages(tmp_path, monkeypatch):
    """Review/curation/ingest/admin GET pages are default-denied for a guest: a page
    navigation bounces to "/", a fetch (non-HTML) gets a 403 — neither leaks the surface."""
    c = _app(tmp_path, monkeypatch).test_client()
    _login(c, "friend", "friend-pass")
    editor_pages = ("/review", "/review/subjects", "/review-hub", "/review-queue",
                    "/staging", "/works/detect/single", "/works/incomplete", "/picker",
                    "/reconcile", "/capture", "/settings", "/integrity", "/library/add")
    for path in editor_pages:
        nav = c.get(path, headers=_HTML)
        assert nav.status_code == 302 and nav.headers["Location"] == "/", (path, nav.status_code)
        assert c.get(path).status_code == 403, path        # bare fetch → hard 403


def test_viewer_read_endpoints_not_overblocked(tmp_path, monkeypatch):
    """Default-deny must not swallow the reading surface: entity detail pages, the in-app
    reader, file stream, and cover art stay reachable. With an empty DB they 404 (the route
    RAN) — the point is they're never the gate's 403 or its bounce-to-"/" (which would mean
    the endpoint fell off _VIEWER_GET_ALLOW)."""
    c = _app(tmp_path, monkeypatch).test_client()
    _login(c, "friend", "friend-pass")
    for path in ("/edition/999", "/work/999", "/person/999", "/subject/999",
                 "/edition/999/read", "/edition/999/coverpage",
                 "/holding/999/file", "/holding/999/position",
                 "/api/v1/edition/999"):
        r = c.get(path, headers=_HTML)
        assert r.status_code != 403, (path, "gate 403'd a read endpoint")
        assert not (r.status_code == 302 and r.headers.get("Location") == "/"), \
            (path, "gate bounced a read endpoint to /")


def test_editor_opens_editor_pages(tmp_path, monkeypatch):
    """The same pages a guest is bounced from render for the editor (not redirected/403)."""
    c = _app(tmp_path, monkeypatch).test_client()
    _login(c, "owner", "owner-pass")
    assert c.get("/review", headers=_HTML).status_code == 200
    assert c.get("/settings", headers=_HTML).status_code == 200


def test_editor_credential_keeps_full_access(tmp_path, monkeypatch):
    c = _app(tmp_path, monkeypatch).test_client()
    _login(c, "owner", "owner-pass")
    h = c.get("/api/v1/health").get_json()
    assert h["role"] == "editor"
    assert h["can_edit"] is True and h["can_download"] is True
    # A POST is NOT blocked by the read-only gate for an editor (it reaches routing —
    # 404 here because there's no detection #1, but crucially not 403).
    assert c.post("/works/detect/1/reviewed", json={"value": "1"}).status_code != 403


def test_no_viewer_configured_is_unchanged(tmp_path, monkeypatch):
    """Without a viewer credential the editor login behaves exactly as before."""
    c = _app(tmp_path, monkeypatch, viewer=False).test_client()
    assert c.post("/login", data={"username": "friend", "password": "friend-pass"}).status_code == 401
    _login(c, "owner", "owner-pass")
    assert c.get("/api/v1/health").get_json()["role"] == "editor"


def test_non_ascii_passphrase_logs_in(tmp_path, monkeypatch):
    """A credential with non-ASCII characters must work — compare_digest rejects non-ASCII
    str, so the comparison happens on UTF-8 bytes. (Regression: 500 on /login.)"""
    monkeypatch.delenv("CATALOGUE_AUTH", raising=False)
    monkeypatch.setenv("CATALOGUE_AUTH_USER", "owner")
    monkeypatch.setenv("CATALOGUE_AUTH_PASS", "café-owner-€")
    monkeypatch.setenv("CATALOGUE_VIEWER_USER", "friend")
    monkeypatch.setenv("CATALOGUE_VIEWER_PASS", "naïve-passphrase-ü")
    app = create_app(tmp_path / "utf8.db")
    app.testing = True
    app.config["ISBN_LOOKUP"] = lambda _i: None
    c = app.test_client()
    # Wrong password → 401, not a 500.
    assert c.post("/login", data={"username": "friend", "password": "nope"}).status_code == 401
    # Correct non-ASCII viewer credential logs in as a viewer.
    _login(c, "friend", "naïve-passphrase-ü")
    assert c.get("/api/v1/health").get_json()["role"] == "viewer"


def test_open_access_is_editor(tmp_path, monkeypatch):
    """No auth env at all → open access, treated as the owner (editor)."""
    monkeypatch.delenv("CATALOGUE_AUTH", raising=False)
    monkeypatch.delenv("CATALOGUE_AUTH_USER", raising=False)
    monkeypatch.delenv("CATALOGUE_AUTH_PASS", raising=False)
    monkeypatch.delenv("CATALOGUE_VIEWER_USER", raising=False)
    monkeypatch.delenv("CATALOGUE_VIEWER_PASS", raising=False)
    app = create_app(tmp_path / "open.db")
    app.testing = True
    app.config["ISBN_LOOKUP"] = lambda _i: None
    h = app.test_client().get("/api/v1/health").get_json()
    assert h["role"] == "editor" and h["can_edit"] is True
