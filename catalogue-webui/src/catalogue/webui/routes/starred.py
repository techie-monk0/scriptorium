"""Starred-editions API (`/api/v1/starred`) — the favourited editions behind the Starred home rail
and the highlighted star drawn on every cover.

A single shared library-wide set of edition ids. The list GET is ETagged (like `/api/v1/replica` and
`/api/v1/wishlist`) so offline clients cache it cheaply and feed it to the shared `homeVM`; the
toggle writes require edit capability. Persistence is `acc.starred`; the access-API maps
`NotFound`/`StaleWrite` to their HTTP status. Each write returns the fresh list so a client refreshes
its in-memory `starredIds` in one round-trip.
"""
from __future__ import annotations

import hashlib
import json as _json

from flask import g, jsonify, make_response, request

from catalogue.contracts import CatalogueError
from catalogue.webui import auth as _auth
from ._shared import _acc


def _list_payload(db) -> dict:
    """The presenter-agnostic starred payload clients cache + feed to `homeVM(starredIds)`."""
    return {"editions": _acc(db).starred.list(), "schema": 1}


def register(app, ctx):
    @app.get("/api/v1/starred")
    def api_starred_list():
        """The shared starred-edition list, newest-starred first. ETagged over content so an
        unchanged list 304s; clients cache it (same pattern as /api/v1/replica)."""
        etag = '"' + hashlib.sha256(
            _acc(g.db).starred.fingerprint().encode("utf-8")).hexdigest()[:32] + '"'
        if request.headers.get("If-None-Match") == etag:
            resp = make_response("", 304)
        else:
            resp = make_response(_json.dumps(_list_payload(g.db), ensure_ascii=False), 200)
            resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.post("/api/v1/starred")
    def api_starred_add():
        """Star an edition. Body: {"edition_id": N}. Idempotent. Returns the fresh starred list."""
        if not _auth.can_edit():
            return jsonify({"error": "read-only"}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("edition_id"), int):
            return jsonify({"error": "provide an integer edition_id"}), 422
        try:
            _acc(g.db).starred.star(body["edition_id"])
            g.db.commit()
        except CatalogueError as e:
            return jsonify({"error": type(e).__name__}), e.http_status
        return jsonify({"ok": True, **_list_payload(g.db)}), 201

    @app.delete("/api/v1/starred/<int:edition_id>")
    def api_starred_delete(edition_id: int):
        """Un-star an edition (a no-op if it wasn't starred). Returns the fresh starred list."""
        if not _auth.can_edit():
            return jsonify({"error": "read-only"}), 403
        try:
            _acc(g.db).starred.unstar(edition_id)
            g.db.commit()
        except CatalogueError as e:
            return jsonify({"error": type(e).__name__}), e.http_status
        return jsonify({"ok": True, **_list_payload(g.db)}), 200
