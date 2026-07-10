"""The single Library dashboard: search + browse + universal inline editor.

See [[library-dashboard-built]] and [[unified-review-edit-panes]]: `/library` is
the everyday loop on one page (search box, `+ Add book`, master list, universal
editor with entity cross-links). Includes add-by-upload (PDF/EPUB → edition +
holding → pipeline → editor) and the read-only Browse summary fragments that
reuse the same partials the editable review pane renders.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from flask import abort, jsonify, redirect, render_template, request, url_for, g

from catalogue.services import library as library_mod
from catalogue.services import reconcile as reconcile_mod
from catalogue.services import search as search_mod
from catalogue.webui.routes._shared import _acc


def register(app, ctx):
    # ── The single sectioned module, served in two modes ──────────────────
    #   read-only Search → /search (and the /library back-compat alias);
    #   editable Review → /review (bulk-select + inline edit). `request.endpoint`
    #   pins which URL we're on so links + redirects stay in the same mode.
    def _module(editable):
        """The everyday loop on one page: a search bar, `+ Add book`, and a sectioned
        pane. Searching ONE field lists candidates of that field's type
        (book→editions, work→works, person→people); two+ fields AND-combine over
        editions (the fallback). When exactly one candidate M matches — or `?eid`/
        `?wid`/`?pid` deep-links one — M's related entities are decomposed into extra
        left-pane sections (its works, its editions, its authors), each a bulk-
        selectable single-type list."""
        endpoint = request.endpoint
        bt = (request.args.get("book_title") or "").strip()
        wt = (request.args.get("work_title") or "").strip()
        # "person" matches any contributor role; `author` kept as a legacy alias.
        pe = (request.args.get("person") or request.args.get("author") or "").strip()
        su = (request.args.get("subject") or "").strip()   # prefix-inclusive subject filter
        eid = request.args.get("eid", type=int)
        wid = request.args.get("wid", type=int)
        pid = request.args.get("pid", type=int)

        # A book-title of "#N" / a bare integer is an edition-number jump (parity with
        # the old /find box): resolve straight to that edition. Only when book-title is
        # the lone active field and no entity is already deep-linked.
        eid_q = search_mod._as_id(bt) if bt else None
        if eid_q is not None and not wt and not pe and not su and not (eid or wid or pid) and \
                _acc(g.db).editions.reads.get(eid_q) is not None:
            return redirect(url_for(endpoint, eid=eid_q))

        def _eds(rows):
            for r in rows:
                r["seltype"] = "edition"
            return rows

        active = [f for f, v in (("book", bt), ("work", wt), ("person", pe), ("subject", su)) if v]
        searched = bool(active)
        # Candidate list of the searched TYPE (single field), the AND-combined edition
        # fallback (2+ fields), or browse-all (nothing searched). Subject scopes to the
        # books filed under it (edition candidates), so a lone subject search lists books.
        if active == ["work"]:
            searched_type, candidates = "work", library_mod.search_works(g.db, wt)
        elif active == ["person"]:
            searched_type, candidates = "person", library_mod.search_persons(g.db, pe)
        elif active == ["book"]:
            searched_type, candidates = "edition", _eds(library_mod.search(g.db, book_title=bt))
        elif active == ["subject"]:
            searched_type, candidates = "edition", _eds(library_mod.search(g.db, subject=su))
        elif searched:
            searched_type = "edition"
            candidates = _eds(library_mod.search(g.db, book_title=bt, work_title=wt, person=pe, subject=su))
        else:
            searched_type, candidates = "edition", _eds(library_mod.browse(g.db))

        # The selected entity M: an explicit deep-link, else the sole candidate.
        sel_type = sel_id = None
        if eid:
            sel_type, sel_id = "edition", eid
        elif wid:
            sel_type, sel_id = "work", wid
        elif pid:
            sel_type, sel_id = "person", pid
        elif len(candidates) == 1:
            sel_type, sel_id = candidates[0]["seltype"], candidates[0]["id"]

        # A bare deep-link (no search field) synthesizes a one-item candidate list of
        # the right type, so the pane has a candidate section to sit the sections under.
        if sel_type and not searched:
            searched_type = sel_type
            row = library_mod.candidate_row(g.db, sel_type, sel_id)
            candidates = [row] if row else []

        sections = library_mod.decompose(g.db, sel_type, sel_id) if sel_type else []
        cand_label = {"edition": "Books", "work": "Works", "person": "People"}[searched_type]
        panes = [{"key": "candidates", "label": cand_label, "seltype": searched_type,
                  "rows": candidates}] + sections
        return render_template("library.html", panes=panes, searched=searched,
                               searched_type=searched_type, bt=bt, wt=wt, pe=pe, su=su,
                               sel_type=sel_type, sel_id=sel_id,
                               rp_editable=editable, endpoint=endpoint,
                               starred_ids=_acc(g.db).starred.list())

    @app.get("/search")
    def search():
        """Read-only browse + decompose (no edits, no bulk) — the 'Search' nav item."""
        return _module(editable=False)

    @app.get("/review")
    def review():
        """The working surface: same module + bulk-select + inline editing."""
        return _module(editable=True)

    @app.get("/review/subjects")
    def review_subjects():
        """Subject-vocabulary curation, folded into the Review surface as its own tab:
        a fold/unfold TREE over topics (or `?kind=series` for Series/Collections).
        Rename, merge duplicates, delete orphans. Shares the row-mapping helper with
        the legacy hub deep-link (which now redirects here)."""
        from catalogue.services import subject_tree as T
        kind = (request.args.get("kind") or "topic").lower()
        if kind not in ("topic", "series"):
            kind = "topic"
        q = (request.args.get("q") or "").strip()
        items = T.subject_review_items(g.db, kind=kind, q=q)
        return render_template("review_subjects.html", items=items, q=q, kind=kind,
                               count=len(items), review_tab="subjects",
                               review_module=True)

    @app.get("/library")
    def library():
        """Back-compat alias for /search — keeps existing `/library?eid=` deep-links
        (entity cross-links, cover JS) working in the read-only view."""
        return _module(editable=False)

    @app.get("/library/suggest/person")
    def library_suggest_person():
        """Field-scoped person completions for the Browse 'Person' box — each match
        carries the ROLE(s) it plays (author / translator) for the dropdown prefix."""
        from catalogue.db_store import fold_key
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"matches": []})
        acc = _acc(g.db)
        rows = acc.persons.reads.search(q, limit=20)
        out = []
        for pid, name, dates, _ext in rows:
            roles = []
            if acc.persons.reads.is_author(pid):
                roles.append("author")
            if acc.persons.reads.is_translator(pid):
                roles.append("translator")
            out.append({"id": pid, "name": name, "dates": dates or "", "roles": roles})
        return jsonify({"matches": out})

    @app.get("/library/suggest/subject")
    def library_suggest_subject():
        """Field-scoped subject completions for the Browse 'Subject' box — name (the
        slash-path label) + how many editions sit under it, both kinds (topic/series)."""
        from catalogue.services import subjects as S
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"matches": []})
        rows = S.list_subjects(g.db, q=q, limit=20)
        return jsonify({"matches": [
            {"id": r["id"], "name": r["name"], "kind": r["kind"],
             "n_editions": r["n_editions"]} for r in rows]})

    @app.get("/edition/<int:eid>/links")
    def edition_links_card(eid):
        """Cross-navigation fragment for the universal editor: this edition's
        authors → their works, translators → their editions, and each contained
        work → its other editions/translations (volume sets grouped once)."""
        links = library_mod.edition_links(g.db, eid)
        if links is None:
            abort(404)
        return render_template("_entity_links.html", links=links)

    @app.get("/edition/<int:eid>/works-summary")
    def edition_works_summary(eid):
        """Read-only three-layer summary for Browse: Edition Basics + "Works In This
        Edition" (Work Basics + collapsed Work Details) + holdings/preview. Reuses the
        SAME partials the editable review pane renders, with editable=False."""
        from catalogue.services import subjects as S
        e = _acc(g.db).editions.reads.summary_card(eid)
        if not e:
            abort(404)
        persons = library_mod.edition_persons(g.db, eid)
        # Effective TOPICS: the edition's own override if any, else inherited from works.
        own = S.subjects_for(g.db, "edition", eid, subject_kind="topic")
        if own:
            subjects = own
        else:
            seen, subjects = set(), []
            for wid in _acc(g.db).works.reads.ids_in_edition(eid):
                for sid, n in S.subjects_for(g.db, "work", wid, subject_kind="topic"):
                    if n.casefold() not in seen:
                        seen.add(n.casefold()); subjects.append((None, n))
        # Series memberships are a separate namespace, shown on their own row.
        series = S.subjects_for(g.db, "edition", eid, subject_kind="series")
        holdings = []
        for hid, form, htype, fp, arch in _acc(g.db).holdings.reads.display_rows(eid):
            hp = fp or arch or ""
            holdings.append({"id": hid, "label": htype or form or "copy",
                             "path": hp, "has_file": bool(hp),
                             "missing": reconcile_mod.file_state(hp) == "missing",
                             "ext": (os.path.splitext(hp)[1].lstrip(".").lower() or "")})
        return render_template(
            "_edition_summary.html", editable=False, eid=eid, kind="single",
            title=e[1] or "", isbn=e[2], isbn_url=None, notes=e[3], tradition=e[4],
            authors=persons["authors"], translators=persons["translators"],
            subjects=subjects, series=series,
            works=library_mod.edition_work_summaries(g.db, eid),
            modern_commentaries=library_mod.edition_commentaries(g.db, eid),
            holdings=holdings)

    # ── Add-by-upload: a PDF/EPUB → edition + holding → pipeline → editor ───
    @app.get("/library/add")
    def library_add_form():
        return render_template("library_add.html", result=None)

    @app.post("/library/add")
    def library_add_submit():
        f = request.files.get("file")
        if not f or not f.filename:
            return render_template("library_add.html",
                                   result={"error": "Choose a PDF or EPUB file."}), 400
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".pdf", ".epub"):
            return render_template("library_add.html",
                                   result={"error": "Only .pdf or .epub files."}), 400
        # Save to a temp path, then hand to the ingest service (which copies it
        # into the managed upload dir under a collision-proof name).
        tmp = Path(app.config["UPLOAD_DIR"]) / f"_incoming_{uuid.uuid4().hex}{ext}"
        f.save(tmp)
        try:
            res = library_mod.ingest_upload(
                g.db, tmp, dest_dir=app.config["UPLOAD_DIR"],
                filename=f.filename, process=app.config["UPLOAD_PROCESS"])
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        if res.get("edition_id"):
            # The uploaded book may satisfy open capture scans of the same title — clear them
            # from the inbox (write-side) so the Capture pill doesn't count now-held duplicates.
            try:
                from catalogue.services import capture_reconcile
                capture_reconcile.reconcile_captures(g.db)
            except Exception:
                pass
            # Land in the editable Review surface with the new book selected.
            return redirect(url_for("review", eid=res["edition_id"]))
        return render_template("library_add.html", result=res), 500

    @app.post("/edition/<int:eid>/mark-reviewed")
    def edition_mark_reviewed(eid):
        """JSON mark-reviewed for the Review module's bulk bar (one POST per ticked
        edition, mirroring the Books queue's per-id contract). Sets the catalogue
        review verdict to 'ok'; a still-Uncategorized edition is reported as an error
        so the batch reports it instead of silently skipping."""
        from catalogue.services import catalogue_review as cr
        from catalogue.services.subjects import UncategorizedError
        if not _acc(g.db).editions.reads.get(eid) is not None:
            abort(404)
        try:
            cr.set_review(g.db, eid, status="ok")
        except UncategorizedError as e:
            return jsonify({"error": str(e)}), 400
        has_inbox, plan, report = _do_filing(eid)
        filing_json = None
        if has_inbox and report is not None:
            filing_json = {"auto": True, "destination": report["destination"],
                           "moved": len(report["moved"]),
                           "deferred": len(report["deferred"])}
        elif has_inbox:
            filing_json = {"auto": False, "candidates": [
                {"path": c.path, "subject": c.source_subject, "n_books": c.n_books,
                 "exists": c.exists, "is_series": c.is_series}
                for c in (plan.candidates if plan else [])]}
        return jsonify({"status": "ok", "reviewed": True, "filing": filing_json})

    @app.get("/edition/<int:eid>/works")
    def edition_works_card(eid):
        from catalogue.services import catalogue_review as cr
        return render_template(
            "_works_card.html", eid=eid, draft=cr.draft_from_edition(g.db, eid),
            structures=["", "single_work", "collection_unsegmented", "multi_work"])

    @app.post("/edition/<int:eid>/works")
    def edition_works_save(eid):
        from catalogue.services import catalogue_review as cr
        cr.apply_draft(g.db, eid, cr.parse_works_form(request.form))
        return redirect(url_for("edition_works_card", eid=eid))

    @app.get("/edition/<int:eid>/review-card")
    def edition_review_card(eid):
        from catalogue.services import catalogue_review as cr
        from catalogue.services import filing
        review = cr.get_review(g.db, eid)
        # Offer the filing panel for a reviewed book that still has an inbox copy.
        ctx = filing.build_context(g.db, eid)
        reviewed = review.get("status") == "ok"
        has_inbox = reviewed and any(h.in_inbox for h in ctx.holdings)
        plan = filing.plan_filing(g.db, eid) if has_inbox else None
        return render_template("_review_checklist.html", eid=eid, review=review,
                               has_inbox=has_inbox, plan=plan)

    @app.post("/edition/<int:eid>/review-card")
    def edition_review_save(eid):
        from catalogue.services import catalogue_review as cr
        from catalogue.services.subjects import UncategorizedError
        flags = {k: (request.form.get(f"f_{k}") is not None)
                 for k in ("title", "contributors", "structure", "authors")}
        try:
            cr.set_review(g.db, eid, status=request.form.get("status"), flags=flags,
                          note=request.form.get("note"))
        except UncategorizedError as e:
            # Blocked: still tagged Uncategorized. The card host swaps in this fragment,
            # so render it back with an inline error rather than flashing (no full reload).
            return render_template("_review_checklist.html", eid=eid,
                                   review=cr.get_review(g.db, eid), error=str(e)), 400
        # Reviewed → file the book out of the inbox onto its subject shelf (auto when
        # unambiguous; otherwise the checklist's filing panel surfaces the candidates).
        has_inbox, plan, report = (False, None, None)
        if request.form.get("status") == "ok":
            has_inbox, plan, report = _do_filing(eid)
        return render_template("_review_checklist.html", eid=eid,
                               review=cr.get_review(g.db, eid), has_inbox=has_inbox,
                               plan=plan, report=report)

    def _do_filing(eid):
        """Auto-file a just-reviewed edition out of the inbox. Returns
        `(has_inbox, plan, report)`: `has_inbox` False when the book has no inbox copy
        (nothing to do); `plan` the FilingPlan; `report` the move result when the
        destination was unambiguous and the move happened, else None (needs confirm)."""
        from catalogue.services import filing
        ctx = filing.build_context(g.db, eid)
        has_inbox = any(h.in_inbox for h in ctx.holdings)
        plan = report = None
        if has_inbox:
            plan = filing.plan_filing(g.db, eid, request.values.get("filing_protocol"))
            if plan.auto and plan.destination:
                report = filing.file_edition(g.db, eid, plan.destination.path)
        return has_inbox, plan, report

    @app.get("/edition/<int:eid>/filing")
    def edition_filing_card(eid):
        """The filing panel: candidate subject directories for this reviewed book."""
        from catalogue.services import filing
        if not _acc(g.db).editions.reads.get(eid) is not None:
            abort(404)
        ctx = filing.build_context(g.db, eid)
        plan = filing.plan_filing(g.db, eid, request.args.get("filing_protocol"))
        return render_template("_filing_panel.html", eid=eid, plan=plan,
                               has_inbox=any(h.in_inbox for h in ctx.holdings))

    @app.post("/edition/<int:eid>/filing")
    def edition_filing_save(eid):
        """Operator-confirmed filing: move the inbox copies to the chosen directory.
        An explicit destination overrides the protocol; `create_new` allows making a
        folder that doesn't exist yet (for a brand-new subject)."""
        from catalogue.services import filing
        if not _acc(g.db).editions.reads.get(eid) is not None:
            abort(404)
        dest = (request.form.get("dest") or "").strip()
        if not dest:
            abort(400)
        create_new = request.form.get("create_new") is not None
        report = filing.file_edition(g.db, eid, dest, create=create_new)
        ctx = filing.build_context(g.db, eid)
        plan = filing.plan_filing(g.db, eid)
        return render_template("_filing_panel.html", eid=eid, plan=plan,
                               has_inbox=any(h.in_inbox for h in ctx.holdings),
                               report=report)

    @app.post("/edition/<int:eid>/file")
    def edition_file_now(eid):
        """JSON file-out-of-inbox (one POST per ticked edition). With an explicit `dest`
        in the JSON body, moves the inbox copies there (the operator's chosen folder);
        without one, auto-moves when the destination is unambiguous, else reports that the
        book needs a folder choice. Works regardless of review status, so already-reviewed
        inbox books can be filed too."""
        from catalogue.services import filing
        if not _acc(g.db).editions.reads.get(eid) is not None:
            abort(404)
        body = request.get_json(silent=True) or {}
        dest = (body.get("dest") or "").strip()
        if dest:                                       # operator-chosen folder
            report = filing.file_edition(g.db, eid, dest, create=bool(body.get("create_new")))
            return jsonify({"ok": True, "in_inbox": True, "auto": True,
                            "destination": report["destination"],
                            "moved": len(report["moved"]), "deferred": len(report["deferred"])})
        has_inbox, plan, report = _do_filing(eid)
        if not has_inbox:
            return jsonify({"ok": True, "in_inbox": False})
        if report is not None:
            return jsonify({"ok": True, "in_inbox": True, "auto": True,
                            "destination": report["destination"],
                            "moved": len(report["moved"]),
                            "deferred": len(report["deferred"])})
        return jsonify({"ok": True, "in_inbox": True, "auto": False,
                        "candidates": [{"path": c.path, "subject": c.source_subject,
                                        "exists": c.exists} for c in (plan.candidates if plan else [])]})
