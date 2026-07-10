"""The home hub and the Review hub tab strip.

See [[home-splash-and-label-swap]] and [[dashboard-5-feature-hub]]: `/` is the
entry-point hub; `/review-hub` redirects the Books / Works / People queues
(Subjects now lives on the Review module at `/review/subjects`). The old
type-grouped `/find` ("Browse") surface is gone — the Search page (/search)
covers it.
"""
from __future__ import annotations

from flask import redirect, render_template, request, url_for, g

from catalogue.webui.routes._shared import _acc, review_backlog_counts


def register(app, ctx):
    # ── Home hub: 5 entry points (Browse / Search / Review / Scan / Capture) ──
    @app.get("/")
    def dashboard():
        # Per-tab Review backlog (mirrors what /review-hub lists), so the Review
        # card can show a single "pending" badge = books + works + people + subjects.
        review = review_backlog_counts(g.db)
        acc = _acc(g.db)
        badges = {
            "review":  sum(review.values()),
            "scan":    acc.review.reads.pending_count("ingest"),
            "capture": acc.capture.unresolved_count(),
            "books":   acc.editions.reads.count(),
        }
        # The splash rails are composed CLIENT-SIDE by the shared Tier-2 presenter
        # (LibraryCore.homeVM over the cached /api/v1/replica), so web/PWA/native build identical
        # shelves. The server supplies only the one PRIMITIVE a client can't know locally: which
        # editions were recently OPENED (global last_opened).
        recent_ids = acc.editions.reads.recently_opened(24)
        return render_template("home.html", badges=badges, review=review, recent_ids=recent_ids)

    # ── Review hub: one tabbed surface (Books / Works / People / Subjects) ──
    # Books/Works/People are the existing per-queue pages (they render the shared
    # tab strip via review_tab); Subjects is curated right here.
    @app.get("/review-hub")
    def review_hub():
        tab = (request.args.get("tab") or "books").lower()
        if tab == "books":
            return redirect(url_for("works_detect_single"))
        if tab == "works":
            return redirect(url_for("works_incomplete"))
        if tab == "people":
            return redirect(url_for("picker_list", kind="person"))
        # tab == "subjects": the subject curation surface now lives ON the Review module
        # (/review/subjects); send the legacy hub deep-link there so there's one home.
        return redirect(url_for("review_subjects", kind=request.args.get("kind"),
                                q=request.args.get("q")))
