"""Web app factory (§8, §13).

`create_app` builds the Flask app — config, the request-scoped DB connection
(a foreign-key-enforcing `Store` on `g.db`), the shared context processor and
Jinja globals — then hands the app to each functional route module's
`register(app, ctx)` to attach that area's routes. The routes themselves live
under `catalogue.webui.routes.*` (home, review, editions, works, detect,
bookfiles, picker, people, capture, api, library_dashboard, …); each registers
on THIS app, so every `url_for(...)` endpoint name is unchanged. `ctx` (an
`AppContext`) carries the few helpers that cross module boundaries.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from flask import Flask, g, request

from catalogue.db_store import DryRunConnection, Store, connect, init_db
from catalogue.webui import auth as auth_mod
from catalogue.db_store import reader_state
from catalogue.services.isbn import (
    lookup as isbn_lookup,
    work_key_for_isbn as isbn_work_key_for_isbn,
)
from catalogue.services import reconcile as reconcile_mod
from catalogue.services import bookfile as bookfile_mod
from catalogue.services.search import SearchService
from catalogue.webui.routes import _shared
from catalogue.webui.routes import api as api_routes
from catalogue.webui.routes import bookfiles as bookfiles_routes
from catalogue.webui.routes import browse_by_author as browse_by_author_routes
from catalogue.webui.routes import capture as capture_routes
from catalogue.webui.routes import detect as detect_routes
from catalogue.webui.routes import editions as editions_routes
from catalogue.webui.routes import home as home_routes
from catalogue.webui.routes import library_dashboard as library_dashboard_routes
from catalogue.webui.routes import people as people_routes
from catalogue.webui.routes import picker as picker_routes
from catalogue.webui.routes import reader_sync as reader_sync_routes
from catalogue.webui.routes import reconcile as reconcile_routes
from catalogue.webui.routes import review as review_routes
from catalogue.webui.routes import sandbox as sandbox_routes
from catalogue.webui.routes import search as search_routes
from catalogue.webui.routes import settings as settings_routes
from catalogue.webui.routes import starred as starred_routes
from catalogue.webui.routes import subjects as subjects_routes
from catalogue.webui.routes import wishlist as wishlist_routes
from catalogue.webui.routes import works as works_routes
# Re-exported for tests that import them from this module.
from catalogue.webui.routes.capture import _extract_isbns_from_csv          # noqa: F401
from catalogue.webui.routes._shared import _acc, review_backlog_counts      # noqa: F401
from catalogue.db_store import default_db_path


def _default_upload_dir() -> str:
    """Read at call time, NOT module import — tests `monkeypatch.setenv`
    after import, and a module-level constant would silently ignore them."""
    return os.environ.get(
        "CATALOGUE_UPLOAD_DIR",
        str(Path.home() / ".library_cataloging" / "staging_uploads"),
    )


DEFAULT_DB = default_db_path()


def create_app(db_path: str | os.PathLike | None = None, *,
               dry_run: bool | None = None,
               ingest_verify: bool | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
    )
    # Secret key backs flash() popups (e.g. the "tagged Uncategorized" notice and the
    # review-gate block). A stable per-host default keeps single-user dev sessions
    # signed; override with CATALOGUE_SECRET in any shared deployment.
    app.secret_key = os.environ.get("CATALOGUE_SECRET", "catalogue-local-dev-secret")
    app.config["DB_PATH"] = str(db_path or DEFAULT_DB)
    # DRY-RUN: experiment in the UI without touching the DB. Writes are swallowed
    # (commit no-op) and rolled back at request end. Enable via create_app(dry_run=
    # True) or the CATALOGUE_DRY_RUN env var.
    if dry_run is None:
        dry_run = os.environ.get("CATALOGUE_DRY_RUN", "").lower() in ("1", "true", "yes", "on")
    app.config["DRY_RUN"] = bool(dry_run)
    # INGEST-VERIFY: run authority matching when a proposal is accepted (binds HARD
    # hits, surfaces the rest in /picker). ON by default (operator's choice); tests
    # set CATALOGUE_INGEST_VERIFY=off to stay hermetic (no network at accept).
    if ingest_verify is None:
        ingest_verify = os.environ.get(
            "CATALOGUE_INGEST_VERIFY", "on").lower() in ("1", "true", "yes", "on")
    app.config["INGEST_VERIFY"] = bool(ingest_verify)
    # SANDBOX: running against a forked copy (…/catalogue.db_store.sandbox). Surfaces a
    # banner + the promote/discard (swap-into-live) actions. See catalogue/sandbox.py.
    app.config["SANDBOX"] = app.config["DB_PATH"].endswith(".sandbox")
    app.config["SEARCH"] = SearchService()
    # ISBN resolver is config-swappable (§12.1: interfaces over tools) so
    # tests inject a fake without monkey-patching the module.
    app.config["ISBN_LOOKUP"] = isbn_lookup
    # OL work-key resolver — clusters editions of one work across formats for the
    # cross-format "already in catalogue" verdict. Swappable for offline tests.
    app.config["ISBN_WORK_KEY_LOOKUP"] = isbn_work_key_for_isbn
    # When a work is added from an authority match, auto-populate its author(s) from that
    # SAME authority (each source implements `authors_for`). Live by default; the fetch is
    # gated on INGEST_VERIFY (network authority matching, OFF in tests) so the suite stays
    # hermetic. Swappable for offline tests that want to exercise the wiring with a stub.
    from catalogue.services import work_authority as _work_authority
    app.config["WORK_AUTHORS_LOOKUP"] = _work_authority.authors_for
    app.config["UPLOAD_DIR"] = _default_upload_dir()
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    # Where WebDAV-fetched real bytes are cached for online-only (cloud placeholder)
    # files, so the viewer can send_file (range requests for big PDFs) and repeat opens
    # are instant. A sibling of the DB; override with CATALOGUE_WEBDAV_CACHE.
    # Absolute, so send_file (which resolves relatives against the app root_path) finds it.
    app.config["WEBDAV_CACHE"] = os.path.abspath(os.environ.get(
        "CATALOGUE_WEBDAV_CACHE",
        os.path.join(os.path.dirname(app.config["DB_PATH"]) or ".", ".webdav-cache")))
    # The one layer that turns a holding into readable local bytes — hides whether
    # they're on disk or pulled from the cloud (WebDAV). Routes below are thin clients.
    app.config["BOOK_FILES"] = bookfile_mod.BookFileService(app.config["WEBDAV_CACHE"])
    # Cached book-cover art (fetched by ISBN — no book download). Absolute for send_file.
    app.config["COVERS_CACHE"] = os.path.abspath(os.environ.get(
        "CATALOGUE_COVERS_CACHE",
        os.path.join(os.path.dirname(app.config["DB_PATH"]) or ".", ".cover-cache")))
    # Operator-PINNED covers (chosen from a holding's page, or uploaded). PERSISTENT —
    # kept out of the throwaway `.cover-cache` so a cache wipe / `fetch_covers --refresh`
    # never discards a deliberate choice; the cover route serves these first.
    app.config["COVERS_PINNED"] = os.path.abspath(os.environ.get(
        "CATALOGUE_COVERS_PINNED",
        os.path.join(os.path.dirname(app.config["DB_PATH"]) or ".", "covers-pinned")))
    # ADD-BY-UPLOAD: run the real extract/segment pipeline on an uploaded book.
    # ON by default; tests set CATALOGUE_UPLOAD_PROCESS=off to register the file
    # without invoking the heavy extractor/LLM (stays hermetic).
    app.config["UPLOAD_PROCESS"] = os.environ.get(
        "CATALOGUE_UPLOAD_PROCESS", "on").lower() in ("1", "true", "yes", "on")

    # Cache-busting for static assets: append the file's mtime as ?v=… so the browser refetches
    # a changed JS/CSS immediately (no stale shared-frontend code) while still caching when
    # unchanged. Used by templates as {{ static_v('js/library-ui-dom.js') }}.
    @app.template_global()
    def static_v(filename: str) -> str:
        base = (app.static_url_path or "/static") + "/" + filename
        try:
            mtime = int(os.stat(os.path.join(app.static_folder, filename)).st_mtime)
        except OSError:
            return base
        return f"{base}?v={mtime}"

    # Section-visibility PROTOCOLS (catalogue/domain/protocols.py): templates gate a section with
    # {% if protocol_visible('local') %}…{% endif %}. The server can evaluate `local` (loopback
    # request = host machine); `desktop` is a client-only fact, so server-rendered sections use
    # `local`/`default`. The SAME protocol names gate the client-rendered nav in library-core.js.
    @app.template_global()
    def protocol_visible(protocol: str) -> bool:
        from catalogue.services import protocols
        from flask import request as _req
        ctx = {"local": (_req.remote_addr or "") in ("127.0.0.1", "::1", "localhost")}
        return protocols.is_visible(protocol, ctx)

    _conn = init_db(app.config["DB_PATH"])  # idempotent; runs the init gate
    # The reader-state concern owns its own tables (bookmarks + the sync rev counter), kept
    # out of the central schema.sql — stand them up once at startup. Idempotent.
    reader_state.ensure_schema(_conn)
    _conn.commit()
    _conn.close()

    # Access control behind a pluggable seam (catalogue/webui/auth.py): the auth PROTOCOL
    # (none / Basic / future bearer-token / Cloudflare-Access-JWT / mTLS) is selected from the
    # env and can be swapped without touching routes. Off by default (local dev + tests stay
    # open); set CATALOGUE_AUTH_USER/PASS (or CATALOGUE_AUTH=basic) when exposing the app
    # through the public tunnel. Installed FIRST so an unauthenticated request is rejected
    # before a DB connection is opened.
    auth_mod.install(app)
    # Expose the current request's capabilities to every template, so the UI hides
    # what a viewer (read-only guest) can't do (edit/review controls, download/save
    # affordances). These are the SOFT layer; the hard boundary is auth's server-side
    # gate. Callables (evaluated per render inside the request) — templates call
    # {% if can_edit() %} / {% if can_download() %}.
    app.jinja_env.globals["can_edit"] = auth_mod.can_edit
    app.jinja_env.globals["can_download"] = auth_mod.can_download
    app.jinja_env.globals["auth_role"] = auth_mod.current_role

    # [PERF] per-request wall-clock: brackets every request so you can compare the SERVER's time
    # (this line) against the browser's perceived load. If the server reports a few ms per
    # /holding/<id>/file range but the page still crawls, the time is in the network path (tunnel),
    # not here. No-op unless --perflog / CATALOGUE_PERFLOG is set.
    @app.before_request
    def _perf_start():
        from catalogue.services import perf
        if perf.is_enabled():
            import time as _t
            g._perf_t0 = _t.perf_counter()

    @app.after_request
    def _perf_end(resp):
        from catalogue.services import perf
        t0 = getattr(g, "_perf_t0", None)
        if perf.is_enabled() and t0 is not None:
            import time as _t
            perf.log(f"{request.method} {request.full_path.rstrip('?')} → {resp.status_code}",
                     ms=(_t.perf_counter() - t0) * 1000)
        return resp

    # Access logging with the REAL client IP. Behind the Cloudflare tunnel every request arrives
    # from 127.0.0.1 (the connector), so werkzeug's default access line can't tell callers apart —
    # a scraper the auth gate is 401ing looks identical to your own PWA. When CATALOGUE_ACCESS_LOG
    # is set (library-serve.sh turns it on in tunnel mode) we silence werkzeug's line and emit our
    # own carrying CF-Connecting-IP / the first X-Forwarded-For hop, so you can see who is actually
    # hitting the public URL. LOG-ONLY: request.remote_addr is left untouched (no ProxyFix), so
    # nothing security-sensitive — auth, any remote_addr checks — changes.
    if os.environ.get("CATALOGUE_ACCESS_LOG", "").strip().lower() in ("1", "true", "yes", "on"):
        import logging as _logging
        _logging.getLogger("werkzeug").setLevel(_logging.ERROR)   # drop the useless 127.0.0.1 line

        @app.after_request
        def _client_access_log(resp):
            import sys as _sys
            import time as _time
            ip = (request.headers.get("CF-Connecting-IP")
                  or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                  or request.remote_addr or "-")
            stamp = _time.strftime("%d/%b/%Y %H:%M:%S", _time.localtime())
            print(f'{ip} [{stamp}] "{request.method} {request.full_path.rstrip("?")} '
                  f'{request.environ.get("SERVER_PROTOCOL", "HTTP/1.1")}" {resp.status_code}',
                  file=_sys.stderr, flush=True)
            return resp

    @app.before_request
    def _open_conn():
        # Refuse to serve on a connection that isn't enforcing foreign keys —
        # writes could silently corrupt referential integrity. `connect()` already
        # sets + self-verifies the pragma (raising if a build can't enforce it);
        # the explicit re-check ALSO catches a future connection helper that simply
        # forgot to turn it on. Either way the request is disallowed with a clear
        # page instead of quietly running unsafe.
        try:
            conn = connect(app.config["DB_PATH"])
        except RuntimeError as e:
            return _fk_refused_page(str(e)), 503
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            conn.close()
            return _fk_refused_page("PRAGMA foreign_keys is OFF on this connection."), 503
        # Every request handle is a Store — the single guarded write API. It is a
        # transparent proxy, so existing g.db.execute(...) reads/writes still work;
        # mutations migrated to g.db.write(...) get the row-count post-condition.
        # check_schema=False: startup init_db already verified conformance once, so
        # we skip the per-request PRAGMA sweep.
        base = DryRunConnection(conn) if app.config["DRY_RUN"] else conn
        g.db = Store(base, check_schema=False)

    @app.context_processor
    def _inject_features():
        from catalogue.services.features import feature_enabled, rel_path
        # Lazy lookup-table options for <datalist> autocompletes (only queried when a
        # template actually calls them). Any FK/vocab text box can offer existing rows.
        def subject_names(kind=None):
            return _acc(g.db).subjects.graph.names(kind)
        def work_type_codes():
            return _acc(g.db).vocab.work_types()
        def person_names():
            return _acc(g.db).persons.reads.live_primary_names()
        # Per-tab pending counts for the shared Review tab strip (_review_tabs.html);
        # only queried when a review page actually renders the strip.
        def review_counts():
            return review_backlog_counts(g.db)
        return {"feature_enabled": feature_enabled, "rel_path": rel_path,
                "subject_names": subject_names, "person_names": person_names,
                "work_type_codes": work_type_codes, "review_counts": review_counts}

    # Open-the-file control data for an edition's best holding, so ANY edition link
    # can pair the record link with a "📖↗ open" icon (the edition_link macro in
    # _edition_link.html). Prefers a holding with a real file, PDF first (opens
    # inline in the browser); returns has_file=False when the edition has no file.
    # Registered as a Jinja GLOBAL (not a context var) so it's reachable from inside
    # {% from '_edition_link.html' import edition_link %} without `with context`.
    def edition_open(eid):
        best = None
        for hid, _form, _ht, fp, arch in _acc(g.db).holdings.reads.display_rows(eid):
            path = fp or arch
            ext = os.path.splitext(path or "")[1].lstrip(".").lower() or None
            missing = reconcile_mod.file_state(path) == "missing"
            cand = {"holding_id": hid, "has_file": bool(path), "file_ext": ext,
                    "missing": missing}
            if cand["has_file"] and not missing and ext == "pdf":
                return cand                           # best case: a present inline PDF
            # Prefer any file over none, and a present file over a broken one.
            if best is None or (cand["has_file"] and (
                    not best["has_file"] or (best["missing"] and not missing))):
                best = cand
        return best or {"holding_id": None, "has_file": False,
                        "file_ext": None, "missing": False}
    app.jinja_env.globals["edition_open"] = edition_open

    # Companion to edition_open: the volume NUMBER for an edition in a multi-volume
    # set (0 = standalone), parsed from the stored edition.volume designation
    # (legacy 'v. 1' → 1). The standard edition-title/link partials call this so the
    # volume ALWAYS travels with the title, app-wide (the operator's request).
    def edition_volume(eid):
        if not eid:
            return 0
        vol = _acc(g.db).editions.reads.volumes([eid]).get(eid)
        if vol in (None, ""):
            return 0
        m = re.search(r"\d+", str(vol))
        return int(m.group()) if m else 0
    app.jinja_env.globals["edition_volume"] = edition_volume

    # Volume-aware display title via the ONE shared rule (catalogue.services.library), the
    # same helper the device replica uses — so the web and every client format volumes
    # identically. Templates call display_title(label, edition_volume(eid)).
    from catalogue.services import library as _library_mod
    app.jinja_env.globals["display_title"] = _library_mod.display_title

    @app.teardown_request
    def _close_conn(_exc):
        db = g.pop("db", None)
        if db is not None:
            if app.config["DRY_RUN"]:
                try:
                    db.rollback()          # discard anything this request "wrote"
                except Exception:
                    pass
            db.close()


    # ── Extracted route modules ──────────────────────────────────────────
    # Each attaches its area's routes to THIS app (same endpoint names), so
    # url_for(...) in Python and templates is unaffected. `ctx` carries the
    # few helpers that cross module boundaries; producers register first.
    ctx = _shared.AppContext()
    home_routes.register(app, ctx)
    review_routes.register(app, ctx)
    editions_routes.register(app, ctx)
    works_routes.register(app, ctx)
    browse_by_author_routes.register(app, ctx)
    detect_routes.register(app, ctx)
    bookfiles_routes.register(app, ctx)
    reader_sync_routes.register(app, ctx)   # /sync/reader — offline-first bookmarks/position
    settings_routes.register(app, ctx)
    subjects_routes.register(app, ctx)
    reconcile_routes.register(app, ctx)
    sandbox_routes.register(app, ctx)
    search_routes.register(app, ctx)
    people_routes.register(app, ctx)
    library_dashboard_routes.register(app, ctx)
    picker_routes.register(app, ctx)
    capture_routes.register(app, ctx)   # sets ctx.capture_one_json …
    wishlist_routes.register(app, ctx)  # /api/v1/wishlist (also reused by capture intent=wishlist)
    starred_routes.register(app, ctx)   # /api/v1/starred (the Starred rail + highlighted covers)
    api_routes.register(app, ctx)       # … which /api/v1/capture consumes

    return app


def _fk_refused_page(detail: str) -> str:
    """The 'connection refused' page shown when a request's DB connection is not
    enforcing foreign keys (so writes could corrupt referential integrity)."""
    return (
        "<!doctype html><meta charset=utf-8><title>Connection refused</title>"
        "<div style='font-family:system-ui,sans-serif;max-width:44rem;margin:3rem auto;"
        "padding:0 1rem'>"
        "<h1>⛔ Database connection refused</h1>"
        "<p>This connection is <b>not enforcing foreign keys</b> "
        "(<code>PRAGMA foreign_keys</code> is OFF). With enforcement off, deletes "
        "don’t cascade and invalid links aren’t rejected, so writes could silently "
        "corrupt the catalogue. The server refuses to handle requests on it.</p>"
        f"<p style='color:#a00'><code>{detail}</code></p>"
        "<p><b>Fix:</b> open the database through <code>catalogue.db_store.connect()</code>, "
        "which sets <code>PRAGMA foreign_keys = ON</code> and verifies it took.</p>"
        "</div>")


if __name__ == "__main__":
    import sys
    from catalogue.services import perf
    # `--perflog` turns on [PERF] tracing (also via CATALOGUE_PERFLOG=1) so you can see WHERE a
    # slow load goes — file resolve, the kDrive xattr probe, the byte-range read, total request
    # time. The server serving each request fast while the browser still crawls points at the
    # network path (e.g. the Cloudflare tunnel round-tripping every range request), not the server.
    if "--perflog" in sys.argv:
        perf.enable()
        print("[PERF] tracing enabled", file=sys.stderr, flush=True)
    # §14.1: bind 0.0.0.0:8000 so the iOS scanner can reach the Mac over LAN.
    # threaded=True: handle requests concurrently. The default single-threaded dev
    # server serializes everything onto one thread, so a single slow request (e.g. a
    # live BDRC/Wikidata authority lookup) blocks every other request behind it —
    # which froze Safari (it reuses one keep-alive connection) while Chrome (≈6 parallel
    # connections) hid it. Concurrency keeps the UI responsive during slow lookups.
    create_app().run(host="0.0.0.0", port=8000, debug=True, threaded=True)
