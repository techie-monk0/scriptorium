"""Works-rebuild review + apply (the "Books" worklist).

The live review worklist over the work_detection cache ([[work-detection-is-review-worklist]],
[[works-review-edit-pane]], [[apply-keeps-curated-works]]): the master-detail dry-run
view, in-pane editing of a single-work edition before apply (title / authors /
translators / multi-work toggle / link-or-add works / root↔commentary), the
multi-work segment resolver, per-edition apply + reversible undo, the works-needing-
review list, entity delete/merge during review, and the "merge & link works" tier.
"""
from __future__ import annotations

import json
import os
import re

from flask import (
    abort, flash, jsonify, redirect, render_template, request, url_for, g,
)

from catalogue.db_store import fold_key
from catalogue.db_store import contributor_store as cs
from catalogue.db_store.integrity import IntegrityError
from catalogue.services import library as library_mod
from catalogue.services import reconcile as reconcile_mod
from catalogue.webui.routes._shared import _acc
from catalogue.services.isbn import normalize_isbn


def register(app, ctx):
    # ── Works-rebuild DRY-RUN report (read-only). Master-detail over the
    # work_detection cache (filled by catalogue.cli.work_detect / segment_detect):
    # per edition, what was detected as title / author / translator / canonical#,
    # with file-open + authority links, so you can verify before anything applies. ──
    def _detect_view(kind):
        from catalogue.services import work_undo
        # The Books review tab (kind="single") is the UNION of single- AND multi-work
        # detections, so its count + worklist match the home "Books" badge — every book
        # needing review confirmation, regardless of structure. The dedicated
        # /works/detect/multi page stays filtered to kind='multi'.
        acc = _acc(g.db)
        rows = acc.editions.reads.detections(None if kind == "single" else kind)
        # An edition the operator re-marked multi-work in the single pane leaves this list.
        emeta = {r[0]: (r[1], r[2], r[3]) for r in acc.editions.reads.detect_meta()}
        multi_marked = {e for e, (s, _t, _v) in emeta.items() if s == "multi_work"}
        items = []
        for eid, k, pj in rows:
            is_multi = eid in multi_marked
            p = json.loads(pj)
            f = p.get("file") or {}
            path = f.get("path") or ""
            det = p.get("determination") or k or "?"
            # Confidence is the classical-match score — only meaningful for classical.
            label = (f"classical · conf. {round((p.get('confidence') or 0) * 100)}%"
                     if det == "classical" else det)
            # The heading/row name is the SAVED stored title (edition.title), so editing
            # it in the card is reflected at the top; fall back to the detected title.
            live_title = (emeta.get(eid) or (None, None, None))[1]
            # Volume as a plain NUMBER (0 = no volume); parse a digit out of the stored
            # designation (legacy 'v. 1' → 1) for display in the numeric input.
            _vol = (emeta.get(eid) or (None, None, None))[2]
            _vm = re.search(r"\d+", str(_vol)) if _vol else None
            volume = int(_vm.group()) if _vm else 0
            # A MAIN work carrying a canonical authority id (not a root/commentary relationship
            # work) → apply should warn if the operator hasn't marked the edition classical.
            has_authority_work = acc.editions.reads.has_authority_work(eid)
            disp_title = (live_title or p.get("stored_title")
                          or (p.get("title") or {}).get("english") or f"edition {eid}")
            if volume:                                   # show the volume in the heading + row label
                disp_title = f"{disp_title} · vol. {volume}"
            # Edition holdings (files) — rendered below the works via _edition_extras.html.
            holdings = []
            for hid, form, htype, fp, arch in acc.holdings.reads.display_rows(eid):
                hp = fp or arch or ""
                holdings.append({"id": hid, "label": htype or form or "copy",
                                 "path": hp, "has_file": bool(hp),
                                 "ext": (os.path.splitext(hp)[1].lstrip(".").lower() or "")})
            items.append({
                "id": eid, "kind": k, "det": p, "is_multi": is_multi, "volume": volume,
                "is_classical": det == "classical", "has_authority_work": has_authority_work,
                # Multi-work books (a kind='multi' detection, or a single-detection edition
                # the operator re-marked multi-work) land in the 'Multi-work' group.
                "group": "multi" if (k == "multi" or (kind == "single" and is_multi)) else det,
                "title": disp_title,
                "subtitle": label, "done": bool(p.get("applied")),
                # Review-recency stamp so the pane can keep only the last-N reviewed visible.
                "reviewed_at": p.get("applied_at") or 0,
                "holding_id": f.get("holding_id"), "has_file": bool(path),
                "file_ext": (os.path.splitext(path)[1].lstrip(".").lower() or None),
                "undo_token": work_undo.pending_undo(g.db, eid) if p.get("applied") else None,
                "holdings": holdings,
                # The works linked to this edition — the read-only "Works In This Edition"
                # section (Work Basics + collapsed Work Details, each linking to /work/<id>
                # to edit). Populated for multi-work editions AND classical single-work
                # editions; a modern single needs none (it is edition-only). The surfacing
                # predicate in edition_work_summaries drops degenerate placeholders.
                "works": (library_mod.edition_work_summaries(g.db, eid)
                          if (k == "multi" or det == "classical" or is_multi) else []),
            })
        if kind == "single":
            counts = {c: sum(1 for it in items if it["group"] == c)
                      for c in ("classical", "modern", "multi")}
            order = {"classical": 0, "modern": 1, "multi": 2}
            labels = {"classical": "Classical", "modern": "Modern", "multi": "Multi-work"}
            items.sort(key=lambda it: (order.get(it["group"], 9), (it["title"] or "").lower()))
            for it in items:
                lbl = labels.get(it["group"], it["group"].capitalize())
                it["group_label"] = f"{lbl} — {counts.get(it['group'], 0)} editions"
            grouped = True
        else:
            counts = {"multi": len(items)}
            items.sort(key=lambda it: (it["title"] or "").lower())
            grouped = False
        return render_template("works_detect.html", items=items, counts=counts,
                               mode=kind, grouped=grouped,
                               review_tab=("books" if kind == "single" else None))

    @app.get("/works/detect")
    def works_detect():
        return redirect(url_for("works_detect_single"))

    @app.get("/works/detect/single")
    def works_detect_single():
        return _detect_view("single")

    @app.get("/works/detect/multi")
    def works_detect_multi():
        from catalogue.services.features import feature_enabled
        if not feature_enabled("multi_work_detection"):
            return redirect(url_for("works_detect_single"))
        return _detect_view("multi")

    @app.post("/works/detect/<int:eid>/volume")
    def works_detect_volume(eid):
        """Set the edition's volume NUMBER (0 = no volume → NULL). Stored in edition.volume."""
        body = request.get_json(silent=True) or request.form
        try:
            n = int(body.get("volume") or 0)
        except (TypeError, ValueError):
            n = 0
        n = max(0, n)
        _acc(g.db).editions.writes.set_columns(eid, {"volume": str(n) if n > 0 else None})
        g.db.commit()
        if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
            return jsonify({"eid": eid, "volume": n})
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/determination")
    def works_detect_determination(eid):
        """Flip a single edition's classical/modern classification — the 'classical work'
        checkbox. classical → the edition is (an edition of) a canonical text, so apply keeps
        a Work; unchecked → modern, no Work (author on the edition)."""
        from catalogue.services import work_detect as WD
        body = request.get_json(silent=True) or request.form
        det = WD.get_detection(g.db, eid)
        if not det or det.get("kind") != "single":
            abort(404)
        det["determination"] = "classical" if body.get("classical") else "modern"
        WD.store_detection(g.db, eid, "single", det, commit=True)
        if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
            return jsonify({"eid": eid, "determination": det["determination"]})
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/apply")
    def works_detect_apply(eid):
        """Materialise one verified SINGLE-work detection into canonical rows
        (Part D apply). Destructive — drops the degenerate work. Returns JSON (incl.
        the undo_token) for the keyboard `s` = apply-and-advance flow; redirects for a
        plain form submit."""
        from catalogue.services import works_apply, work_detect as WD
        is_fetch = request.headers.get("X-Requested-With") == "fetch" or request.is_json
        # Guard: a MAIN work carries a canonical authority id but the edition isn't marked
        # classical — a canonical work IS a classical text, so the classification is wrong.
        # Ask before applying (the JS resolves OK by ticking 'classical work' first).
        det = WD.get_detection(g.db, eid)
        if (is_fetch and det and det.get("determination") != "classical"
                and _acc(g.db).editions.reads.has_authority_work(eid)):
            return jsonify({"status": "confirm", "field": "classical", "reason":
                "This edition's work has a canonical authority id (toh / bdrc / …) but "
                "“classical work” is not checked — a work with a canonical id is a classical "
                "text.\n\nOK = mark it “classical work” and apply.\nCancel = review."})
        res = works_apply.apply_single(g.db, eid)
        if is_fetch:
            return jsonify(res)
        return redirect(request.referrer or url_for("works_detect_single"))

    @app.post("/works/detect/<int:eid>/apply-multi")
    def works_detect_apply_multi(eid):
        """Materialise a CHOSEN segmentation method's works for a multi-work edition."""
        from catalogue.services import works_apply
        from catalogue.services.features import feature_enabled
        if not feature_enabled("multi_work_detection"):
            abort(404)
        method = request.form.get("method")
        if method:
            works_apply.apply_multi(g.db, eid, method)
        return redirect(request.referrer or url_for("works_detect_multi"))

    @app.post("/works/detect/<int:eid>/reviewed")
    def works_detect_reviewed(eid):
        """Mark a MULTI-work edition reviewed (value=1) or unmark it (value=0) WITHOUT
        an AI segmentation — the operator has curated its contained works by hand (via
        add-work / work-detach). Flips the detection's `applied` flag so the edition
        leaves the Books backlog (review_backlog_counts) and collapses in the pane. No
        undo journal — it's a pure review flag, just toggle it back. Ungated: re-marked
        multi-work editions exist regardless of the multi_work_detection feature."""
        import time
        from catalogue.services import work_detect as WD
        det = WD.get_detection(g.db, eid)
        row = _acc(g.db).editions.reads.detection(eid)
        if not det or not row:
            abort(404)
        raw = (request.get_json(silent=True) or {}).get("value")
        if raw is None:
            raw = request.values.get("value", "1")
        on = str(raw) not in ("0", "", "false", "False", "None")
        if on:
            det["applied"] = True
            det["applied_at"] = time.time()
            det["applied_method"] = "reviewed (manual)"
            det["reviewed_manual"] = True
        else:
            det["applied"] = False
            for k in ("applied_at", "applied_method", "reviewed_manual"):
                det.pop(k, None)
        WD.store_detection(g.db, eid, row[0], det)
        if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
            return jsonify({"status": "ok", "reviewed": on})
        return redirect(request.referrer or url_for("works_detect_multi"))

    @app.post("/works/detect/undo")
    def works_detect_undo():
        """Reverse a works-apply (single or multi) via the shared undo journal — the
        works twin of /picker/person/undo."""
        from catalogue.services import contributor_undo as U
        body = request.get_json(silent=True) or request.form
        token = body.get("token")
        if not token:
            abort(400)
        try:
            result = U.apply_undo(g.db, int(token))
        except IntegrityError as e:
            result = {"error": f"Undo rolled back (would leave the catalogue "
                               f"inconsistent): {e}"}
        if request.is_json:
            return jsonify(result)
        return redirect(request.referrer or url_for("works_detect_single"))

    # ── In-pane editing of a single-work edition before you apply: stored title,
    # authors/translators (person typeahead), the multi-work checkbox, and the works
    # linked to the edition (link an existing work or add a brand-new one). Reuses the
    # book-browser [data-card-url] host + the shared Typeahead widget + contributor_store.
    def _person_name(pid):
        p = _acc(g.db).persons.reads.get(pid)
        return p.primary_name if p else f"person #{pid}"

    def _work_title(wid):
        # Prefer the English title; fall back to ANY alias (a Tibetan/Sanskrit-only work
        # has no English yet — show its native title rather than 'work #N').
        return _acc(g.db).works.reads.alias_title(wid) or f"work #{wid}"

    def _linked_work_of_type(eid, wt):
        return _acc(g.db).editions.reads.linked_work_of_type(eid, wt)

    def _resolve_work_from_form(f):
        """A submitted work picker carries EITHER an existing work_id OR new-work fields —
        resolve to a work_id (creating + de-duping via create_work when new)."""
        wid = f.get("work_id", type=int)
        if wid:
            return wid
        epick = f.get("edition_id", type=int)        # picked an EDITION → use its work
        if epick:
            ewr = _acc(g.db).works.reads.edition_work_rows(epick)
            return ewr[0][0] if ewr else None
        eng = (f.get("english_title") or "").strip()
        sa = (f.get("sanskrit_title") or "").strip()
        bo = (f.get("tibetan_title") or "").strip()
        csys = (f.get("canonical_system") or "").strip()
        cnum = (f.get("canonical_number") or "").strip()
        # Create from ANY identity — a title in any script OR a canonical id. (A Tibetan-
        # only authority record has no English title; don't force one.)
        if not (eng or sa or bo or (csys and cnum)):
            return None
        from catalogue.services import work_identity
        pids = [int(p) for p in f.getlist("author_pids") if p]
        subs = [s.strip() for s in (f.get("subjects") or "").split(",") if s.strip()]
        wid, created, _mc = work_identity.create_work(
            g.db, english_title=eng or None, sanskrit_title=sa or None,
            tibetan_title=bo or None, canonical_system=csys or None,
            canonical_number=cnum or None, work_type=f.get("work_type"),
            original_language=f.get("original_language"), era=f.get("era"),
            notes=f.get("notes"), author_pids=pids, subjects=subs)
        # A new COMMENTARY work can name the root text it explains (root picker in the
        # add-a-new-work form) → record the commentary_on relation right away.
        if wid and (f.get("work_type") or "").strip() == "commentary":
            root_wid = f.get("root_work_id", type=int)
            if root_wid and root_wid != wid:
                work_identity.relate_commentary(g.db, wid, root_wid)
        # Auto-populate the author(s) from the picked authority: if the operator created a
        # NEW work from an authority id (no person hand-picked), ask THAT authority for the
        # work's authors and link each (resolving its name → a person). Best-effort and
        # gated on INGEST_VERIFY so the test suite never hits the network.
        if wid and created and csys and cnum and not pids and app.config.get("INGEST_VERIFY"):
            _populate_authors_from_authority(wid, csys, cnum, eng or sa or bo)
        return wid

    def _populate_authors_from_authority(wid, system, number, title):
        """Link the authority's author(s) onto a freshly-created work, matching
        CONSERVATIVELY so a spelling variant never mints a duplicate person:

          1. external-id match — the author's authority id (wikidata/bdrc/viaf/dila),
             if already on a catalogue person, links THAT person (spelling-independent).
          2. name fold-key match — an existing person alias matching the author's name
             links them, and (if that person is not yet authority-bound) binds the
             authority id + cross-links + the authority spelling as an alias, so the match
             is spelling-proof next time.
          3. neither — DO NOT create a person. The author's spelling isn't in the
             catalogue (nor any alias); note it on the work so the operator resolves it by
             hand (link/create via the work page). Never raises."""
        fetch = app.config.get("WORK_AUTHORS_LOOKUP")
        if not fetch:
            return
        try:
            from catalogue.db_store import contributor_store as cs, fold_key, nfc
            from catalogue.services.names import split_name_dates
            from catalogue.services import verify as V

            def _by_external_ids(ids):
                for v in [i for i in ids if i]:
                    pid = _acc(g.db).persons.reads.id_by_external_id(v)
                    if pid is not None:
                        return pid
                return None

            unresolved = []
            for a in fetch(system, number, title=title) or []:
                name = (a.get("name") or "").strip()
                if not name:
                    continue
                ext = a.get("external_id")
                extra = a.get("extra_ids") or {}
                pid = _by_external_ids([ext, *extra.values()])           # (1) by identity
                if pid is None:                                          # (2) by name fold-key
                    clean, _d = split_name_dates(nfc(name).strip())
                    pid = _acc(g.db).persons.reads.id_by_alias_key(fold_key(clean))
                    if pid is not None:
                        if ext:        # confident name match → bind the authority identity
                            V.bind_person(g.db, pid, ext, name=name, aliases=[name],
                                          extra_ids=extra, commit=False)
                if pid is None:                                          # (3) unresolved
                    unresolved.append(name)
                    continue
                cs.add_work_author(g.db, wid, pid)
            if unresolved:
                note = ("Authority (%s) author(s) NOT auto-linked — spelling not in the "
                        "catalogue; resolve by hand: %s" % (system, ", ".join(unresolved)))
                cur = (_acc(g.db).works.reads.notes(wid) or "")
                if note not in cur:                                      # idempotent
                    _acc(g.db).works.writes.set_scalars(wid, {"notes": (cur + "\n" + note).strip()})
        except Exception:
            pass

    def _edit_card(eid):
        from catalogue.db_store import contributor_store as cs
        from catalogue.services import work_identity
        e = _acc(g.db).editions.reads.detect_card_fields(eid)
        if not e:
            abort(404)
        names = lambda pids: [{"id": p, "name": _person_name(p)} for p in pids]
        author_pids = cs.edition_author_ids(g.db, eid)
        linked = _acc(g.db).works.reads.linked_with_type(eid)
        detected = []                              # authors on the linked work(s), as quick-add chips
        for w, _wt in linked:
            for p in cs.work_author_ids(g.db, w):
                if p not in detected:
                    detected.append(p)
        def _canon(w):
            wf = _acc(g.db).works.reads.summary_fields(w)
            return f"{wf[3]}:{wf[4]}" if wf and wf[3] and wf[4] else None
        works = [{"id": w, "title": _work_title(w), "work_type": wt, "canonical": _canon(w),
                  "authors": [_person_name(p) for p in cs.work_author_ids(g.db, w)]}
                 for w, wt in linked]
        comm_wid = _linked_work_of_type(eid, "commentary")
        root_wid = (work_identity.commentary_root_id(g.db, comm_wid) if comm_wid else None) \
            or _linked_work_of_type(eid, "root")
        brief = lambda wid: {"id": wid, "title": _work_title(wid)} if wid else None
        # The non-editable detection facts shown alongside the editable fields in the
        # one unified card (native title, canonical#/glosses/links, ISBN, file).
        from catalogue.services import subjects as S, work_detect as WD
        det = WD.get_detection(g.db, eid) or {}
        holdings = []          # the edition's actual files (EPUB / PDF / …) — open in viewer
        for hid, form, htype, fp, arch in _acc(g.db).holdings.reads.display_rows(eid):
            p = fp or arch or ""
            holdings.append({"id": hid, "label": htype or form or "copy",
                             "path": p, "has_file": bool(p),
                             "missing": reconcile_mod.file_state(p) == "missing",
                             "ext": (os.path.splitext(p)[1].lstrip(".").lower() or "")})
        own_subjects = S.subjects_for(g.db, "edition", eid, subject_kind="topic")  # override (if any)
        own_series = S.subjects_for(g.db, "edition", eid, subject_kind="series")   # series memberships
        inherited = []                                             # union of the works' TOPICS
        seen = set()
        for w, _wt in linked:
            for _sid, n in S.subjects_for(g.db, "work", w, subject_kind="topic"):
                if n.casefold() not in seen:
                    seen.add(n.casefold()); inherited.append(n)
        sug = S.suggest_edition_subject(g.db, eid)
        if sug and (sug.casefold() in seen or any(n.casefold() == sug.casefold() for _, n in own_subjects)):
            sug = None
        # Classical single editions edit their work inline via the shared work-card (the
        # pane includes _edition_works.html), so this card drops its Works + Commentary rows
        # for them; a MODERN single edition keeps them (author on the edition, no work-card).
        is_classical = det.get("determination") == "classical"
        # ISBN shown is the LIVE edition value — the cached detection payload (det) lags a
        # Detect/manual edit, which is why an applied ISBN looked like it "didn't fill".
        from catalogue.db_store import fold_key
        from catalogue.services import detect as detect_mod
        edition_isbn = e[4] or det.get("isbn")
        isbn_url = (f"https://books.google.com/books?vid=ISBN{edition_isbn}"
                    if edition_isbn else None)
        # What the filename(s) suggest but the record doesn't have yet — surfaced as
        # quick-add author/translator chips (resolved to an existing person where the name
        # uniquely folds, else the chip opens the picker pre-filled). Detection is filename-
        # only (no disk I/O), so this is cheap to recompute on every card render.
        translator_pids = cs.edition_translator_ids(g.db, eid)
        fdet = detect_mod.merge(detect_mod.detect_paths(
            [h["path"] for h in holdings if h["path"]]))

        def _suggest(cands, current_pids):
            cur = {fold_key(_person_name(p)) for p in current_pids}
            out, seen = [], set()
            for nm in (cands or []):
                k = fold_key(nm)
                if not k or k in cur or k in seen:
                    continue
                seen.add(k)
                out.append({"name": nm, "pid": detect_mod.resolve_person(g.db, nm)})
            return out
        filename_authors = _suggest(getattr(fdet, "authors", None), author_pids)
        filename_translators = _suggest(getattr(fdet, "translators", None), translator_pids)
        return render_template(
            "_detection_edit.html", eid=eid, title=e[1] or "", structure=e[2], det=det,
            notes=e[3], isbn=edition_isbn, isbn_url=isbn_url,
            tradition=e[5], traditions=_acc(g.db).vocab.traditions(),
            is_classical=is_classical, holdings=holdings,
            own_subjects=own_subjects, inherited_subjects=inherited, subject_suggestion=sug,
            own_series=own_series,
            authors=names(author_pids), translators=names(translator_pids),
            # Quick-add chips for EVERY detected author not yet added — never gated on
            # "has the operator added one already", so picking one of several candidate
            # authors keeps the others addable (multi-author works).
            detected=names([p for p in detected if p not in author_pids]),
            filename_authors=filename_authors, filename_translators=filename_translators,
            works=works, is_commentary=bool(comm_wid or root_wid),
            commentary_work=brief(comm_wid), root_work=brief(root_wid),
            modern_commentaries=library_mod.edition_commentaries(g.db, eid),
            cover_pinned=_cover_pinned(eid), cover_v=_cover_version(eid))

    def _cover_pinned(eid):
        from catalogue.services import covers
        return bool(covers.cached_path(app.config["COVERS_PINNED"], f"e{eid}"))

    def _cover_version(eid):
        """A cache-bust token for the <img> so a pinned/reset cover refreshes — the newest
        mtime among the pinned + auto cover files (0 when none yet)."""
        from catalogue.services import covers
        best = 0
        for d, k in ((app.config["COVERS_PINNED"], f"e{eid}"),
                     (app.config["COVERS_CACHE"], f"e{eid}")):
            p = covers.cached_path(d, k)
            if p:
                try:
                    best = max(best, int(os.path.getmtime(p)))
                except OSError:
                    pass
        return best

    @app.get("/works/detect/<int:eid>/edit")
    def works_detect_edit(eid):
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/set-title")
    def works_detect_set_title(eid):
        from catalogue.services import works_apply as WA
        body = request.get_json(silent=True) or request.form
        new = (body.get("title") or "").strip() or None
        # Keep the per-edition placeholder work's name in step with the edition title so a
        # renamed (de-garbled) edition doesn't leave the linked placeholder under the OLD
        # name (and so apply still recognises + drops it). Match-then-rename, so before the UPDATE.
        WA.sync_placeholder_title(g.db, eid, new)
        _acc(g.db).editions.writes.set_columns(eid, {"title": new})
        g.db.commit()
        if request.is_json:
            return jsonify({"ok": True})
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/set-isbn")
    def works_detect_set_isbn(eid):
        """Edit the edition's ISBN in place from the Review edit card (the in-place
        sibling of set-title). Trimmed; blank → NULL. Re-renders the card so the
        Google Books link tracks the new value."""
        body = request.get_json(silent=True) or request.form
        new = normalize_isbn(body.get("isbn") or "") or None
        _acc(g.db).editions.writes.set_columns(eid, {"isbn": new})
        g.db.commit()
        if request.is_json:
            return jsonify({"ok": True})
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/set-notes")
    def works_detect_set_notes(eid):
        """Edit the edition's book-level notes in place from the Review edit card
        (sibling of set-title/set-isbn). Trimmed; blank → NULL."""
        body = request.get_json(silent=True) or request.form
        new = (body.get("notes") or "").strip() or None
        _acc(g.db).editions.writes.set_columns(eid, {"notes": new})
        g.db.commit()
        if request.is_json:
            return jsonify({"ok": True})
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/set-tradition")
    def works_detect_set_tradition(eid):
        """Edit the edition's Buddhist tradition in place from the Review edit card
        (sibling of set-isbn/set-notes). Free text; trimmed, blank → NULL."""
        body = request.get_json(silent=True) or request.form
        new = (body.get("tradition") or "").strip() or None
        _acc(g.db).editions.writes.set_columns(eid, {"tradition": new})
        g.db.commit()
        if request.is_json:
            return jsonify({"ok": True})
        return _edit_card(eid)

    def _isbn_metadata(isbn):
        """Authoritative ISBN→metadata, cached in resolver_cache (so repeat Detects and
        offline runs don't re-hit the network). Uses app.config['ISBN_LOOKUP'] (OpenLibrary;
        tests override it). Returns the OpenLibrary-shaped dict or None."""
        from catalogue.services.work_canonical_resolver import cached_rows
        ol = app.config["ISBN_LOOKUP"]
        def _compute():
            m = ol(isbn)
            return [m] if m else []
        rows = cached_rows(g.db, namespace="isbn_meta", source="openlibrary",
                           query=isbn, version=1, compute=_compute, cache_empty=False)
        return rows[0] if rows else None

    def _compute_detection(eid):
        """The filename → Detection pipeline WITHOUT applying: merge the file-name parse,
        then prefer ISBN-sourced metadata (ISBN from the filename or already on the
        edition). Returns a Detection or None. Shared by the per-card detect and the bulk
        detect so both see identical results."""
        from catalogue.services import detect as detect_mod
        acc = _acc(g.db)
        paths = acc.holdings.reads.detect_paths(eid)
        det = detect_mod.merge(detect_mod.detect_paths(paths))
        ed = acc.editions.reads.get(eid)
        isbn = (det.isbn if det else None) or (ed.isbn if ed else None)
        if isbn:
            det = detect_mod.enrich_with_isbn(det, isbn, lookup=_isbn_metadata)
        return det

    @app.post("/works/detect/bulk-detect")
    def works_detect_bulk():
        """Bulk "Detect from filename" with a SAFETY GUARD: a single bulk edit must not
        manufacture duplicate titles across the selected books (the multi-volume / shared-
        ISBN clobber). Dry-run every selection, and if two would END UP with the same title
        — and at least one is a CHANGE — refuse the whole batch (apply nothing) and report
        the collisions. Library-wide duplicate titles are fine; this only blocks dups the
        bulk op itself would create. No collision → apply all in one transaction."""
        from collections import defaultdict
        from catalogue.services import detect as detect_mod
        from catalogue.db_store import fold_key
        body = request.get_json(silent=True) or {}
        ids = []
        for i in (body.get("ids") or []):
            try:
                ids.append(int(i))
            except (TypeError, ValueError):
                continue
        if not ids:
            return jsonify({"ok": False, "error": "no editions selected"}), 400
        # Dry-run: what title would each book END UP with (overwrite only a raw title)?
        plan = {}
        for eid in ids:
            det = _compute_detection(eid)
            ed = _acc(g.db).editions.reads.get(eid)
            old = ed.title if ed else None
            plan[eid] = {"det": det, "old": old,
                         "pred": detect_mod.predicted_title(old, det)}
        # Collisions: ≥2 selected books sharing a predicted title where ≥1 is a change.
        groups = defaultdict(list)
        for eid, p in plan.items():
            if p["pred"]:
                groups[fold_key(p["pred"])].append(eid)
        collisions = []
        for grp in groups.values():
            if len(grp) > 1 and any(
                    fold_key(plan[e]["pred"] or "") != fold_key(plan[e]["old"] or "")
                    for e in grp):
                collisions.append({"title": plan[grp[0]]["pred"], "ids": sorted(grp)})
        if collisions:
            return jsonify({
                "ok": False, "collisions": collisions,
                "message": "Bulk detect would give these selected books the same title, so "
                           "nothing was changed. Detect them individually, or fix the shared "
                           "ISBN / filenames first."})
        # No collision → apply all, one commit. Classify each outcome so the caller can
        # tell "cleaned" from "already fine" from "no recognizable filename" — otherwise a
        # no-op (curated title preserved / non-Anna's filename) looks like a silent failure.
        applied, changed, suggested, unchanged, undetected, failed = [], [], [], [], [], []
        for eid, p in plan.items():
            if p["det"] is None:                          # filename not recognizable
                undetected.append(eid)
                continue
            try:
                summary = detect_mod.apply_to_edition(g.db, eid, p["det"], commit=False)
                summary.update(id=eid, detected=True, source=p["det"].source)
                applied.append(summary)
                fields = summary.get("applied") or {}
                # Real writes (title/subtitle/publisher/year/isbn) vs. a kept-curated title
                # whose filename merely SUGGESTS a different one (offered, not applied). The
                # two are independent: a book can gain a subtitle AND still keep its title.
                wrote = {k: v for k, v in fields.items() if k != "title_suggestion"}
                if fields.get("title_suggestion"):
                    suggested.append({"id": eid, **fields["title_suggestion"]})
                if wrote:
                    changed.append(eid)
                elif not fields.get("title_suggestion"):
                    unchanged.append(eid)                 # already clean → nothing to do                 # already clean → nothing to do
            except Exception as e:                        # noqa: BLE001 — report, don't 500
                failed.append({"id": eid, "error": f"{type(e).__name__}: {e}"})
        g.db.commit()
        # Each selected edition's CURRENT title, so the client can set every left-pane row
        # directly (deterministic — no page re-fetch, and it also corrects rows left stale
        # by an earlier run, not just the ones changed now).
        _titled = _acc(g.db).editions.reads
        titles = {eid: (lambda e: e.title if e else None)(_titled.get(eid)) for eid in ids}
        return jsonify({"ok": True, "applied": applied, "changed": changed,
                        "suggested": suggested, "unchanged": unchanged,
                        "undetected": undetected, "failed": failed, "titles": titles})

    @app.post("/works/detect/<int:eid>/detect")
    def works_detect_from_filename(eid):
        """Recover edition basics for this book. The filename detectors
        (catalogue.services.detect) yield the ISBN + rough fields; when an ISBN is known
        (from the filename OR already on the edition) we look it up and prefer the
        AUTHORITATIVE title/authors/publisher/year over the filename parse. Then apply:
        clean title/subtitle, fill-if-empty publisher/year, set-or-alias ISBN, link any
        author/translator that uniquely resolves to an existing person (others come back
        `unresolved` for one-click add). JSON summary for the card host, else the card."""
        from catalogue.services import detect as detect_mod
        det = _compute_detection(eid)
        if det is None:
            if request.is_json:
                return jsonify({"ok": True, "detected": False,
                                "message": "Nothing recognizable in the filename, and no "
                                           "ISBN to look up."})
            return _edit_card(eid)
        summary = detect_mod.apply_to_edition(g.db, eid, det)
        summary.update(ok=True, detected=True, source=det.source)
        if request.is_json:
            return jsonify(summary)
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/from-isbn")
    def works_detect_from_isbn(eid):
        """The ISBN counterpart of "Detect from filename": look the edition's stored ISBN up
        in the authorities (OpenLibrary via `_isbn_metadata`) and fill the AUTHORITATIVE
        title / authors / translators (+ publisher/year) from the catalogue record. No
        filename parse — so `enrich_with_isbn(None, …)` skips the filename-compat guard and
        uses the ISBN result directly. JSON summary for the card host, else the card."""
        from catalogue.services import detect as detect_mod
        ed = _acc(g.db).editions.reads.get(eid)
        isbn = ed.isbn if ed else None
        if not isbn:
            if request.is_json:
                return jsonify({"ok": True, "detected": False,
                                "message": "No ISBN on this edition to look up — add one first."})
            return _edit_card(eid)
        det = detect_mod.enrich_with_isbn(None, isbn, lookup=_isbn_metadata)
        if det is None or det.is_empty():
            if request.is_json:
                return jsonify({"ok": True, "detected": False,
                                "message": f"No catalogue metadata found for ISBN {isbn}."})
            return _edit_card(eid)
        summary = detect_mod.apply_to_edition(g.db, eid, det)
        summary.update(ok=True, detected=True, source=det.source)
        if request.is_json:
            return jsonify(summary)
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/<any(author,translator):role>/<any(add,remove):op>")
    def works_detect_contrib(eid, role, op):
        from catalogue.db_store import contributor_store as cs
        pid = request.form.get("pid", type=int)
        # Add a BRAND-NEW person by name (the picker's "➕ add as a new person") — no
        # existing match to pick. Create the person, then link them like any other.
        if not pid and op == "add":
            name = (request.form.get("name") or "").strip()
            if name:
                from catalogue.services.promote import get_or_create_person
                pid, _ = get_or_create_person(g.db, name)
        if pid:
            if role == "author":
                ids = [p for p in cs.edition_author_ids(g.db, eid) if p != pid]
                if op == "add":
                    ids.append(pid)
                cs.set_edition_authors(g.db, eid, ids)
            else:
                ids = [p for p in cs.edition_translator_ids(g.db, eid) if p != pid]
                if op == "add":
                    ids.append(pid)
                cs.set_edition_translators(g.db, eid, ids)
            g.db.commit()
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/structure")
    def works_detect_structure(eid):
        from catalogue.services import edition_structure
        body = request.get_json(silent=True) or request.form
        structure = "multi_work" if body.get("multi") else "single_work"
        edition_structure.set_structure(g.db, eid, structure)
        g.db.commit()
        if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
            return jsonify({"eid": eid, "structure": structure})
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/work/<any(link,unlink):op>")
    def works_detect_work_link(eid, op):
        from catalogue.db_store import contributor_store as cs
        if op == "unlink":
            wid = request.form.get("work_id", type=int)
            if wid:
                cs.unlink_work(g.db, eid, wid)
                g.db.commit()
        else:
            wid = _resolve_work_from_form(request.form)   # existing pick OR new work
            if wid:
                cs.link_work(g.db, eid, wid)
                g.db.commit()
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/work/new")
    def works_detect_work_new(eid):
        """Back-compat alias: add a brand-new work and link it (the picker now posts
        new works straight to /work/link)."""
        from catalogue.db_store import contributor_store as cs
        if not (request.form.get("english_title") or "").strip():
            abort(400)
        wid = _resolve_work_from_form(request.form)
        if wid:
            cs.link_work(g.db, eid, wid)
            g.db.commit()
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/work/set-<any(commentary,root):role>")
    def works_detect_set_relation(eid, role):
        """Set the edition's commentary or root work (pick existing or add new), link it,
        and record the commentary→root relationship once both ends are present."""
        from catalogue.db_store import contributor_store as cs
        from catalogue.services import work_identity
        wid = _resolve_work_from_form(request.form)
        if wid:
            work_identity.set_work_type(g.db, wid, role)
            cs.link_work(g.db, eid, wid)
            other = _linked_work_of_type(eid, "root" if role == "commentary" else "commentary")
            if other:
                comm, root = (wid, other) if role == "commentary" else (other, wid)
                work_identity.relate_commentary(g.db, comm, root)
            g.db.commit()
        return _edit_card(eid)

    @app.post("/works/detect/<int:eid>/work/clear-<any(commentary,root):role>")
    def works_detect_clear_relation(eid, role):
        """Remove the chosen commentary/root work: drop the commentary_on relationship
        and unlink it from the edition (the work row itself is kept)."""
        from catalogue.db_store import contributor_store as cs
        wid = _linked_work_of_type(eid, role)
        if wid:
            _acc(g.db).works.writes.unrelate_commentary(wid, as_root=(role != "commentary"))
            cs.unlink_work(g.db, eid, wid)
            g.db.commit()
        return _edit_card(eid)

    # ── Layer 2: this EDITION is a modern commentary on classical work(s) (edition→work,
    # `edition_commentary_on`). Edition-level (the modern author's commentary belongs to the
    # whole book, not a contained work), so it works for single AND multi-work editions and
    # supersedes the old degenerate-work commentary block. Many-to-many; the target may be a
    # contained work (internal) or one held elsewhere (external). Plain insert/delete (a
    # single trivially-reversible edge — remove reverses add), mirroring work_set_commentary
    # _root. See docs/design/commentary_relationships_model.md. ──
    @app.post("/edition/<int:eid>/modern-commentary/add")
    def edition_modern_commentary_add(eid):
        """Record that this edition is a modern commentary on the picked/new work."""
        wid = _resolve_work_from_form(request.form)
        if wid:
            _acc(g.db).editions.writes.add_modern_commentary(eid, wid)
            g.db.commit()
        return _edit_card(eid)

    @app.post("/edition/<int:eid>/modern-commentary/<int:wid>/remove")
    def edition_modern_commentary_remove(eid, wid):
        """Drop one edition→work modern-commentary edge (the work itself is kept)."""
        _acc(g.db).editions.writes.remove_modern_commentary(eid, wid)
        g.db.commit()
        return _edit_card(eid)

    # ── Multi-work edition: resolve each AI-detected segment by operator choice — create
    # a new work from the detection, or attach an EXISTING work (the same work-search the
    # single-work pane uses). Nothing is created or deduped automatically; one segment at
    # a time. The chosen work_id is recorded back on the segment so the pane shows it as
    # resolved and offers its editable /work/<wid>/card. ──
    def _multi_segment(eid, method, idx):
        """The detection payload + the chosen method's segment list + that one segment —
        or abort 404 if the edition has no multi detection / the index is out of range."""
        from catalogue.services import work_detect as WD
        det = WD.get_detection(g.db, eid)
        works = ((det or {}).get("methods") or {}).get(method or "", {}).get("works") or []
        if det is None or det.get("kind") != "multi" or not (0 <= idx < len(works)):
            abort(404)
        return det, works[idx]

    @app.post("/works/detect/<int:eid>/segment/link")
    def works_detect_segment_link(eid):
        """Attach one detected segment to a work: create-from-detection (from_detection=1,
        using the segment's own title/native/canonical/authors) OR resolve the work picker
        (an existing work, or a typed/authority new one via _resolve_work_from_form). Links
        it to the edition at the segment's sequence and records the work_id on the segment."""
        from catalogue.services.features import feature_enabled
        from catalogue.services import work_detect as WD
        from catalogue.db_store import contributor_store as cs
        if not feature_enabled("multi_work_detection"):
            abort(404)
        try:
            idx = int(request.values.get("idx"))
        except (TypeError, ValueError):
            abort(400)
        method = request.values.get("method")
        det, seg = _multi_segment(eid, method, idx)
        if request.form.get("from_detection"):
            from catalogue.services.promote import get_or_create_person
            from catalogue.services import work_identity
            canon = seg.get("canonical") or {}
            pids = []
            for nm in (seg.get("authors") or []):
                if nm and nm.strip():
                    pid, _ = get_or_create_person(g.db, nm)
                    if pid not in pids:
                        pids.append(pid)
            wid, _c, _m = work_identity.create_work(
                g.db, english_title=(seg.get("title") or None),
                sanskrit_title=(seg.get("title_sanskrit") or None),
                tibetan_title=(seg.get("title_tibetan") or None),
                canonical_system=(canon.get("system") or None),
                canonical_number=(canon.get("number") or None),
                author_pids=pids)
        else:
            wid = _resolve_work_from_form(request.form)
        if wid:
            prev = seg.get("work_id")
            if prev and prev != wid:
                cs.unlink_work(g.db, eid, prev)          # re-pick: drop the old link first
            cs.link_work(g.db, eid, wid, sequence=idx + 1)
            if request.form.get("from_detection"):
                # A new work was minted from this detection — it now appears in "Works In
                # This Edition" above, so retire the detection from the AI-triage list (no
                # lingering "✓ used" entry that a later "Delete all" would sweep up).
                det["methods"][method]["works"].pop(idx)
            else:
                seg["work_id"] = wid                      # existing work: mark seg resolved
            WD.store_detection(g.db, eid, "multi", det, commit=False)
            g.db.commit()
        return redirect(request.referrer or url_for("works_detect_multi"))

    @app.post("/works/detect/<int:eid>/segment/unlink")
    def works_detect_segment_unlink(eid):
        """Detach a segment's chosen work from the edition (the work row itself is kept) so
        the operator can pick a different one; clears the segment's recorded work_id."""
        from catalogue.services.features import feature_enabled
        from catalogue.services import work_detect as WD
        from catalogue.db_store import contributor_store as cs
        if not feature_enabled("multi_work_detection"):
            abort(404)
        try:
            idx = int(request.values.get("idx"))
        except (TypeError, ValueError):
            abort(400)
        method = request.values.get("method")
        det, seg = _multi_segment(eid, method, idx)
        wid = seg.pop("work_id", None)
        if wid:
            cs.unlink_work(g.db, eid, wid)
            WD.store_detection(g.db, eid, "multi", det, commit=False)
            g.db.commit()
        return redirect(request.referrer or url_for("works_detect_multi"))

    @app.post("/works/detect/<int:eid>/segment/delete")
    def works_detect_segment_delete(eid):
        """Drop a spurious AI-detected segment from the proposal (a bad detection): remove it
        from the method's works list. If it had already been resolved to a work, that work is
        also detached from the edition (the work row itself is kept)."""
        from catalogue.services.features import feature_enabled
        from catalogue.services import work_detect as WD
        from catalogue.db_store import contributor_store as cs
        if not feature_enabled("multi_work_detection"):
            abort(404)
        try:
            idx = int(request.values.get("idx"))
        except (TypeError, ValueError):
            abort(400)
        method = request.values.get("method")
        det = WD.get_detection(g.db, eid)
        works = ((det or {}).get("methods") or {}).get(method or "", {}).get("works") or []
        if det is None or det.get("kind") != "multi" or not (0 <= idx < len(works)):
            abort(404)
        wid = works.pop(idx).get("work_id")
        if wid:
            cs.unlink_work(g.db, eid, wid)
        WD.store_detection(g.db, eid, "multi", det, commit=False)
        g.db.commit()
        return redirect(request.referrer or url_for("works_detect_multi"))

    @app.post("/works/detect/<int:eid>/segments/clear")
    def works_detect_segments_clear(eid):
        """Delete ALL AI-detected segments from the proposal at once (every method) — the
        'Delete all' action. This clears only the AI proposal text; works already resolved
        from segments (made-new or linked-existing) are KEPT — they live in 'Works In This
        Edition' and are real catalogue records, not detections to be swept away."""
        from catalogue.services.features import feature_enabled
        from catalogue.services import work_detect as WD
        if not feature_enabled("multi_work_detection"):
            abort(404)
        det = WD.get_detection(g.db, eid)
        if det is None or det.get("kind") != "multi":
            abort(404)
        for m in (det.get("methods") or {}).values():
            m["works"] = []                              # clear the AI proposal, keep the works
        WD.store_detection(g.db, eid, "multi", det, commit=False)
        g.db.commit()
        return redirect(request.referrer or url_for("works_detect_multi"))

    @app.post("/works/detect/<int:eid>/add-work")
    def works_detect_add_work(eid):
        """Link a work to the edition — resolve the picker (an existing work, or a brand-new
        one via _resolve_work_from_form) and attach it. The ONE shared "add / link a work"
        action behind the "Works on this edition" section: used by the multi-work pane (texts
        the segmentation missed) AND the classical single-work pane. Generic link/detach, so
        it is NOT gated on the multi-detection feature; one per submit, reusable any number."""
        from catalogue.db_store import contributor_store as cs
        wid = _resolve_work_from_form(request.form)
        if wid and wid not in _acc(g.db).works.reads.ids_in_edition(eid):
            cs.link_work(g.db, eid, wid)
            g.db.commit()
        return redirect(request.referrer or url_for("works_detect_multi"))

    @app.post("/works/detect/<int:eid>/work-detach")
    def works_detect_work_detach(eid):
        """Detach a work from the edition (the work row itself is kept) — the unlink action in
        the shared "Works on this edition" list (multi-work AND classical single-work panes).
        For a multi-work edition it also clears any detected segment that pointed to the work,
        so the proposal reflects the change (a no-op for a single detection). Generic, ungated."""
        from catalogue.services import work_detect as WD
        from catalogue.db_store import contributor_store as cs
        wid = request.values.get("wid", type=int)
        if not wid:
            abort(400)
        cs.unlink_work(g.db, eid, wid)
        det = WD.get_detection(g.db, eid)
        if det and det.get("kind") == "multi":
            changed = False
            for m in (det.get("methods") or {}).values():
                for seg in (m.get("works") or []):
                    if seg.get("work_id") == wid:
                        seg.pop("work_id", None)
                        changed = True
            if changed:
                WD.store_detection(g.db, eid, "multi", det, commit=False)
        g.db.commit()
        return redirect(request.referrer or url_for("works_detect_multi"))

    @app.post("/works/detect/<int:eid>/work-note")
    def works_detect_work_note(eid):
        """Set the per-appearance note for a work within THIS edition — the inline note
        editor in the shared "Works In This Edition" list (Review pane). Scoped to the
        join, not the work; blank clears it. Keyed by wid like work-detach (a work
        appears in an edition once). Generic, ungated."""
        wid = request.values.get("wid", type=int)
        if not wid:
            abort(400)
        note = (request.form.get("note") or "").strip() or None
        _acc(g.db).works.writes.set_edition_work_note(eid, wid, note)
        g.db.commit()
        return redirect(request.referrer or url_for("works_detect_multi"))

    # ── Works needing review: every work with incomplete data (no subject/author/
    # canonical identity/type) — the safety net for frictionless work creation. Fix on
    # the work page, then ✓ Mark reviewed (work.review_status='ok') to clear it. ──
    @app.get("/works/incomplete")
    def works_incomplete():
        from catalogue.services import work_review as WR
        items = [{"id": w["id"], "title": w["title"], "done": False,
                  "subtitle": "incomplete: " + ", ".join(w["reasons"]) if w["reasons"]
                              else (w["status"] or "needs review"),
                  "detail": WR.review_detail(g.db, w["id"])}
                 for w in WR.incomplete_works(g.db)]
        return render_template("works_incomplete.html", items=items, count=len(items),
                               review_tab="works")

    @app.post("/work/<int:wid>/review")
    def work_review_set(wid):
        from catalogue.services import work_review as WR
        from catalogue.services.subjects import UncategorizedError
        status = (request.values.get("status") or
                  (request.get_json(silent=True) or {}).get("status"))
        is_fetch = request.is_json or request.headers.get("X-Requested-With") == "fetch"
        try:
            WR.set_review(g.db, wid, status or None)
        except UncategorizedError as e:
            # Blocked: still tagged Uncategorized. Surface as a popup, not a 500.
            if is_fetch:
                return jsonify({"ok": False, "id": wid, "error": str(e)}), 400
            flash(str(e), "error")
            return redirect(request.referrer or url_for("works_incomplete"))
        if is_fetch:
            return jsonify({"ok": True, "id": wid, "status": status})
        return redirect(request.referrer or url_for("works_incomplete"))

    # ── Delete / merge an EDITION or a WORK during review (reversible — entity_undo).
    # JSON, mirroring the person picker's delete/merge contract; undo runs through the
    # shared /works/detect/undo (kind-dispatched journal). ──
    def _entity_op(fn, *args, **kwargs):
        from catalogue.services import entity_undo as EU
        try:
            return jsonify(getattr(EU, fn)(g.db, *args, **kwargs))
        except IntegrityError as e:
            return jsonify({"error": f"Rolled back (would leave the catalogue "
                                     f"inconsistent): {e}"}), 500

    @app.post("/works/detect/<int:eid>/delete-edition")
    def works_detect_delete_edition(eid):
        # Same delete behavior as everywhere else: move to Trash (files + rows, reversible).
        return _entity_op("delete_edition", eid,
                          cover_cache=app.config["COVERS_CACHE"],
                          cover_pinned=app.config["COVERS_PINNED"])

    # ── Bulk actions over the ticked review rows (each row = an edition). Both fan out
    # over the selected ids and report per-id failures; mirror the single-record routes
    # so the UX (subjects, reversible delete) is identical, just many at once. ──
    @app.post("/works/detect/bulk-subject")
    def works_detect_bulk_subject():
        """Assign one label (by name) to many editions at once. `subject_kind` selects the
        namespace: 'topic' (default) adds a topical subject and lifts the Uncategorized
        placeholder on each (mirrors /subjects/edition/<id>/add); 'series' adds a
        Series/Collections membership WITHOUT lifting it (a series is not a topic). One
        backend for every bulk-assign kind. Non-destructive; commits once."""
        from catalogue.services import subjects as S
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        subject_kind = (data.get("subject_kind") or "topic").lower()
        if subject_kind not in ("topic", "series"):
            subject_kind = "topic"
        ids = [int(i) for i in (data.get("ids") or [])]
        if not name or not ids:
            abort(400)
        assigned, errors = [], []
        for eid in ids:
            try:
                # add_subject lifts Uncategorized for a topical add and leaves it for a series.
                S.add_subject(g.db, "edition", eid, name, subject_kind=subject_kind)
                assigned.append(eid)
            except Exception as e:                 # one bad id shouldn't sink the batch
                errors.append({"edition_id": eid, "error": str(e)})
        g.db.commit()
        return jsonify({"assigned": assigned, "errors": errors,
                        "name": name, "subject_kind": subject_kind})

    @app.post("/works/detect/bulk-author")
    def works_detect_bulk_author():
        """Add one author (by name) to many editions at once — resolves to an existing
        person on the fold-key (alias-aware, so spelling variants collapse) or creates one,
        then appends them to each edition's authors (mirrors the single /author/add). Same
        {ids, name} contract as bulk-subject so the reusable BulkAssign control drives it.
        Non-destructive; commits once."""
        from catalogue.db_store import contributor_store as cs
        from catalogue.services.promote import get_or_create_person
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        ids = [int(i) for i in (data.get("ids") or [])]
        if not name or not ids:
            abort(400)
        pid, _ = get_or_create_person(g.db, name)
        assigned, errors = [], []
        for eid in ids:
            try:
                cur = [p for p in cs.edition_author_ids(g.db, eid) if p != pid]
                cur.append(pid)
                cs.set_edition_authors(g.db, eid, cur)
                assigned.append(eid)
            except Exception as e:                 # one bad id shouldn't sink the batch
                errors.append({"edition_id": eid, "error": str(e)})
        g.db.commit()
        return jsonify({"assigned": assigned, "errors": errors,
                        "name": name, "person_id": pid})

    @app.post("/works/detect/bulk-delete-editions")
    def works_detect_bulk_delete_editions():
        """Delete many editions at once — each via the shared `delete_edition` (move to
        Trash: holdings + links cascade, files moved to Trash, reversible), so bulk and
        single deletes behave identically. A single DB file snapshot is taken before the
        batch (the operational safety net); each delete is also individually journaled, so
        every removed row keeps its own ↩ Undo."""
        from catalogue.services import entity_undo as EU
        data = request.get_json(silent=True) or {}
        ids = [int(i) for i in (data.get("ids") or [])]
        if not ids:
            abort(400)
        if not app.config.get("DRY_RUN"):          # snapshot once before the bulk delete
            from catalogue.cli import backup as backup_mod
            try:
                backup_mod.backup(app.config["DB_PATH"])
            except SystemExit:
                pass                               # a stale same-second backup shouldn't block
        deleted, errors = [], []
        for eid in ids:
            try:
                deleted.append(EU.delete_edition(
                    g.db, eid,
                    cover_cache=app.config["COVERS_CACHE"],
                    cover_pinned=app.config["COVERS_PINNED"]))
            except IntegrityError as e:
                errors.append({"edition_id": eid, "error": str(e)})
        return jsonify({"deleted": deleted, "errors": errors})

    def _into_arg():
        into = request.values.get("into", type=int)
        if into is None:
            into = (request.get_json(silent=True) or {}).get("into")
        return int(into) if into else None

    @app.post("/works/detect/<int:eid>/merge-edition")
    def works_detect_merge_edition(eid):
        into = _into_arg()
        if not into:
            abort(400)
        return _entity_op("merge_editions", eid, into,
                          cover_cache=app.config["COVERS_CACHE"],
                          cover_pinned=app.config["COVERS_PINNED"])

    @app.post("/work/<int:wid>/delete")
    def work_delete(wid):
        return _entity_op("delete_work", wid)

    # ── Tier 4 — "Merge & link works": the cross-translation / duplicate work
    # identity review home (the works twin of the person picker). Lists the
    # duplicate-work groups work_dedup detects — same canonical#, same title+author,
    # or a title collision across authors (promote's merge_candidate) — and folds a
    # chosen duplicate into the canonical work via the existing work_merge engine. ──
    def _work_group_view(group):
        def member(m):
            pids = m["author_person_ids"]
            names = _acc(g.db).persons.reads.names_by_ids(pids) if pids else []
            return {"work_id": m["work_id"], "title": m["title"],
                    "n_editions": len(m["editions"]), "authors": names}
        return {"label": group.get("canonical") or group.get("fold_key") or "",
                "winner": group["suggested_winner"],
                "members": [member(m) for m in group["members"]]}

    @app.get("/works/merge")
    def works_merge():
        from catalogue.cli import work_dedup as WD
        t2 = WD.tier2_groups(g.db)
        sections = [
            ("Same canonical id", "strong — same Toh/BDRC number",
             [_work_group_view(x) for x in WD.tier1_groups(g.db)]),
            ("Same ISBN", "safe — one physical book entered twice",
             [_work_group_view(x) for x in t2 if x["classification"] == "isbn_safe"]),
            ("Same title & author", "duplicate — confirm, then merge",
             [_work_group_view(x) for x in t2 if x["classification"] == "duplicate"]),
            ("Title collision (different authors)", "homonym — check before merging",
             [_work_group_view(x) for x in WD.title_collision_groups(g.db)]),
        ]
        sections = [(t, h, gs) for (t, h, gs) in sections if gs]
        vol = [_work_group_view(x) for x in t2 if x["classification"] == "volume_set"]
        return render_template("works_merge.html", sections=sections, volume_sets=vol)

    @app.post("/works/merge")
    def works_merge_apply_one():
        from catalogue.services import work_merge as WM
        dup = request.form.get("dup", type=int)
        into = request.form.get("into", type=int)
        if dup and into and dup != into:
            WM.apply_work_merge(g.db, dup, into)   # atomic; commits on success
        return redirect(url_for("works_merge"))
