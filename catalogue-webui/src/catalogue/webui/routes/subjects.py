"""Subjects: the controlled vocabulary plus per-record tagging.

Two surfaces share the subject domain ([[subject-required-invariant]],
[[subject-curation-helpers]]):
  - vocabulary curation (rename / merge / delete) under the Review hub, and
  - attaching/removing subjects on an edition or work (the `_subjects_card`
    fragment hosted by the [data-card-url] browser).
"""
from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for, g


def register(app, ctx):
    # ── Vocabulary curation (Review → Subjects tab) ──────────────────────
    @app.post("/subject/<int:sid>/rename")
    def subject_rename(sid):
        from catalogue.services import subjects as S
        name = (request.form.get("name") or "").strip()
        if name:
            try:
                S.rename_subject(g.db, sid, name)
            except S.ProtectedSubjectError as e:
                flash(str(e), "error")
                return redirect(url_for("review_subjects"))
            g.db.commit()
        return redirect(url_for("review_subjects"))

    @app.post("/subject/<int:sid>/merge")
    def subject_merge(sid):
        from catalogue.services import subjects as S
        name = (request.form.get("into") or "").strip()
        from catalogue.access_api import system_conn
        into_id = system_conn(g.db).subjects.graph.id_by_name(name)
        if into_id and into_id != sid:
            try:
                S.merge_subjects(g.db, sid, into_id)
            except S.ProtectedSubjectError as e:
                flash(str(e), "error")
                return redirect(url_for("review_subjects"))
            g.db.commit()
        return redirect(url_for("review_subjects"))

    @app.post("/subject/<int:sid>/delete")
    def subject_delete(sid):
        from catalogue.services import subjects as S
        try:
            S.delete_subject(g.db, sid)
        except S.ProtectedSubjectError as e:
            flash(str(e), "error")
            return redirect(url_for("review_subjects"))
        g.db.commit()
        return redirect(url_for("review_subjects"))

    @app.get("/subject/<int:sid>/card")
    def subject_card(sid):
        """Lazy detail fragment: the works + editions a subject tags."""
        from catalogue.services import subjects as S
        return render_template("_subject_card.html", tagged=S.subject_tagged(g.db, sid))

    # ── Canonical subject browse page (the target of every subject link) ──
    @app.get("/subject/<int:sid>")
    def subject_browse(sid):
        """A subject's books, DESCENDANT-INCLUSIVE (a topic rolls up its sub-topics; a
        series lists its volumes in order). Two display mechanisms over the same shared
        subject_tree data:
          • default — a Netflix-style page of shelves (one cover/spine rail per
            sub-topic, drill-down), matching the home splash;
          • ?view=grid — the chip + poster-grid variant (kept for later)."""
        from catalogue.services import subject_tree as T
        if (request.args.get("view") or "").lower() == "grid":
            page = T.subject_page(g.db, sid)
            if not page:
                abort(404)
            return render_template("subject_browse.html", page=page)
        page = T.subject_shelves(g.db, sid)
        if not page:
            abort(404)
        return render_template("subject_shelves.html", page=page)

    # ── Per-record tagging (edition/work subject chips) ──────────────────
    def _subjects_redirect(kind, pid):
        # The card host (data-card-url) ignores this target and refetches the GET
        # fragment itself; full-page forms (the work page) follow it back home.
        if request.referrer:
            return redirect(request.referrer)
        return redirect(url_for("work_detail", wid=pid) if kind == "work"
                        else url_for("subjects_card", kind=kind, pid=pid))

    @app.get("/subjects/<kind>/<int:pid>")
    def subjects_card(kind, pid):
        if kind not in ("edition", "work"):
            abort(404)
        from catalogue.services import subjects as S
        rows = S.subjects_for(g.db, kind, pid, subject_kind="topic")
        series = S.subjects_for(g.db, kind, pid, subject_kind="series")
        sug = S.suggest_edition_subject(g.db, pid) if kind == "edition" else None
        if sug and any(name.casefold() == sug.casefold() for _, name in rows):
            sug = None                       # already attached — don't re-offer
        return render_template("_subjects_card.html", kind=kind, pid=pid,
                               subjects=rows, series=series, suggestion=sug)

    @app.post("/subjects/<kind>/<int:pid>/add")
    def subjects_add(kind, pid):
        if kind not in ("edition", "work"):
            abort(404)
        from catalogue.services import subjects as S
        name = (request.form.get("name") or "").strip()
        subject_kind = (request.form.get("subject_kind") or "topic").lower()
        if subject_kind not in ("topic", "series"):
            subject_kind = "topic"
        if name:
            # add_subject lifts the Uncategorized placeholder for a real TOPICAL add,
            # and (correctly) leaves it in place for a series add.
            S.add_subject(g.db, kind, pid, name, subject_kind=subject_kind)
            g.db.commit()
        return _subjects_redirect(kind, pid)

    @app.post("/subjects/<kind>/<int:pid>/remove")
    def subjects_remove(kind, pid):
        if kind not in ("edition", "work"):
            abort(404)
        from catalogue.services import subjects as S
        sid = request.form.get("subject_id", type=int)
        if sid:
            S.remove_subject(g.db, kind, pid, sid)
            # Keep the invariant: removing the last subject re-tags Uncategorized so
            # nothing is ever subject-less (and review stays blocked until recategorized).
            S.ensure_categorized(g.db, kind, pid)
            g.db.commit()
        return _subjects_redirect(kind, pid)
