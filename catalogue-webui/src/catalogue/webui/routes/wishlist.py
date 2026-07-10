"""Wishlist API (`/api/v1/wishlist`) — books wanted but not yet owned.

A single shared library-wide list, added one of three ways (typed ISBN, typed title/author, or a
scanned ISBN/CIP — see also the `intent:"wishlist"` branch of the capture routes). Resolution is
delegated to `services.wishlist_resolve`; persistence to `acc.wishlist`. The list GET is ETagged
(like `/api/v1/replica`) so offline clients cache it cheaply; writes require edit capability and the
access-API maps `StaleWrite`/`NotFound` to their HTTP status.

`add_from_input` is the shared entry point — both these routes and the capture `intent:"wishlist"`
branch call it, so the three input forms resolve identically wherever a wishlist add originates.
"""
from __future__ import annotations

from flask import jsonify, make_response, render_template, request, g

from catalogue.contracts import CatalogueError
from catalogue.services import wishlist_resolve as WR
from catalogue.webui import auth as _auth
from ._shared import _acc

_WISHLIST_SOURCES = {"manual", "isbn", "cip", "scan"}


def add_from_input(db, *, source: str, isbn=None, title=None, author=None, cip_text=None,
                   isbn_lookup=None, work_key_fetch=None, title_search=None) -> dict:
    """Resolve one wishlist input and persist it; returns `{item, verdict, added, owned, duplicate}`.

    Two guards before persisting:
      • OWNED — if the catalogue already holds the book, it is NOT added (`owned=True`, `item=None`);
        the verdict's editions tell the client what they already have.
      • DUPLICATE — if a live wishlist item already covers it (same ISBN / work key / title), that
        EXISTING item is returned (`duplicate=True`) instead of a second copy.

    Input precedence: ISBN → CIP text → title/author. Resolution is best-effort (a network miss
    degrades the status, never raises). Staged + committed here. Shared by the wishlist POST route
    and the capture `intent:"wishlist"` branch so every entry path behaves identically.
    """
    src = source if source in _WISHLIST_SOURCES else "manual"
    if isbn:
        res = WR.resolve_isbn(db, isbn, isbn_lookup=isbn_lookup, work_key_fetch=work_key_fetch)
        raw = {"raw_isbn": isbn}
        if src == "manual":
            src = "isbn"
    elif cip_text:
        res = WR.resolve_cip(db, cip_text, isbn_lookup=isbn_lookup, work_key_fetch=work_key_fetch)
        raw = {"raw_cip_text": cip_text}
        if src == "manual":
            src = "cip"
    else:
        res = WR.resolve_title(db, title or "", author, title_search=title_search)
        raw = {"raw_title": title, "raw_author": author}

    acc = _acc(db)
    snap = res.snapshot
    # Guard 1 — already owned: don't wishlist a book the catalogue already holds.
    if res.status == "owned":
        return {"item": None, "verdict": res.verdict, "added": False,
                "owned": True, "duplicate": False}
    # Guard 2 — already wishlisted: return the existing item rather than a duplicate.
    existing = acc.wishlist.match(isbn=snap.get("isbn"), ol_work_key=snap.get("ol_work_key"),
                                  title=snap.get("title"))
    if existing is not None:
        return {"item": existing.to_dict(), "verdict": res.verdict, "added": False,
                "owned": False, "duplicate": True}

    # Guard 3 — SUSPECTED owned: a similar title + shared author (e.g. the catalogue has the
    # hardcover under a different ISBN). Not confident enough to call owned, so add it but flag it
    # 'suspected' with the candidate editions, so the operator is asked "is this the same book?".
    status = res.status
    if status in ("resolved", "unresolved"):
        suspects = _suspected_for(db, snap)
        if suspects:
            status, snap = "suspected", {**snap, "candidates": suspects}

    item_id = acc.wishlist.add(source=src, status=status, snapshot=snap, **raw)
    db.commit()
    item = acc.wishlist.get(item_id)
    return {"item": item.to_dict() if item else None, "verdict": res.verdict,
            "added": True, "owned": False, "duplicate": False}


def _suspected_for(db, snapshot: dict):
    """Weak catalogue suspects ("might already own this") for a freshly-resolved item — used at add
    time to ask the operator rather than silently adding a likely-duplicate. Local-only."""
    from catalogue.services import intake_match
    title = snapshot.get("title")
    if not title:
        return []
    return intake_match.suspected_editions(
        db, {"title": title, "authors": list(snapshot.get("authors") or [])})


def _list_payload(db) -> dict:
    """The presenter-agnostic wishlist payload clients cache + feed to `wishlistVM`."""
    return {"items": [w.to_dict() for w in _acc(db).wishlist.list()],
            "schema": 1}


