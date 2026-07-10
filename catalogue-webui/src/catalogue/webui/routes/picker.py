"""Authority picker: manual resolution of contributors/works (CLI twin is
`catalogue.services.picker`).

A list-the-choices-and-pick UI generic over entity KIND ([[picker-bulk-select]],
[[picker-detail-refresh-and-undo]]): lazy per-row candidate fetch, bind +
on-bind dedup in one transaction, bulk ops, authority-linked person dedup, and
the reversible split/delete/merge/undo journal. Person editing (alias/rename/new)
and live authority search live here too.
"""
from __future__ import annotations

from flask import abort, jsonify, redirect, render_template, request, url_for, g

from catalogue.db_store import add_alias as _add_alias
from catalogue.webui.routes._shared import _acc
from catalogue.db_store import SchemaDriftError, WriteError
from catalogue.db_store.integrity import IntegrityError
from catalogue.services import library as library_mod


def register(app, ctx):
    # ── Authority picker (manual resolution; CLI twin is catalogue.picker) ──
    # List-the-choices-and-pick UI, generic over entity KIND. Candidates are
    # fetched lazily per row (one network call per "find candidates" click, the
    # same [data-card-url] idiom as /review) so opening the page is cheap.
    @app.get("/picker")
    def picker_home():
        return redirect(url_for("picker_list", kind="person"))

    @app.get("/picker/<kind>")
    def picker_list(kind):
        from catalogue.services import picker as P
        if kind not in P.KINDS:
            abort(404)
        limit = request.args.get("limit", type=int) or 100
        rows = P.unresolved(g.db, kind, limit=limit)
        total = P.count_unresolved(g.db, kind)      # M in the "N of M" header
        # Shape each unresolved entry into the master-detail browser contract so
        # the same keyboard shell (_book_browser.html) drives person review.
        items = [{
            "id": eid, "title": label or "(unnamed)",
            "subtitle": f"{cur or 'unbound'} · {len(aliases)} "
                        + ("alias" if len(aliases) == 1 else "aliases"),
            "done": False, "current": cur, "aliases": aliases, "label": label,
        } for (eid, label, cur, aliases) in rows]
        # Surface the curator note (disambiguation rationale) in the review pane, so the
        # reviewer sees WHY a row is its own person without opening the full person page.
        if kind == "person" and items:
            meta = _acc(g.db).persons.reads.notes_suggestions([it["id"] for it in items])
            for it in items:
                row = meta.get(it["id"])
                it["notes"] = row[1] if row else None
                # A picked-but-unconfirmed authority id (from the add-person form): the
                # reviewer one-click accepts it, which runs on-bind dedup before verifying.
                it["suggested"] = row[2] if row else None
        return render_template("picker.html", kind=kind, spec=P.KINDS[kind],
                               items=items, rows=rows, kinds=sorted(P.KINDS),
                               limit=limit, total=total,
                               review_tab=("people" if kind == "person" else None),
                               bulk_ops=[o.as_dict() for o in P.bulk_ops(kind)])

    @app.get("/picker/person/<int:pid>/books")
    def picker_person_books(pid):
        """Books this contributor appears in (author or translator), each title
        linking to the file viewer. Lazy fragment for the person review pane."""
        return render_template("_person_books.html",
                               books=library_mod.person_books(g.db, pid), pid=pid)

    @app.get("/picker/person/<int:pid>/works")
    def picker_person_works(pid):
        """Works (FRBR layer above books) this contributor authored or translated,
        each linking to /work/<id>. Lazy fragment for the person review pane."""
        return render_template("_person_works.html",
                               works=library_mod.person_works(g.db, pid), pid=pid)

    @app.get("/picker/<kind>/<int:eid>/candidates")
    def picker_candidates(kind, eid):
        from catalogue.services import picker as P
        if kind not in P.KINDS:
            abort(404)
        items = P.unresolved(g.db, kind, ids=[eid])
        if not items:
            abort(404)
        _id, label, current, aliases = items[0]
        # ?q=… lets the operator re-search with a better term when the stored name
        # form doesn't surface the right record (e.g. a bare Wylie mononym). A query
        # shaped like an authority id (bdr:P123 / wikidata:Q42 / viaf:…) is resolved
        # to that exact record instead of a name search — empty result ⇒ "no match".
        q = (request.args.get("q") or "").strip()
        if q:
            by_id = P.lookup_by_id(g.db, kind, q)
            cands = by_id if by_id is not None else P.gather(g.db, kind, q, ())
        else:
            cands = P.gather(g.db, kind, label, aliases)
        return render_template("_picker_candidates.html", kind=kind, eid=eid,
                               label=label, current=current, candidates=cands,
                               query=q, query_is_id=P.looks_like_authority_id(q))

    @app.post("/picker/<kind>/<int:eid>/bind")
    def picker_bind(kind, eid):
        from catalogue.services import picker as P
        if kind not in P.KINDS:
            abort(404)
        cid = (request.form.get("candidate_id") or "").strip()
        if not cid:
            abort(400)
        cand = P.Candidate(cid, (request.form.get("source") or "").strip(),
                           (request.form.get("label") or "").strip())
        force = request.form.get("force") == "1"
        # Bind + on-bind dedup share ONE transaction (§6.12), via the same helper the
        # CLI picker uses so the two surfaces never drift: a person bound to an id
        # another record already holds is merged (or flagged) on the spot.
        #
        # Surface DB-level failures instead of swallowing them: a write that throws
        # (missing column → SchemaDriftError; rolled-back/no-op → WriteError; any
        # sqlite error) would otherwise 500 and the bind would silently vanish on
        # reload — the exact bug this guard exists to make impossible to hide.
        import sqlite3
        try:
            res = P.bind_with_dedup(g.db, kind, eid, cand, force=force)
        except (IntegrityError, SchemaDriftError, WriteError, sqlite3.Error) as e:
            try:
                g.db.rollback()
            except Exception:
                pass
            msg = f"Bind did not persist — {type(e).__name__}: {e}"
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify({"ok": False, "error": msg}), 500
            abort(500, msg)
        ok, dedup = res["ok"], res["dedup"]
        if request.headers.get("X-Requested-With") == "fetch":
            resp = {"ok": ok, "id": cid}
            if dedup:
                resp["dedup"] = dedup        # {'merged_into': …} or {'suggest': […]}
            if not ok:
                # Distinguish "already bound" (offer a rebind) from a real failure.
                acc = _acc(g.db)
                if kind == "person":
                    p = acc.persons.reads.get(eid)
                    bound = p.external_id if p else None
                else:
                    wf = acc.works.reads.summary_fields(eid)
                    bound = wf[4] if wf else None
                if bound:
                    resp["already_bound"] = True
                    resp["current"] = bound
            return jsonify(resp)
        return redirect(url_for("picker_list", kind=kind))

    @app.post("/picker/<kind>/bulk")
    def picker_bulk(kind):
        """Apply ONE operation to a SET of selected rows at once (the action bar's
        Apply button). Body: {op, ids:[…], target?}. Routes through the same
        picker.bulk_apply the CLI multi-select uses, in one transaction. A malformed
        request (unknown op / empty selection / bad merge target) is a 400; a DB-level
        write failure is surfaced (not a silent 500), the same guard as /bind."""
        from catalogue.services import picker as P
        if kind not in P.KINDS:
            abort(404)
        body = request.get_json(silent=True) or {}
        op = (body.get("op") or "").strip()
        ids = body.get("ids") or []
        target = body.get("target")
        import sqlite3
        try:
            res = P.bulk_apply(g.db, kind, op, ids, target=target)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except (IntegrityError, SchemaDriftError, WriteError, sqlite3.Error) as e:
            try:
                g.db.rollback()
            except Exception:
                pass
            return jsonify({"error": f"Bulk op did not persist — "
                                     f"{type(e).__name__}: {e}"}), 500
        # 200 + the result dict itself: {op, kind, ok:[…], skipped:[…], failed:[…],
        # target?}. Success is the HTTP status; `ok` here is the list of done rows.
        return jsonify(res)

    # AUTHORITY-LINKED dedup of contributor records: collapse `person` rows that the
    # union-find over authority cross-links proves are the SAME person (the "deep
    # linking" dedup; CLI twin is `python -m catalogue.services.person_dedup`). GET is
    # a dry-run PREVIEW (which rows would fold into which) + an Apply button; POST
    # runs it and returns the same report, now reflecting what was merged.
    @app.get("/picker/person/dedupe")
    def picker_dedupe_preview():
        from catalogue.services import person_dedup as PD
        plan = PD.plan_batch(g.db)                 # offline (no reharvest) → fast
        return render_template("picker_dedupe.html",
                               report=PD.dedup_report(plan), applied=False)

    @app.post("/picker/person/dedupe")
    def picker_dedupe_apply():
        from catalogue.services import person_dedup as PD
        import sqlite3
        # Plan + apply in ONE request; apply_batch re-plans each pair just-in-time and
        # asserts integrity per component. Surface a write failure instead of a silent
        # 500 (same guard as /bind), so a half-applied dedup can never hide.
        try:
            plan = PD.plan_batch(g.db)
            result = PD.apply_batch(g.db, plan, commit=True)
        except (IntegrityError, SchemaDriftError, WriteError, sqlite3.Error) as e:
            try:
                g.db.rollback()
            except Exception:
                pass
            msg = f"Dedup did not persist — {type(e).__name__}: {e}"
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify({"error": msg}), 500
            abort(500, msg)
        report = PD.dedup_report(plan, result)
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(report)
        return render_template("picker_dedupe.html", report=report, applied=True)

    @app.post("/picker/person/<int:pid>/local")
    def picker_confirm_local(pid):
        from catalogue.services.verify import confirm_local
        ok = confirm_local(g.db, pid)
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": ok})
        return redirect(url_for("picker_list", kind="person"))

    @app.post("/picker/person/<int:pid>/unbind")
    def picker_person_unbind(pid):
        """Clear a person's authority binding → back to provisional (re-enters the
        worklist), dropping the harvested cross-links. Safe against a record that
        is already gone (merged away / deleted): returns ok=False and NEVER
        resurrects a row (the UPDATE/DELETE simply match nothing)."""
        acc = _acc(g.db)
        ok = acc.persons.reads.get(pid) is not None
        if ok:
            acc.persons.writes.bind_external(pid, None, "provisional")   # unbind → back to worklist
            acc.persons.writes.clear_external_ids(pid)
            g.db.commit()
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": ok})
        return redirect(url_for("picker_list", kind="person"))

    # Contributor cleanup: GET returns the plan (the confirm preview — "tell the
    # user"), POST applies it and returns the report.
    @app.get("/picker/person/<int:pid>/split")
    def picker_split_plan(pid):
        from catalogue.services import contributor_edit as CE
        return jsonify(CE.plan_split(g.db, pid))

    @app.post("/picker/person/<int:pid>/split")
    def picker_split_apply(pid):
        from catalogue.services import contributor_edit as CE
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(CE.apply_split(g.db, pid, assignments=body.get("assignments"),
                                          record_undo=True))
        except IntegrityError as e:
            return jsonify({"error": f"Split rolled back (would leave the catalogue "
                                     f"inconsistent): {e}"}), 500

    @app.get("/picker/person/<int:pid>/delete")
    def picker_delete_plan(pid):
        from catalogue.services import contributor_edit as CE
        return jsonify(CE.plan_delete(g.db, pid))

    @app.post("/picker/person/<int:pid>/delete")
    def picker_delete_apply(pid):
        from catalogue.services import contributor_edit as CE
        try:
            return jsonify(CE.apply_delete(g.db, pid, record_undo=True))
        except IntegrityError as e:
            return jsonify({"error": f"Delete rolled back (would leave the catalogue "
                                     f"inconsistent): {e}"}), 500

    # Mark a contributor as an ORGANIZATION (translation group/committee) → off the
    # person match worklist, work edges kept. ?undo=1 reverts it to provisional.
    @app.post("/picker/person/<int:pid>/org")
    def picker_mark_org(pid):
        from catalogue.services.names import set_person_kind
        ok = set_person_kind(g.db, pid,
                             organization=request.values.get("undo") is None)
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": ok})
        return redirect(url_for("picker_list", kind="person"))

    # MERGE a duplicate person into a canonical one (?into=<pid>). GET previews,
    # POST applies — the same plan/apply contract as split/delete.
    @app.get("/picker/person/<int:pid>/merge")
    def picker_merge_plan(pid):
        from catalogue.services import contributor_edit as CE
        into = request.args.get("into", type=int)
        if not into:
            abort(400)
        return jsonify(CE.plan_merge(g.db, pid, into))

    @app.post("/picker/person/<int:pid>/merge")
    def picker_merge_apply(pid):
        from catalogue.services import contributor_edit as CE
        body = request.get_json(silent=True) or request.form
        into = body.get("into")
        if not into:
            abort(400)
        # Whether to keep the merged-away record's NAME as an alias of the survivor
        # (the UI checkbox). Default True; only an explicit false/0/off disables it.
        keep = body.get("keep_name_alias", True)
        if isinstance(keep, str):
            keep = keep.lower() not in ("false", "0", "off", "no", "")
        try:
            return jsonify(CE.apply_merge(g.db, pid, int(into), keep_name_alias=bool(keep),
                                          record_undo=True))
        except IntegrityError as e:
            return jsonify({"error": f"Merge rolled back (would leave the catalogue "
                                     f"inconsistent): {e}"}), 500

    # Undo a journaled contributor op (merge / delete / split). Restores the snapshot
    # captured before the op and reports the affected person ids so the UI refreshes
    # exactly those rows. The token is the undo_log id returned by the apply route.
    @app.post("/picker/person/undo")
    def picker_undo():
        from catalogue.services import contributor_undo as U
        body = request.get_json(silent=True) or request.form
        token = body.get("token")
        if not token:
            abort(400)
        try:
            return jsonify(U.apply_undo(g.db, int(token)))
        except IntegrityError as e:
            return jsonify({"error": f"Undo rolled back (would leave the catalogue "
                                     f"inconsistent): {e}"}), 500

    # ADD an alias to an existing person (transliteration / ordinal / variant form,
    # e.g. "4th Panchen Lama"). The work twin is /work/<wid>/alias/add.
    @app.post("/picker/person/<int:pid>/alias")
    def picker_person_add_alias(pid):
        text = (request.form.get("text") or "").strip()
        if not text:
            abort(400)
        if _acc(g.db).persons.reads.get(pid) is None:
            abort(404)
        aid = _add_alias(g.db, "person", pid, text,
                         (request.form.get("scheme") or "english").strip() or "english")
        g.db.commit()
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": True, "alias_id": aid, "text": text})
        return redirect(url_for("picker_list", kind="person"))

    @app.post("/picker/person/<int:pid>/rename")
    def picker_person_rename(pid):
        """Edit a person's display (primary) name. The new spelling is also seeded
        as an alias (if absent) so it stays searchable; the OLD name remains as an
        alias — earlier forms are never lost (e.g. an OCR garble stays matchable)."""
        from catalogue.db_store import fold_key
        name = (request.form.get("primary_name") or "").strip()
        if not name:
            abort(400)
        acc = _acc(g.db)
        if acc.persons.reads.get(pid) is None:
            abort(404)
        acc.journal.update_row("person", {"primary_name": name}, {"id": pid})
        # add_alias does not dedupe → guard so a rename never piles duplicate aliases.
        if not acc.persons.reads.has_alias_key(pid, fold_key(name)):
            _add_alias(g.db, "person", pid, name, "english")
        g.db.commit()
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": True, "name": name})
        return redirect(url_for("picker_list", kind="person"))

    # REMOVE a wrong alias from a person (the twin of /work/<wid>/alias/<aid>/delete).
    # The primary_name's own seed alias is protected (correct the name itself instead).
    @app.post("/picker/person/<int:pid>/alias/<int:aid>/delete")
    def picker_person_delete_alias(pid, aid):
        _acc(g.db).persons.writes.remove_alias(pid, aid)
        g.db.commit()
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": True})
        return redirect(url_for("picker_list", kind="person"))

    # CREATE a new catalogue person from inside the picker flow (no candidate fits).
    @app.post("/picker/person/new")
    def picker_person_new():
        name = (request.form.get("primary_name") or "").strip()
        if not name:
            abort(400)
        new_pid = _acc(g.db).persons.writes.insert_person(
            name, (request.form.get("role_hint") or "").strip() or None)
        _add_alias(g.db, "person", new_pid, name,
                   (request.form.get("scheme") or "english").strip() or "english")
        g.db.commit()
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": True, "id": new_pid, "name": name})
        return redirect(url_for("picker_list", kind="person"))

    # Search existing persons by name/alias — used to pick a MERGE target.
    @app.get("/picker/person/search")
    def picker_person_search():
        from catalogue.access_api import system_conn
        q = (request.args.get("q") or "").strip()
        exclude = request.args.get("exclude", type=int)
        if not q:
            return jsonify({"matches": []})
        rows = system_conn(g.db).persons.reads.search(q, exclude=exclude)
        return jsonify({"matches": [
            {"id": r[0], "name": r[1], "dates": r[2], "external_id": r[3]} for r in rows]})

    @app.get("/picker/person/authority/search")
    def picker_person_authority_search():
        """Live AUTHORITY search for a person by name (BDRC + Wikidata + VIAF) — the person
        twin of /works/authority/search. Returns ranked candidates {id (bdr:P… / wikidata:Q…
        / viaf:…), source, label (name), detail, url} so the add/edit-person form can fill
        the name + hub id (+ dates when the candidate carries them). An id-shaped query
        (bdr:P… / wikidata:Q…) resolves to that exact record instead of a name search."""
        from catalogue.services import picker as P
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"matches": []})
        by_id = P.lookup_by_id(g.db, "person", q)
        cands = by_id if by_id is not None else P.gather(g.db, "person", q)
        return jsonify({"matches": [c.as_dict() for c in cands]})
