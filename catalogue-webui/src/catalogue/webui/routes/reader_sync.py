"""Offline-first reader-state sync endpoint (reader_module_plan.md Phase 2 / §5).

One endpoint reconciles per-copy reader state across the user's devices (web, the offline
PWA, a future native app) — bookmarks, reading position*, and annotations (highlights,
underline, strikeout, notes, handwritten ink). This module is *transport only*: it parses the
HTTP/JSON, enforces the auth gate, and delegates every read/write to a `ReaderStateStore` (the
reader-state PORT in catalogue.db_store.reader_state), which owns the tables and the SQL. No
hand-written SQL lives here, and the route depends on the ABSTRACT store — the SQLite adapter is
the default, but a remote/in-memory implementation can be injected via `ctx.reader_store_factory`
(a `conn -> ReaderStateStore` callable) without touching this transport code.

  GET  /sync/reader?since=<rev>
       → {"rev": <cursor>, "bookmarks": [...], "annotations": [...]}  (rows with rev > since,
         INCLUDING tombstones, so a deletion on one device propagates instead of reappearing).

  POST /sync/reader  body {"ops": [ {"type":"bookmark"|"annotation", id, holding_id, …} , … ]}
       → {"rev": <cursor>, "applied": [ {"id","rev"} | {"id","skipped":true} , … ]}
       Each op is an idempotent last-write-wins upsert keyed by the client id. Writes are
       editor-only (a read-only viewer reads books but doesn't own the library's marks).
       Best-effort per op: one bad op (e.g. a holding that no longer exists) is skipped, never
       failing the whole batch.
"""
from __future__ import annotations

from dataclasses import asdict

from flask import g, jsonify, request

from catalogue.webui import auth as auth_mod
from catalogue.db_store.reader_state import SqliteReaderStateStore
from catalogue.db_store import (
    reader_sync_contract_descriptor,
    reader_sync_contract_version_payload as _contract_version,
)


def register(app, ctx):
    # The store is the swappable seam: default to the SQLite adapter over the request's
    # connection; a host (tests, a future remote backend) can inject `ctx.reader_store_factory`.
    store_factory = getattr(ctx, "reader_store_factory", None) or SqliteReaderStateStore

    @app.get("/sync/reader/contract")
    def reader_sync_contract_route():
        # The published, versioned wire contract (catalogue.reader_sync). A client fetches this
        # once to learn the shape + version it must speak; every /sync/reader response also carries
        # `contract_version` so drift is caught on each sync. See db_store/reader_sync_contract.py.
        return jsonify(reader_sync_contract_descriptor())

    @app.get("/sync/reader")
    def reader_sync_pull():
        store = store_factory(g.db)
        # `?holding=<id>` is the cheap, reader-scoped read (one book's live marks) — what the reader
        # uses to paint, so opening never pulls the whole library's bookmark/annotation tables. The
        # `?since=<rev>` form is the offline-sync delta pull (all holdings, incl. tombstones).
        holding = request.args.get("holding", type=int)
        if holding is not None:
            # `?holding&since` = that copy's deltas INCLUDING tombstones (the native/PWA reader's
            # offline-sync pull, so cross-device deletions propagate). Bare `?holding` stays the
            # cheap LIVE paint the web reader uses on open.
            since_arg = request.args.get("since")
            if since_arg is not None:
                try:
                    since = int(since_arg)
                except (TypeError, ValueError):
                    since = 0
                bookmarks = [asdict(bm) for bm in store.bookmarks_since_for_holding(holding, since)]
                annotations = [asdict(a) for a in store.annotations_since_for_holding(holding, since)]
                outlines = [asdict(o) for o in store.outlines_since_for_holding(holding, since)]
            else:
                bookmarks = [asdict(bm) for bm in store.bookmarks_for_holding(holding)]
                annotations = [asdict(a) for a in store.annotations_for_holding(holding)]
                one = store.outline_for_holding(holding)
                outlines = [asdict(one)] if one else []
            return jsonify({"rev": store.cursor(),
                            "bookmarks": bookmarks, "annotations": annotations,
                            "outlines": outlines, **_contract_version()})
        try:
            since = int(request.args.get("since", "0"))
        except (TypeError, ValueError):
            since = 0
        bookmarks = [asdict(bm) for bm in store.bookmarks_since(since)]
        annotations = [asdict(a) for a in store.annotations_since(since)]
        outlines = [asdict(o) for o in store.outlines_since(since)]
        return jsonify({"rev": store.cursor(),
                        "bookmarks": bookmarks, "annotations": annotations,
                        "outlines": outlines, **_contract_version()})

    @app.get("/sync/reader/rev")
    def reader_sync_rev():
        # The cheap CHANGE PROBE: max rev per resource for ONE copy, so a reader can ask "did this
        # book's bookmarks / outline / annotations change on another device since I last synced?"
        # without pulling any rows — then only do the full `?holding&since` pull when something did.
        # Editor-only, same boundary as the pull (the auth before_request gate keys on the endpoint
        # name; a viewer doesn't sync marks). A pure read → no `can_edit()` here.
        store = store_factory(g.db)
        holding = request.args.get("holding", type=int)
        if holding is None:
            return jsonify({"error": "holding required"}), 400
        return jsonify({**store.holding_revs(holding), **_contract_version()})

    @app.post("/sync/reader")
    def reader_sync_push():
        # Reader marks are the library owner's; a read-only viewer doesn't write them.
        if not auth_mod.can_edit():
            return jsonify({"error": "read-only"}), 403
        store = store_factory(g.db)
        data = request.get_json(silent=True) or {}
        applied = []
        for op in (data.get("ops") or []):
            kind = op.get("type")
            oid = op.get("id")
            try:
                if kind == "bookmark":
                    row = store.apply_bookmark(
                        id=oid, holding_id=op.get("holding_id"),
                        locator=op.get("locator"), fraction=op.get("fraction"),
                        label=op.get("label"), created_at=op.get("created_at"),
                        updated_at=op.get("updated_at"), deleted_at=op.get("deleted_at"))
                elif kind == "annotation":
                    row = store.apply_annotation(
                        id=oid, holding_id=op.get("holding_id"),
                        kind=op.get("kind"), cfi_range=op.get("cfi_range"),
                        page=op.get("page"), rect=op.get("rect"), color=op.get("color"),
                        note_text=op.get("note_text"), ink=op.get("ink"),
                        created_at=op.get("created_at"), updated_at=op.get("updated_at"),
                        deleted_at=op.get("deleted_at"))
                elif kind == "outline":
                    row = store.apply_outline(
                        id=oid, holding_id=op.get("holding_id"), entries=op.get("entries"),
                        created_at=op.get("created_at"), updated_at=op.get("updated_at"),
                        deleted_at=op.get("deleted_at"))
                else:
                    continue
            except Exception:
                # malformed op, or the holding was deleted out from under an offline edit —
                # skip it, don't sink the batch.
                applied.append({"id": oid, "skipped": True})
                continue
            applied.append({"id": row.id, "rev": row.rev} if row is not None
                           else {"id": oid, "skipped": True})
        g.db.commit()
        return jsonify({"rev": store.cursor(), "applied": applied, **_contract_version()})