def register(app, ctx):
    @app.get("/wishlist")
    def wishlist_page():
        """The web Wishlist page — renders the shared `LibraryCore.wishlistVM` over the live
        `/api/v1/wishlist` payload (same VM the PWA + iOS consume)."""
        return render_template("wishlist.html")

    def _lookups():
        """The configured (swappable) OpenLibrary fetchers — real in production, fakes in tests."""
        return (app.config.get("ISBN_LOOKUP"), app.config.get("ISBN_WORK_KEY_LOOKUP"))

    @app.get("/api/v1/wishlist")
    def api_wishlist_list():
        """The shared wishlist, newest/priority first. ETagged over content so an unchanged list
        304s; clients cache it for offline display (same pattern as /api/v1/replica)."""
        import hashlib
        import json as _json
        # Read-only: reconciliation runs WRITE-side (sweep/ingest, edition delete, capture), never here.
        etag = '"' + hashlib.sha256(
            _acc(g.db).wishlist.fingerprint().encode("utf-8")).hexdigest()[:32] + '"'
        if request.headers.get("If-None-Match") == etag:
            resp = make_response("", 304)
        else:
            resp = make_response(_json.dumps(_list_payload(g.db), ensure_ascii=False), 200)
            resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.post("/api/v1/wishlist")
    def api_wishlist_add():
        """Add a wanted book. Body carries exactly one input form:
        {"isbn":"…"} | {"title":"…","author":"…"} | {"cip_text":"…"}. Optional "source"
        (manual|isbn|cip|scan). Returns the created item + the cross-format dedupe verdict."""
        if not _auth.can_edit():
            return jsonify({"error": "read-only"}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "format"}), 422
        isbn = (body.get("isbn") or "").strip() or None
        title = (body.get("title") or "").strip() or None
        cip_text = (body.get("cip_text") or "").strip() or None
        if not (isbn or title or cip_text):
            return jsonify({"error": "provide an isbn, a title, or cip_text"}), 422
        lookup, wk = _lookups()
        try:
            out = add_from_input(
                g.db, source=body.get("source") or "manual", isbn=isbn, title=title,
                author=(body.get("author") or "").strip() or None, cip_text=cip_text,
                isbn_lookup=lookup, work_key_fetch=wk)
        except CatalogueError as e:
            return jsonify({"error": type(e).__name__}), e.http_status
        return jsonify(out), 201

    @app.patch("/api/v1/wishlist/<int:item_id>")
    def api_wishlist_update(item_id: int):
        """Edit an item: set notes/priority/status, or PICK an ambiguous candidate by index
        ({"pick": N} → resolve from `candidates[N]`). Optional "rev" guards a lost update."""
        if not _auth.can_edit():
            return jsonify({"error": "read-only"}), 403
        body = request.get_json(silent=True) or {}
        acc = _acc(g.db)
        rev = body.get("rev")
        try:
            if "confirm_owned" in body:
                # The operator confirmed a suspected match IS the same book they already own →
                # mark it acquired (linked to that catalogue edition). Removes it from "wanted".
                acc.wishlist.mark_acquired(item_id, int(body["confirm_owned"]), expected_rev=rev)
            elif body.get("decline_suspected"):
                # Not the same book → drop the suspicion, keep it on the active wishlist.
                acc.wishlist.resolve(item_id, {"candidates": []}, "resolved", expected_rev=rev)
            elif "pick" in body:
                item = acc.wishlist.get(item_id)
                if item is None:
                    return jsonify({"error": "NotFound"}), 404
                idx = body["pick"]
                if not isinstance(idx, int) or not (0 <= idx < len(item.candidates)):
                    return jsonify({"error": "bad pick index"}), 422
                snap = WR.snapshot_from_candidate(item.candidates[idx])
                acc.wishlist.resolve(item_id, snap, "resolved", expected_rev=rev)
            else:
                acc.wishlist.update(
                    item_id, notes=body.get("notes"), priority=body.get("priority"),
                    status=body.get("status"), expected_rev=rev)
            g.db.commit()
        except CatalogueError as e:
            return jsonify({"error": type(e).__name__}), e.http_status
        item = acc.wishlist.get(item_id)
        return jsonify({"item": item.to_dict() if item else None}), 200

    @app.delete("/api/v1/wishlist/<int:item_id>")
    def api_wishlist_delete(item_id: int):
        """Soft-delete (tombstone) a wishlist item. Optional "rev" query/body guards a lost update."""
        if not _auth.can_edit():
            return jsonify({"error": "read-only"}), 403
        rev = request.args.get("rev", type=int)
        try:
            _acc(g.db).wishlist.remove(item_id, expected_rev=rev)
            g.db.commit()
        except CatalogueError as e:
            return jsonify({"error": type(e).__name__}), e.http_status
        return jsonify({"ok": True}), 200
