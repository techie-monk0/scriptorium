"""Serving + reading a holding's book file, plus covers and reading position.

Every route here is a thin client over the file-source layer
([[cloud-placeholder-webdav]], `catalogue.services.bookfile.BookFileService`): it
asks for readable local bytes and turns the outcome into a response, never
deciding whether those bytes were on local disk or pulled from the cloud over
WebDAV. Covers are fetched by ISBN with NO book download ([[book-covers-by-isbn]]).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

from flask import (
    abort, after_this_request, jsonify, redirect, render_template, request, send_file, url_for, g,
)

from catalogue.db_store.reader_state import SqliteReaderStateStore
from catalogue.services import covers as covers_mod
from catalogue.services import library as library_mod
from catalogue.services import perf
from catalogue.webui import annotate_export
from catalogue.webui import auth as auth_mod
from catalogue.webui.routes import _shared
from catalogue.webui.routes._shared import _acc


def _is_local() -> bool:
    """True when the request comes from the machine running the catalogue — gates the
    write-into-original export (it mutates a library file) and toggles its UI affordance."""
    return (request.remote_addr or "") in ("127.0.0.1", "::1", "localhost")


# Per-holding resolved-path cache for the file route, so the burst of PDF Range requests doesn't
# re-run resolve() (an `xattr` subprocess + a DB Access) on every chunk. Short TTL: a reading
# session reuses it; a moved/changed file is picked up after it lapses (and send_file 404s if the
# cached path vanished). Keyed by holding id; process-global (single-user library).
_RESOLVE_TTL = 300.0
_RESOLVE_CACHE: "dict[tuple[str, int], tuple[str, str, float]]" = {}


def _perf_probe_read(path: str, rng: "str | None") -> None:
    """[PERF] diagnostic: time reading the actually-requested byte range (or first 256KB for a full
    request) directly off `path`. Reveals whether local-disk/kDrive byte reads are the bottleneck.
    Best-effort; never affects the response."""
    try:
        start, length = 0, 262144
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            if b:
                length = int(b) - start + 1
        import time as _t
        t0 = _t.perf_counter()
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(max(1, min(length, 4 << 20)))   # cap probe at 4MB
        perf.log(f"byte-range read @{start}", ms=(_t.perf_counter() - t0) * 1000, n=len(data))
    except OSError as e:
        perf.log(f"byte-range read FAILED: {e}")


def register(app, ctx):
    # ── Open a holding's book file in the quickest viewer ─────────────────
    def _book_files():
        return app.config["BOOK_FILES"]

    def _holding_file_path(hid):
        """The absolute on-disk path recorded for a holding (the file-source layer
        owns the resolution; this thin wrapper keeps the existing call sites)."""
        return _book_files().stored_path(g.db, hid)

    def _mark_opened(hid):
        """Stamp `holding.last_opened` = now so the home page's 'Recently opened'
        shelf can order by genuine viewer opens. Best-effort — a failure here must
        never block serving the file. NOT called from the preview-render route, so
        the home page's own thumbnails don't count as opens."""
        try:
            _acc(g.db).holdings.writes.mark_opened(hid)
            g.db.commit()
        except Exception:
            pass

    def _readable_local(path):
        """A path whose REAL bytes are on disk now (no download) — delegated to the
        file-source layer, which knows the hydrated-original / WebDAV-cache rules."""
        return _book_files().readable_now(path)

    def _revalidate(resp):
        """Cache, but revalidate with the server before every reuse (`no-cache`). Paired
        with send_file's ETag this is the right policy for BOOK FILES and COVERS alike: a
        repeat fetch transfers no body when unchanged (304 from the stored copy — the
        bandwidth win on Tailscale/cellular), yet a file/cover edited in place, moved, or
        deleted is reflected immediately (fresh 200 / 404), with no stale window. The
        round-trip is a few hundred bytes — negligible, and the alternative (a long
        max-age) left re-pinned/backfilled covers stale on the shelf for up to an hour.
        `private` = one user's library; never let a shared proxy store it."""
        resp.headers["Cache-Control"] = "private, no-cache"
        return resp

    def _refresh_cover(hid, readable):
        """A book just became locally readable (opened/downloaded) → fill or upgrade its
        cover from the file's first page (cheap; no download; keeps a good existing cover)."""
        if not readable:
            return
        r = _acc(g.db).holdings.reads.get(hid)
        if r:
            try:
                covers_mod.refresh_from_file(app.config["COVERS_CACHE"], f"e{r.edition_id}", readable)
            except Exception:
                pass

    @app.get("/holding/<int:hid>/file")
    def holding_file(hid):
        """Stream the book file to the browser. PDFs render inline in the
        browser's built-in viewer; other types are sent inline too (the
        detail pane routes EPUBs to /open instead, since browsers can't show
        them). A thin client over the file-source layer: it asks for readable
        bytes and turns the outcome into a response — it never decides whether
        those bytes came from local disk or were pulled from the cloud."""
        # A PDF viewer fires MANY HTTP Range requests (one per chunk). Re-running resolve() on each
        # is costly — on macOS it spawns an `xattr` SUBPROCESS (cloud-placeholder probe) and builds
        # a DB Access every call, and a single keep-alive connection serializes them. Cache the
        # resolved (path, name) per holding for a short window so the range burst reuses it; the
        # first request (and any after the TTL) does the real resolve.
        rng = request.headers.get("Range")
        is_range = rng is not None
        perf.log(f"/holding/{hid}/file  range={rng or 'full'}")
        now = time.monotonic()
        ckey = (str(app.config["DB_PATH"]), hid)     # per-DB so tests/instances don't cross-talk
        hit = _RESOLVE_CACHE.get(ckey)
        if hit and hit[2] > now:
            path, name = hit[0], hit[1]
            perf.log(f"resolve: cache hit → {path}")
        else:
            with perf.span(f"resolve holding {hid}"):
                res = _book_files().resolve(g.db, hid)
            if res.is_missing:
                abort(404)
            if res.is_not_downloaded:
                # Cloud-only placeholder whose real bytes can't be fetched right now
                # (WebDAV unconfigured / offline) — show the 'not downloaded' guidance.
                return render_template("_online_only.html", hid=hid, triggered=False), 202
            path, name = res.path, res.download_name
            _RESOLVE_CACHE[ckey] = (path, name, now + _RESOLVE_TTL)
        # DIAGNOSTIC (perflog only): the total file size + the time to read the requested range
        # straight off `path`. Size is the number that explains a slow EPUB (fetched WHOLE — the zip
        # can't range-stream) over the tunnel: the server hands off in ms, but cloudflared then has
        # to relay the whole file to the browser (invisible here). A sub-ms range read means the
        # bytes are truly local; the wall-clock is the tunnel transferring `size` bytes.
        if perf.is_enabled():
            try:
                perf.log(f"file total size {os.path.getsize(path)} B"
                         f"{'  (EPUB → whole-file transfer)' if not is_range else ''}")
            except OSError:
                pass
            _perf_probe_read(path, rng)
        if not is_range:
            # Only the owner's opens drive the home "Recently opened" shelf — a read-only
            # guest reading a book shouldn't reorder it.
            if auth_mod.can_edit():
                _mark_opened(hid)
            _refresh_cover(hid, path)            # now local → cover from first page
        return _revalidate(send_file(path, conditional=True, download_name=name))

    # ── Export an annotated PDF (third-party-tool path; reader_module_plan §7) ──
    # Bake the reader's synced marks (highlight/underline/strikeout/note/ink) into a PDF that
    # GoodReader/Acrobat/Apple Books can read. Editor-only. Two modes:
    #   GET  /holding/<id>/annotated.pdf  → a NEW copy, streamed as a download (original untouched)
    #   POST /holding/<id>/annotated      → write the marks back INTO the original (localhost-only,
    #                                       since it mutates the user's file in place)
    def _pdf_marks(hid):
        """(source-readable-path, [PDF-anchored annotations]) for a holding, or (None, _) if it
        isn't a ready local PDF. EPUB/cfi-only marks are dropped by the exporter."""
        res = _book_files().resolve(g.db, hid)
        if res.is_missing or res.is_not_downloaded:
            return None, []
        if (library_mod._file_ext(res.path) or "").lower() != "pdf":
            return None, []
        anns = SqliteReaderStateStore(g.db).annotations_for_holding(hid)
        return res.path, anns

    @app.get("/holding/<int:hid>/annotated.pdf")
    def holding_annotated_pdf(hid):
        if not auth_mod.can_edit():
            abort(403)
        src, anns = _pdf_marks(hid)
        if not src:
            abort(404)
        if not annotate_export.has_pdf_annotations(anns):
            abort(409)                              # nothing to bake in — let the UI say so
        fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="annotated-")
        os.close(fd)

        @after_this_request
        def _cleanup(resp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return resp

        annotate_export.export_annotated(src, anns, out_path=tmp, mode="copy")
        base = os.path.splitext(os.path.basename(src))[0]
        return send_file(tmp, mimetype="application/pdf", as_attachment=True,
                         download_name=f"{base}-annotated.pdf")

    @app.post("/holding/<int:hid>/annotated")
    def holding_annotated_inplace(hid):
        # Mutates the user's original file → editor AND localhost only (same guard as sensitive
        # settings: a remote phone client can't rewrite library files).
        if not auth_mod.can_edit():
            abort(403)
        if (request.remote_addr or "") not in ("127.0.0.1", "::1", "localhost"):
            abort(403)
        # In-place must target the REAL stored file, not a cloud cache copy.
        stored = _holding_file_path(hid)
        if not stored or (library_mod._file_ext(stored) or "").lower() != "pdf":
            abort(404)
        if not _readable_local(stored):
            abort(409)
        anns = SqliteReaderStateStore(g.db).annotations_for_holding(hid)
        if not annotate_export.has_pdf_annotations(anns):
            return jsonify({"written": False, "reason": "no annotations"}), 409
        annotate_export.export_annotated(stored, anns, mode="inplace")
        return jsonify({"written": True})

    @app.get("/reconcile/file")
    def reconcile_file():
        """Open a reconcile-proposed file in the viewer. The Scan page lists files
        that have NO holding yet (genuinely new), so they can't go through
        /holding/<id>/file — they're addressed by path. Guarded: only serves a
        path that some *pending* ingest item actually references, so this can't be
        turned into an arbitrary-file read."""
        want = request.args.get("path") or ""
        allowed = False
        for _rid, pj in _acc(g.db).review.reads.pending_items("ingest"):
            if (json.loads(pj).get("path") or "") == want:
                allowed = True
                break
        if not allowed:
            abort(403)
        # Same file-source layer as /holding/<id>/file — so a pending file that's a
        # cloud placeholder is hydrated over WebDAV here too. Not cached: these are
        # curation-time views of files mid-ingest, where staleness would mislead.
        res = _book_files().resolve_path(os.path.abspath(want))
        if not res.is_ready:
            abort(404)
        return send_file(res.path, conditional=True, download_name=res.download_name)

    @app.get("/holding/<int:hid>/preview.png")
    def holding_preview(hid):
        """Render the first page (≈ title page) of the holding's file as a PNG — works
        for PDF and EPUB (PyMuPDF opens both). Best-effort: 404 if the file is missing or
        can't be rendered, so the <img> just hides itself."""
        path = _holding_file_path(hid)
        if not path:
            abort(404)
        png = None
        try:
            import fitz
            doc = fitz.open(path)
            try:
                if doc.page_count >= 1:
                    page = doc.load_page(0)
                    zoom = min(2.0, 480.0 / max(1.0, page.rect.width))   # ~480px wide
                    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                    png = pix.tobytes("png")
            finally:
                doc.close()
        except Exception:
            png = None
        if not png:
            abort(404)
        return app.response_class(png, mimetype="image/png",
                                  headers={"Cache-Control": "max-age=3600"})

    @app.post("/holding/<int:hid>/open")
    def holding_open(hid):
        """Launch the file in the OS default reader (macOS `open` → Books for
        EPUB, Preview for PDF). The server runs on the user's Mac, so this is
        the quickest way to view formats the browser can't render. 204 on
        success; 501 off-macOS (the UI falls back to /file there)."""
        # Ask the file-source layer for readable local bytes (it transparently pulls a
        # cloud placeholder over WebDAV) — never hand the OS reader an empty placeholder.
        res = _book_files().resolve(g.db, hid)
        if res.is_missing:
            abort(404)
        if res.is_not_downloaded:
            return jsonify({"online_only": True, "fetched": False}), 202
        if sys.platform != "darwin":
            abort(501)
        _mark_opened(hid)
        _refresh_cover(hid, res.path)
        subprocess.Popen(["open", res.path])
        return ("", 204)

    def _readable_holding(eid):
        """The holding to open for an edition: first copy that has a file on disk.
        Returns (hid, ext) or (None, None)."""
        for hid, _form, _ht, fp, ap in _acc(g.db).holdings.reads.display_rows(eid):
            path = fp or ap
            if path:
                return hid, library_mod._file_ext(path)
        return None, None

    @app.get("/read")
    def read_resume():
        """The Read menu target. The recent-books list is DEVICE-LOCAL (browser localStorage), not on
        the server, so this thin page reads it client-side and redirects to the last book's reader
        (or shows an empty state). See static/js/web-recent.js."""
        return render_template("read_resume.html")

    @app.get("/edition/<int:eid>/read")
    def edition_read(eid):
        """Full-screen in-app reader for an edition — opens the first copy with a
        file, restoring the last reading position. PDFs render with PDF.js, EPUBs
        with epub.js (both vendored, offline). The home tiles link here."""
        e = _acc(g.db).editions.reads.get(eid)
        if not e:
            abort(404)
        hid, ext = _readable_holding(eid)
        if not hid:
            # Nothing to read (physical-only or missing file) — fall back to detail.
            return redirect(url_for("edition_detail", eid=eid))
        return render_template("reader.html", eid=eid, hid=hid, ext=(ext or ""),
                               title=e.title or "(untitled)", is_local=_is_local(),
                               starred=_acc(g.db).starred.is_starred(eid))

    @app.get("/holding/<int:hid>/read")
    def holding_read(hid):
        """In-app reader for a SPECIFIC holding — the open-control icon links here for
        EPUBs (epub.js renders inline; no external Books). Loads /holding/<hid>/file,
        which serves real bytes even for cloud online-only placeholders (via WebDAV)."""
        row = _acc(g.db).holdings.reads.read_target(hid)
        if not row:
            abort(404)
        path = row[0] or row[1]
        if not path:
            return redirect(url_for("edition_detail", eid=row[2]))
        return render_template("reader.html", eid=row[2], hid=hid,
                               ext=(library_mod._file_ext(path) or ""),
                               title=row[3] or "(untitled)", is_local=_is_local(),
                               starred=_acc(g.db).starred.is_starred(row[2]))

    def _cover_inputs(eid):
        """(isbn, holding_path, title, author) for cover lookup — ISBN from the edition or
        an edition_isbn alias; first holding's path; first author for the placeholder."""
        acc = _acc(g.db)
        ed = acc.editions.reads.get(eid)
        if not ed:
            return None
        isbn = acc.editions.reads.first_isbn(eid)   # own column, else first edition_isbn alias
        h = acc.editions.reads.first_file_path(eid)
        from catalogue.db_store import contributor_store as cs
        authors = cs.edition_author_ids(g.db, eid)
        author = _shared.person_name(authors[0]) if authors else ""
        return isbn, h, (ed.title or "Untitled"), author

    @app.get("/edition/<int:eid>/cover.jpg")
    def edition_cover(eid):
        """Book cover by ISBN (Open Library → Google Books) — NO book download — cached on
        disk; a title+author SVG tile when none is found. The render path does only the
        cheap ISBN lookup; embedded-EPUB covers (which fetch the file) are populated by the
        bulk `catalogue.cli.fetch_covers` CLI, not per page view."""
        cache = app.config["COVERS_CACHE"]
        key = f"e{eid}"
        pinned = covers_mod.cached_path(app.config["COVERS_PINNED"], key)
        if pinned:                                   # operator's chosen cover wins
            return _revalidate(send_file(pinned, conditional=True))
        cached = covers_mod.cached_path(cache, key)
        if cached:
            return _revalidate(send_file(cached, conditional=True))
        info = _cover_inputs(eid)
        if not info:
            abort(404)
        isbn, _path, title, author = info
        readable = _readable_local(_path)        # already-downloaded file (no fetch), else None
        if not covers_mod.is_missed(cache, key) and (isbn or title or readable):
            # ISBN + title/author (no download). File-reading providers (embedded EPUB,
            # first-page render) run ONLY when the file is already local — skip_file when
            # it isn't, and mounts=[] so they never trigger a WebDAV fetch on a render.
            got = covers_mod.fetch_cover(isbn, title=title, author=author,
                                         local_path=readable, mounts=[],
                                         skip_file=(readable is None))
            if got:
                return _revalidate(send_file(covers_mod.write_cache(cache, key, got[0]),
                                             conditional=True))
            covers_mod.mark_miss(cache, key)
        return app.response_class(covers_mod.placeholder_svg(title, author),
                                  mimetype="image/svg+xml",
                                  headers={"Cache-Control": "private, no-cache"})

    @app.get("/edition/<int:eid>/spine.svg")
    def edition_spine(eid):
        """Book spine (constructed SVG) tinted by the cover's dominant colour — for the
        spine shelf view. Goes through the SAME cover layer as cover.jpg (the tint comes
        from `fetch_spine` → `fetch_cover`), but caches under its OWN edition-keyed
        namespace (`spine-e<id>`), independent of the cover cache: a spine always builds
        (palette fallback) and a later cover change never invalidates it."""
        cache = app.config["COVERS_CACHE"]
        key = f"spine-e{eid}"
        cached = covers_mod.cached_path(cache, key)
        if cached:
            return _revalidate(send_file(cached, conditional=True))
        info = _cover_inputs(eid)
        if not info:
            abort(404)
        isbn, _path, title, author = info
        # Reuse a pinned cover, else an already-cached cover (no network); else go through
        # the spine provider, which fetches a cover via the same cover layer.
        cover_path = (covers_mod.cached_path(app.config["COVERS_PINNED"], f"e{eid}")
                      or covers_mod.cached_path(cache, f"e{eid}"))
        if cover_path:
            try:
                with open(cover_path, "rb") as f:
                    cover_bytes = f.read()
            except OSError:
                cover_bytes = None
            data = covers_mod.make_spine(title, cover_bytes=cover_bytes)
        else:
            readable = _readable_local(_path)    # already-downloaded file (no fetch), else None
            got = covers_mod.fetch_spine(isbn, title=title, author=author,
                                         local_path=readable, mounts=[],
                                         skip_file=(readable is None))
            data = got[0] if got else covers_mod.spine_svg(title)
        return _revalidate(send_file(covers_mod.write_cache(cache, key, data),
                                     conditional=True))

    # ── Operator cover override (pin from a holding's page, or upload) ─────────
    def _bust_art(dir_, key):
        for ext in (".jpg", ".png", ".gif", ".svg", ".miss"):
            try:
                os.remove(os.path.join(dir_, key + ext))
            except OSError:
                pass

    def _pin_cover(eid, data):
        """Persist `data` as edition <eid>'s pinned cover and invalidate the derived art
        (auto cover + spine) so they rebuild from it. Returns False for non-image bytes."""
        if not covers_mod._looks_art(data):                     # jpg/png/gif/svg only
            return False
        data = covers_mod.prepare_cover(data)                   # trim baked frame + downscale/re-encode; SVG passes through
        covers_mod.write_cache(app.config["COVERS_PINNED"], f"e{eid}", data)
        _bust_art(app.config["COVERS_CACHE"], f"e{eid}")        # drop stale auto cover + .miss
        _bust_art(app.config["COVERS_CACHE"], f"spine-e{eid}")  # re-tint spine from the new cover
        return True

    @app.post("/edition/<int:eid>/cover/from-holding/<int:hid>")
    def edition_cover_from_holding(eid, hid):
        """Pin the cover to a rendering of holding <hid>'s page (form `page`, default 1)."""
        row = _acc(g.db).holdings.reads.cover_source(hid, eid)
        if not row:
            abort(404)
        path = _readable_local(row[0] or row[1] or "")          # local bytes only (no download)
        if not path:
            return jsonify({"ok": False, "error": "file isn't downloaded — open it once, then retry"}), 409
        try:
            page = max(1, int(request.values.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        img = covers_mod.first_page_image(path, page=page)
        if not img or not _pin_cover(eid, img):
            return jsonify({"ok": False, "error": "could not render that page"}), 422
        return jsonify({"ok": True, "source": f"holding {hid} p.{page}"})

    @app.post("/edition/<int:eid>/cover/upload")
    def edition_cover_upload(eid):
        """Pin an uploaded image as the cover (multipart field `image`)."""
        if _acc(g.db).editions.reads.get(eid) is None:
            abort(404)
        f = request.files.get("image")
        data = f.read() if f else b""
        if not data:
            return jsonify({"ok": False, "error": "no image uploaded"}), 400
        if not _pin_cover(eid, data):
            return jsonify({"ok": False, "error": "not a JPG / PNG / GIF image"}), 415
        return jsonify({"ok": True, "source": "upload"})

    @app.post("/edition/<int:eid>/cover/reset")
    def edition_cover_reset(eid):
        """Drop the pinned cover → revert to auto (ISBN / file-derived) art."""
        _bust_art(app.config["COVERS_PINNED"], f"e{eid}")
        _bust_art(app.config["COVERS_CACHE"], f"e{eid}")
        _bust_art(app.config["COVERS_CACHE"], f"spine-e{eid}")
        return jsonify({"ok": True})

    @app.get("/holding/<int:hid>/position")
    def holding_position_get(hid):
        """The saved reading position for a copy, as JSON {locator, fraction}; an
        empty object if none recorded yet."""
        row = _acc(g.db).reading_position.get(hid)
        if not row:
            return jsonify({})
        return jsonify({"locator": row[0], "fraction": row[1]})

    @app.post("/holding/<int:hid>/position")
    def holding_position_set(hid):
        """Upsert the reading position for a copy (the reader posts this as you read).
        Body: JSON {locator, fraction}. Best-effort — 204 even on a no-op."""
        data = request.get_json(silent=True) or {}
        locator = data.get("locator")
        if locator is None:
            return ("", 204)
        fraction = data.get("fraction")
        try:
            _acc(g.db).reading_position.upsert(hid, str(locator), fraction)
            g.db.commit()
        except Exception:
            pass
        return ("", 204)
