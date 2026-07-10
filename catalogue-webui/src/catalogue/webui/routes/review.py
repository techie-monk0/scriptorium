"""Review queue (Step 3a) + staging resolution.

The flat queue list + per-item detail, the rich authority accept/reject cards,
and the capture_staging → edition/holding resolve path. (The old book_toc_pattern
proposal-promotion master-detail UI was removed; the promotion domain logic lives
on in `catalogue.services.promote`.)
"""
from __future__ import annotations

import json

from flask import (
    abort, flash, redirect, render_template, request, url_for, g,
)

from catalogue.services import verify as verify_mod
from catalogue.services import work_authority as work_authority_mod
from catalogue.services import person_work as person_work_mod
from catalogue.services import work_titles as work_titles_mod
from catalogue.services import edition_resolve as edition_resolve_mod
from catalogue.services import edition_verify as edition_verify_mod
from catalogue.services import intake_match
from catalogue.services.isbn import normalize_isbn
from catalogue.webui.routes import _shared
from catalogue.webui.routes._shared import _acc


def register(app, ctx):
    # ── Review queue (Step 3a) ───────────────────────────────────────────
    #    Moved off /review → /review-queue: the /review URL now serves the
    #    editable Review module (library_dashboard.py). Endpoint names keep
    #    their `review_*` prefix.
    @app.get("/review-queue")
    def review_queue():
        """Flat list of queue items at a status, optionally filtered to one type;
        each row links to its per-item detail page (authority / OCR resolution)."""
        item_type = request.args.get("type") or None
        status = request.args.get("status") or "pending"

        acc = _acc(g.db)
        # Type tabs: count by item_type at the current status.
        type_counts = acc.review.reads.type_counts(status)
        items = [
            {"id": r[0], "item_type": r[1], "status": r[2]}
            for r in acc.review.reads.list_at_status(status, item_type, 500)
        ]
        types = acc.review.reads.item_type_codes()
        return render_template(
            "review.html",
            items=items, types=types, type_counts=type_counts,
            current_type=item_type, current_status=status,
        )

    # Authority-candidate item types get a rich accept/reject card; everything
    # else uses the generic review_detail view.
    _AUTHORITY_TYPES = ("person_authority", "work_canonical", "work_authorship",
                        "person_work_joint", "title_proposal", "edition_metadata",
                        "edition_verify")

    def _authority_ctx(item_type, payload):
        """Local row label + click-through authority URL for the accept card."""
        if item_type in ("person_authority", "person_work_joint"):
            pid = payload.get("person_id")
            p = _acc(g.db).persons.reads.get(pid)
            label = p.primary_name if p else f"person {pid}"
            url = _shared.authority_url(payload.get("candidate_id"))
        elif item_type == "edition_verify":
            # No single external page — the "record" is the book; show its title.
            eid = payload.get("edition_id")
            ed = _acc(g.db).editions.reads.get(eid)
            label = ed.title if ed else f"edition {eid}"
            url = None
        elif item_type in ("title_proposal", "edition_metadata"):
            # No external authority page — the "record" is the book we're editing;
            # show its current (old) title.
            label = payload.get("old_title") or f"edition {payload.get('edition_id')}"
            url = None
        else:  # work_canonical | work_authorship
            wid = payload.get("work_id")
            label = _acc(g.db).works.reads.representative_title(wid) or f"work {wid}"
            url = _shared.authority_url(payload.get("candidate_id")
                                        or payload.get("canonical_number"))
        return {"local_label": label, "authority_url": url}

    @app.get("/review-queue/<int:item_id>")
    def review_detail(item_id):
        row = _acc(g.db).review.reads.detail(item_id)
        if not row:
            abort(404)
        payload = json.loads(row[2]) if row[2] else {}
        if row[1] in _AUTHORITY_TYPES:
            item = {"id": row[0], "item_type": row[1], "payload": payload,
                    "status": row[3], "created_at": row[4], "resolved_at": row[5]}
            return render_template("_authority_review.html", item=item,
                                   **_authority_ctx(row[1], payload))
        # For low_quality_ocr, fish out the holding so the template can link
        # to it and offer the override action.
        related_holding = None
        if row[1] == "low_quality_ocr" and payload.get("file_hash"):
            related_holding = _acc(g.db).holdings.reads.ocr_review_holding(payload["file_hash"])
        return render_template(
            "review_detail.html",
            row=row, payload=payload, related_holding=related_holding,
        )

    @app.post("/review-queue/<int:item_id>/authority/accept")
    def review_authority_accept(item_id):
        """Bind a queued authority candidate onto its person/work."""
        it = _acc(g.db).review.reads.get(item_id)
        it = (it["item_type"],) if it else None
        if not it:
            abort(404)
        t = it[0]
        if t == "person_authority":
            verify_mod.accept_person_authority(g.db, item_id)
        elif t == "work_canonical":
            verify_mod.accept_work_canonical(g.db, item_id)
        elif t == "work_authorship":
            work_authority_mod.accept_work_authorship(g.db, item_id)
        elif t == "person_work_joint":
            person_work_mod.accept_person_work_joint(g.db, item_id)
        elif t == "title_proposal":
            work_titles_mod.accept_title_proposal(g.db, item_id)
        elif t == "edition_metadata":
            edition_resolve_mod.accept_edition_metadata(g.db, item_id)
        elif t == "edition_verify":
            edition_verify_mod.accept_edition_verify(g.db, item_id)
        else:
            abort(400)
        return redirect(url_for("review_detail", item_id=item_id))

    @app.post("/review-queue/<int:item_id>/authority/reject")
    def review_authority_reject(item_id):
        it = _acc(g.db).review.reads.get(item_id)
        it = (it["item_type"],) if it else None
        if not it:
            abort(404)
        t = it[0]
        if t == "work_authorship":
            work_authority_mod.reject_work_authorship(g.db, item_id)
        elif t == "person_work_joint":
            person_work_mod.reject_person_work_joint(g.db, item_id)
        elif t == "title_proposal":
            work_titles_mod.reject_title_proposal(g.db, item_id)
        elif t == "edition_metadata":
            edition_resolve_mod.reject_edition_metadata(g.db, item_id)
        elif t == "edition_verify":
            edition_verify_mod.reject_edition_verify(g.db, item_id)
        else:
            verify_mod.reject_candidate(g.db, item_id)
        return redirect(url_for("review_detail", item_id=item_id))

    @app.post("/review-queue/<int:item_id>/resolve")
    def review_resolve(item_id):
        acc = _acc(g.db)
        row = acc.review.reads.get(item_id)
        if not row:
            abort(404)
        # Idempotent: a second resolve on the same item is a no-op redirect
        # (the user clicking twice should not double-side-effect).
        if row["status"] == "resolved":
            return redirect(url_for("review_detail", item_id=item_id))

        action = request.form.get("action") or "resolve"

        # Item-type-specific side effects. The plan reserves merge/edition
        # dedup logic for Step 5; here we only implement the side effect
        # we can soundly do now: the OCR-quality override.
        if action == "ocr_override" and row["item_type"] == "low_quality_ocr":
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            fh = payload.get("file_hash")
            if fh:
                acc.holdings.writes.set_text_status_by_hash(fh, "ocr_good")

        acc.review.writes.resolve(item_id)
        g.db.commit()
        return redirect(url_for("review_detail", item_id=item_id))

    # ── Staging resolution (Step 3a; capture lands in 3b) ────────────────
    @app.get("/staging")
    def staging_list():
        rows = _acc(g.db).capture.raw_list()
        return render_template("staging.html", rows=rows)

    def _staging_matches(row, metadata):
        """Existing editions this scan likely DUPLICATES — exact ISBN across
        edition/holding/edition_isbn PLUS title-containment-with-a-shared-author,
        so a different printing (different ISBN) of a book we already hold — even one
        whose contributors we haven't recorded yet — is still surfaced. Local-only
        (no network); reused by the resolve guard so "create new" can never silently
        duplicate an already-catalogued book."""
        return intake_match.resolve_candidates(
            g.db, isbn=row[2] or None, meta=metadata)

    @app.get("/staging/<int:sid>")
    def staging_detail(sid):
        acc = _acc(g.db)
        row = acc.capture.detail(sid)
        if not row:
            abort(404)
        metadata = json.loads(row[6]) if row[6] else None
        return render_template(
            "staging_detail.html",
            row=row, matches=_staging_matches(row, metadata), metadata=metadata,
        )

    @app.post("/staging/<int:sid>/discard")
    def staging_discard(sid):
        """Delete a captured scan without cataloguing anything — a mis-scan, a book you
        decided not to add, or junk from a stray tap. Always available on the detail page."""
        acc = _acc(g.db)
        if not acc.capture.detail(sid):
            abort(404)
        acc.capture.discard(sid)
        g.db.commit()
        # Back to the Capture page (a fresh GET, so the just-deleted scan is gone),
        # not the staging list — that's where the operator came from.
        return redirect(url_for("capture_form"))

    @app.post("/staging/<int:sid>/resolve")
    def staging_resolve(sid):
        acc = _acc(g.db)
        row = acc.capture.detail(sid)
        if not row:
            abort(404)
        if row[5] == "resolved":
            return redirect(url_for("staging_list"))

        metadata = json.loads(row[6]) if row[6] else None
        matches = _staging_matches(row, metadata)
        resolution = (request.form.get("resolution") or "").strip()

        # Confirmed duplicate: the book is already in the catalogue → add NOTHING
        # (no edition, no holding); just close the capture row.
        if resolution.startswith("match:"):
            try:
                match_id = int(resolution.split(":", 1)[1])
            except ValueError:
                abort(400)
            if not any(m["id"] == match_id for m in matches):
                abort(400)               # stale/forged pick — candidate no longer offered
            acc.capture.resolve(sid)
            g.db.commit()
            return redirect(url_for("edition_detail", eid=match_id))

        # Creating a new edition requires an EXPLICIT "add as a new book" choice.
        # Anything else — nothing selected, or an accidental Resolve — is a no-op that
        # just re-renders with a prompt, so a stray click can never write to the DB
        # (and a duplicate can never be created without the operator saying so).
        if resolution != "new":
            return render_template(
                "staging_detail.html", row=row, matches=matches, metadata=metadata,
                notice=("Nothing added. Pick the book this matches, or choose "
                        "“add as a new book”, then Resolve."
                        if matches else
                        "Nothing added. Choose “add as a new book”, then Resolve."))

        # Explicit new → create the edition + holding.
        title = (request.form.get("title") or "").strip() or "Untitled"
        isbn = normalize_isbn(request.form.get("isbn") or "") or row[2]
        edition_id = acc.editions.writes.create({"title": title, "isbn": isbn}).target.id
        from catalogue.services import subjects as S
        if S.ensure_categorized(g.db, "edition", edition_id):
            flash("New edition tagged “Uncategorized” — assign a real subject "
                  "during review; it can’t be marked reviewed until then.", "warn")

        acc.holdings.writes.insert_holding(
            edition_id=edition_id, form=row[1] or "physical", shelf_location=row[4])
        acc.capture.resolve(sid)
        g.db.commit()
        # Key the new edition so the NEXT scan of a different format matches it
        # cross-format. Best-effort — never block the resolve.
        try:
            intake_match.ensure_ol_work_key(
                g.db, edition_id, fetch=app.config.get("ISBN_WORK_KEY_LOOKUP"))
        except Exception:
            pass
        # This new edition may now satisfy OTHER open scans of the same book — clear them
        # out of the inbox (write-side), so the Capture pill/worklist don't count duplicates.
        try:
            from catalogue.services import capture_reconcile
            capture_reconcile.reconcile_captures(g.db)
        except Exception:
            pass
        return redirect(url_for("edition_detail", eid=edition_id))
