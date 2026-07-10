"""Full-text content search (`/search`).

Passages (chunks) match independently; results are grouped by EDITION — one card
per book, labelled title + authors, showing its top few matching passages (not a
single snippet, and not one row per copy). The service is swappable for tests via
`current_app_search`.
"""
from __future__ import annotations

from flask import render_template, request, g

from catalogue.services.search import SearchService

SEARCH_SNIPPETS_PER_BOOK = 5


def current_app_search(app) -> SearchService:
    """Indirection so tests/config can swap the service."""
    return app.config["SEARCH"]


def register(app, ctx):
    # ── Full-text "Text" content search (moved off /search; that URL is now the
    #    read-only browse module — see library_dashboard.py). ──────────────
    @app.get("/text")
    def text_search():
        # Full-text "Content search": passages (chunks) match independently; group them by
        # EDITION — one card per book, labelled by title + authors — and show its top few
        # matching passages. Grouping is shared with /api/v1/content (one source of truth).
        q = (request.args.get("q") or "").strip()
        results = current_app_search(app).search_grouped(
            g.db, q, snippets_per_book=SEARCH_SNIPPETS_PER_BOOK) if q else []
        return render_template("search.html", q=q, results=results)
