"""The "Ask" feature: a grounded-Q&A panel backed by an external RAG service.

`POST /api/v1/ask` is a same-origin, authed proxy to the configured `AskBackend`
(BuddhistLLM by default, `app.config["ASK_BACKEND"]`). It forwards the FULL message
history (the backend's own multi-turn scoping depends on it) and enriches each returned
source with a local edition link, so a citation deep-links back into the catalogue.

Kept behind the same `/api/v1/*` seam as every other client-facing feature: the browser
never talks to the model host directly; this route is the only thing that does.
"""
from __future__ import annotations

from flask import g, jsonify, render_template, request

from catalogue.webui.ask import AskUnavailable


def register(app, ctx):
    def _backend():
        return app.config.get("ASK_BACKEND")

    # ── Ask page (the "ask" app section) ─────────────────────────────────
    @app.get("/ask")
    def ask_page():
        """The grounded-Q&A page: mounts LibraryUI.ask on the shared web adapter, which talks to
        /api/v1/ask below. No server state — the panel keeps its own chat history."""
        return render_template("ask.html")

    def _link_source(db, s):
        """Attach a local `eid` to a source when its durable `edition_pub_id` resolves to an
        edition we hold, so the client can deep-link the citation. If there's no resolver or no
        match, `eid` stays None and the client renders the source as plain text."""
        s = dict(s)
        pub = s.get("edition_pub_id")
        resolver = getattr(ctx, "edition_id_for_pub", None)   # seam: catalogue pub_id → local eid
        s["eid"] = resolver(db, pub) if (resolver and pub) else None
        return s

    @app.get("/api/v1/ask/models")
    def api_ask_models():
        """Which grounded models the backend advertises (for the panel's model picker).
        `available:false` (never an error) when Ask isn't configured or the backend is down."""
        b = _backend()
        if b is None:
            return jsonify({"available": False, "models": []})
        try:
            return jsonify({"available": True, "models": b.models()})
        except AskUnavailable as e:
            return jsonify({"available": False, "models": [], "error": str(e)})

    @app.post("/api/v1/ask")
    def api_ask():
        """One grounded turn. Body: `{model, messages:[…full history…]}`. Returns
        `{available, model, content, sources:[{…,eid}], timing}`. A backend outage is a soft
        502 with `available:false` so the panel shows "Ask is offline", not a hard failure."""
        b = _backend()
        if b is None:
            return jsonify({"available": False,
                            "error": "Ask isn’t configured on this server."}), 503
        body = request.get_json(silent=True) or {}
        model = body.get("model") or "library-fast"
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return jsonify({"available": True, "error": "No question."}), 400
        try:
            r = b.ask(model, messages)
        except AskUnavailable as e:
            return jsonify({"available": False, "error": str(e)}), 502
        sources = [_link_source(g.db, s) for s in r.get("sources", [])]
        return jsonify({"available": True, "model": r.get("model", model),
                        "content": r.get("content", ""), "sources": sources,
                        "timing": r.get("timing", {})})
