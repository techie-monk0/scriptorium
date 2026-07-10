"""Device-local sync API (`/api/v1/*`), the PWA shell, and system probes.

The client seam ([[capture-not-in-catalogue-log]], the device-local PWA): a
stable, client-agnostic `/api/v1/*` contract every offline-capable client
consumes — the server branches on NO client type, and replica rows carry an
OPAQUE `open_url` so the storage provider (kDrive/…) never leaks across the seam.
Also serves the installable PWA shell (service worker at root scope) and the
cheap `/health` + `/integrity` probes.
"""
from __future__ import annotations

import threading
from pathlib import Path

from flask import (
    abort, jsonify, make_response, render_template, request, send_file, g,
)

from catalogue.db_store import sqlite_source

_PWA_STATIC = Path(__file__).resolve().parent.parent / "static" / "pwa"

# Serialize the (large, slow) content-index build so concurrent requests on the threaded dev
# server don't each build ~600 MB, and so (gz, sig) is published atomically (avoids the
# check-then-act KeyError where a 2nd request saw the sig before the bytes were ready).
_CONTENT_INDEX_LOCK = threading.Lock()


def register(app, ctx):
    @app.get("/health")
    def health():
        return {
            "ok": True,
            "sqlite_source": sqlite_source(),
            "db_path": app.config["DB_PATH"],
        }

    @app.get("/integrity")
    def integrity_check():
        """Referential + completeness integrity report (catalogue/integrity.py).
        `ok:false` means dangling references — real corruption. 200 always (it's a
        report, not a gate); inspect `errors`."""
        from catalogue.db_store import integrity as I
        return jsonify(I.check_integrity(g.db))

    # ── Device-local sync API (versioned + client-agnostic; PWA & native) ─
    @app.get("/api/v1/health")
    def api_health():
        """Cheap reachability probe — no DB write. A client uses it to decide between
        live mode (server up) and offline mode (serve from its cached replica). Also
        advertises the signed-in identity's CAPABILITIES (role/can_edit/can_download) so
        every client — web, PWA, native iOS — inherits the same read-only-vs-editor and
        view-only-vs-download behavior without per-client wiring. (The server still
        enforces these regardless; this only lets a client hide what it can't do.)"""
        from catalogue.webui import auth as _auth
        return jsonify({"ok": True, "service": "catalogue", "api": 1,
                        "role": _auth.current_role(),
                        "can_edit": _auth.can_edit(),
                        "can_download": _auth.can_download()})

    @app.get("/api/v1/replica")
    def api_replica():
        """The thin device-local replica (`export_replica.build_replica`). Read-only.
        Clients cache it (PWA→IndexedDB, native→own store) for offline lookup+open. The
        ETag is computed over CONTENT (excluding the `exported_at` stamp) so a client with
        unchanged data gets a 304 even though each export is freshly timestamped."""
        import hashlib
        import json as _json
        from catalogue.services import export_replica as ER

        doc = ER.build_replica(g.db)
        stable = {k: v for k, v in doc.items() if k != "exported_at"}
        etag = '"' + hashlib.sha256(
            _json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:32] + '"'
        if request.headers.get("If-None-Match") == etag:
            resp = make_response("", 304)
        else:
            resp = make_response(_json.dumps(doc, ensure_ascii=False), 200)
            resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ── Read-only feature JSON (the shared frontend layer consumes these) ──
    # metadata-Search / Content-search expose the SAME shapes the web client-renders
    # AND a native client consumes. The web build fetches these live; the PWA serves
    # Search from its cached replica and only hits /api/v1/content live (the in-book
    # text isn't in the replica).
    @app.get("/api/v1/library")
    def api_library():
        """Metadata book search / browse (the "Search" feature) as browser rows:
        `{rows:[{id,title,subtitle,done,holding_id,has_file,file_ext}]}`. No query →
        the newest-first browse list (same as `/library` with no args)."""
        from catalogue.services import library as L
        book_title = request.args.get("book_title") or request.args.get("q") or ""
        work_title = request.args.get("work_title") or ""
        person = request.args.get("person") or request.args.get("author") or ""
        if book_title or work_title or person:
            rows = L.search(g.db, book_title=book_title, work_title=work_title,
                            person=person)
        else:
            rows = L.browse(g.db)
        return jsonify({"q": book_title, "rows": rows})

    @app.get("/api/v1/content")
    def api_content():
        """Full-text in-book content search (the "Content search" feature), grouped by
        edition: `{q, books:[{eid,title,authors,snippets}], available}`. Reuses the same
        `SearchService.search_grouped` the `/search` page uses, so results are identical.
        `available` is false only if the FTS index isn't present (then `books` is empty)."""
        q = (request.args.get("q") or "").strip()
        svc = app.config["SEARCH"]
        available, books = True, []
        if q:
            try:
                for b in svc.search_grouped(g.db, q):
                    books.append({"eid": b["edition_id"], "title": b["title"],
                                  "authors": b["authors"], "snippets": b["snippets"]})
            except Exception:
                available = False                # FTS table missing / not yet built
        return jsonify({"q": q, "books": books, "available": available})

    @app.get("/api/v1/edition/<int:eid>")
    def api_edition(eid):
        """One edition's full detail (the read-only Book-detail feature) — the SAME
        per-edition shape as a replica row (display_title, authors, translators, subjects,
        isbns, publisher, year, holdings, cover_url/spine_url). 404 if the edition is gone.
        A native client + a web read-only detail page consume this; the PWA reads the same
        shape from its cached replica (no extra request)."""
        from catalogue.services import export_replica as ER
        row = ER.edition_row(g.db, eid)
        if not row:
            abort(404)
        return jsonify(row)

    @app.get("/api/v1/subjects")
    def api_subjects():
        """The subject HIERARCHY as a pre-order forest (the shared source for a
        fold/unfold tree on web + PWA + native): `{kind, tree:[{id,name,leaf_label,
        depth,parent_id,has_children,is_protected,n_books_direct,n_books_total}]}`.
        `?kind=topic` (default) or `?kind=series`; `?q=` filters to matching names
        plus their ancestors."""
        from catalogue.services import subject_tree as T
        kind = request.args.get("kind") or "topic"
        q = (request.args.get("q") or "").strip() or None
        return jsonify({"kind": kind, "tree": T.subject_forest(g.db, kind=kind, q=q)})

    @app.get("/api/v1/subject/<int:sid>")
    def api_subject(sid):
        """One subject's browse page (the canonical subject target): `{subject, crumbs,
        children, books, n_books}`. `books` is DESCENDANT-INCLUSIVE (a topic rolls up
        its sub-topics; a series lists its volumes in edition.volume order). `children`
        are the immediate sub-subjects to drill into. 404 if the subject is gone."""
        from catalogue.services import subject_tree as T
        page = T.subject_page(g.db, sid)
        if not page:
            abort(404)
        return jsonify(page)

    @app.get("/api/v1/content-index")
    def api_content_index():
        """The downloadable standalone SQLite FTS index for OFFLINE content search (the PWA
        fetches it once when the user enables offline content search; a native client uses the
        same file). gzipped; the ETag is a cheap content signature so an unchanged index 304s.
        Built lazily and cached in-process keyed by that signature (the build can be large)."""
        from catalogue.services import export_content_index as ECI
        sig = ECI.signature(g.db)
        etag = f'"{sig}"'
        if request.headers.get("If-None-Match") == etag:
            resp = make_response("", 304)
            resp.headers["ETag"] = etag
            return resp
        cache = app.config.setdefault("_CONTENT_INDEX_CACHE", {})
        # Build under a lock and publish (gz, sig) ATOMICALLY. The dev server is threaded, the
        # build is large + slow, and a naive "set sig then build gz" let a second request see the
        # sig and read a not-yet-present gz (KeyError) — and could start a second ~600 MB build.
        if cache.get("sig") != sig or "gz" not in cache:
            with _CONTENT_INDEX_LOCK:
                if cache.get("sig") != sig or "gz" not in cache:   # re-check after acquiring
                    gz = ECI.build_gzip(g.db)[0]
                    app.config["_CONTENT_INDEX_CACHE"] = {"sig": sig, "gz": gz}
        gz = app.config["_CONTENT_INDEX_CACHE"]["gz"]
        resp = make_response(gz, 200)
        resp.headers["Content-Type"] = "application/octet-stream"
        resp.headers["Content-Encoding"] = "gzip"      # fetch() transparently decompresses
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.post("/api/v1/capture")
    def api_capture():
        """Append-only capture from an offline-capable client (the PWA outbox flushes
        here when the Mac is reachable). Reuses the idempotent JSON capture path, so a
        re-flushed scan dedupes on its ISBN (`capture_staging_raw_isbn_uq`). Defaults the
        source to 'pwa'."""
        payload = dict(request.get_json(silent=True) or {})
        payload.setdefault("source", "pwa")
        body, status = ctx.capture_one_json(payload)
        return jsonify(body), status

    # ── PWA: install + offline shell (device-local lookup/open/capture) ───
    # The client implementation of the `/api/v1/*` contract. A service worker (served at
    # root scope) caches the shell; the page loads the replica into IndexedDB for offline
    # lookup. start_url=/app, so "Add to Home Screen" launches this offline-capable view.
    @app.get("/app")
    def pwa_app():
        return render_template("app.html")

    @app.get("/sw.js")
    def pwa_sw():
        """Service worker MUST be served from root to control the whole-origin scope."""
        resp = send_file(str(_PWA_STATIC / "sw.js"),
                         mimetype="application/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/manifest.webmanifest")
    def pwa_manifest():
        return send_file(str(_PWA_STATIC / "manifest.webmanifest"),
                         mimetype="application/manifest+json")
