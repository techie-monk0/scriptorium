"""Works + aliases (§4.1), work merge (FRBR dedup), and work/edition search +
authority resolution.

The Work layer ([[name-denormalization-sync]], [[commentary-needs-known-root]]):
the work record + its `/card` fragment, alias CRUD (re-folding normalized_key and
resyncing the denormalized native-title columns), root↔commentary classification,
authors, reversible work/edition merge, the title/authority typeaheads, and the
single- vs multi-work structure toggle.
"""
from __future__ import annotations

import os
import sqlite3

from flask import (
    abort, flash, jsonify, redirect, render_template, request, url_for, g,
)

from catalogue.db_store import add_alias as _add_alias
from catalogue.db_store import fold_key
from catalogue.db_store import contributor_store as cs
from catalogue.db_store.integrity import IntegrityError
from catalogue.webui.routes import _shared
from catalogue.webui.routes._shared import _acc


def _live_authorities(q: str, *, deadline: float = 4.0) -> list:
    """Live WORK authority search across BDRC + Wikidata, run IN PARALLEL under a shared
    deadline so the box never waits on the sum of their latencies. Each is best-effort
    ([] on failure/timeout). Returns `[{system, number, title, note}]`."""
    import time
    import concurrent.futures as cf
    from catalogue.services import bdrc, wikidata
    out, end = [], time.monotonic() + deadline
    # NB: a `with ThreadPoolExecutor` would block on shutdown(wait=True) until BOTH searches
    # finish — defeating the deadline (the 'search ran 120s' bug). Abandon slow ones instead.
    ex = cf.ThreadPoolExecutor(max_workers=2)
    try:
        futs = [ex.submit(bdrc.live_work_matches, q, limit=6),
                ex.submit(wikidata.live_work_matches, q, limit=4)]
        for fut in futs:
            try:
                rows = fut.result(timeout=max(0.1, end - time.monotonic()))
            except Exception:
                rows = []
            for m in rows:
                note = (", ".join((m.get("titles") or [])[1:4]) if m.get("titles")
                        else m.get("desc")) or None
                sysn = m["system"]
                # Map the title to its real script: BDRC titles are Wylie → Tibetan;
                # Wikidata labels are ~English. Never put a non-English title in english.
                eng = m.get("english")
                sa = m.get("sanskrit")
                bo = m.get("tibetan")
                if sysn == "bdrc":
                    bo = bo or m["title"]
                elif sysn == "wikidata":
                    eng = eng or m["title"]
                out.append({"system": sysn, "number": m["number"], "title": m["title"],
                            "english": eng, "sanskrit": sa, "tibetan": bo, "note": note})
    except Exception:
        pass
    finally:
        ex.shutdown(wait=False)            # return now; let any slow search finish in the background
    return out


def register(app, ctx):
    # ── Works + aliases (§4.1) ──────────────────────────────────────────
    @app.get("/works")
    def works_list():
        # ?q=… filters to works with ANY matching alias (search is over every alias/title,
        # not just the display one). No query → list ALL works (no cap).
        from catalogue.db_store import fold_key
        q = (request.args.get("q") or "").strip()
        acc = _acc(g.db)
        rows = acc.works.reads.list_rows(fold_key(q) if q else None)
        types = acc.vocab.work_types()
        total = acc.works.reads.count()
        return render_template("works.html", rows=rows, types=types, q=q, total=total,
                               genre_opts=acc.vocab.field_values("work", "genre"),
                               tenet_opts=acc.vocab.field_values("work", "tenet_system"))

    @app.post("/works/new")
    def work_new():
        wid = _acc(g.db).works.writes.insert_work({
            "work_type": request.form.get("work_type") or None,
            "original_language": (request.form.get("original_language") or "").strip() or None,
            "notes": (request.form.get("notes") or "").strip() or None,
        })
        # Controlled-vocab scalars (doctrinal tenet + rhetorical genre) — validated against the
        # CategoricalField registry in the store; only set the ones actually chosen.
        vocab_scalars = {k: v for k in ("tenet_system", "genre")
                         if (v := (request.form.get(k) or "").strip() or None)}
        if vocab_scalars:
            _acc(g.db).works.writes.set_scalars(wid, vocab_scalars)
        seed_alias = (request.form.get("seed_alias") or "").strip()
        if seed_alias:
            _add_alias(g.db, "work", wid, seed_alias,
                       request.form.get("scheme") or "other")
        from catalogue.services import subjects as S
        for name in (request.form.get("subjects") or "").split(","):
            if name.strip():
                S.add_subject(g.db, "work", wid, name.strip())
        if S.ensure_categorized(g.db, "work", wid):     # operator left subjects blank
            flash("New work tagged “Uncategorized” — assign a real subject; it can’t be "
                  "marked reviewed until then.", "warn")
        g.db.commit()
        return redirect(url_for("work_detail", wid=wid))

    def _work_card_context(wid):
        """Everything the work-detail view renders (fields, authors, editions +
        ALL their holdings, subjects, aliases). Shared by the full `/work/<wid>`
        page AND the `/work/<wid>/card` fragment that the book-browser injects
        inline, so the SAME rich detail shows wherever a work appears. None if gone."""
        acc = _acc(g.db)
        w = acc.works.reads.card_fields(wid)
        if not w:
            return None
        aliases = acc.works.reads.aliases_full(wid)
        schemes = acc.vocab.alias_schemes()
        # Authors live on the work (work_author); translators are edition-level and
        # shown per-edition below. Each links to its person page.
        contributors = acc.works.reads.author_rows_named(wid)
        # Editions this work appears in, each with its holdings (so the work page
        # can open the actual book file in the viewer, like /holdings does). The
        # edition_work.translator_person_id is the per-edition translator.
        editions = []
        for eid, etitle, seq, locator in acc.works.reads.editions_of(wid):
            # Effective translator for this work in this edition (override → the
            # edition's translator set).
            tpid = cs.work_translator(g.db, eid, wid)
            tp = acc.persons.reads.get(tpid) if tpid is not None else None
            tname = tp.primary_name if tp else ""
            holdings = acc.holdings.reads.display_rows(eid)
            hs = []
            for hid, form, htype, fp, arch in holdings:
                ext = os.path.splitext(fp or arch or "")[1].lstrip(".").lower() or None
                hs.append({"id": hid, "form": form, "holding_type": htype,
                           "has_file": bool(fp or arch), "ext": ext})
            editions.append({
                "id": eid, "title": etitle, "sequence": seq,
                "translator_id": tpid, "translator_name": tname,
                "locator": locator, "holdings": hs,
            })
        from catalogue.services import subjects as S, work_identity
        # The display/primary title is the lowest-id alias (every read does ORDER BY id LIMIT 1).
        primary_alias_id = aliases[0][0] if aliases else None
        # Human title for the card heading (shown wherever the work renders, page or
        # inline) — the primary alias, never 'Work #N' unless the work is truly untitled.
        title = next((a[1] for a in aliases if a[0] == primary_alias_id), None) \
            or f"Work #{wid}"
        # Root/commentary: the root this work comments ON (if any), and the commentaries
        # that point AT this work (if it's a root). Per-work, so an edition can hold many
        # root↔commentary pairs. Editable via the card's "Root / commentary" section.
        root_id = work_identity.commentary_root_id(g.db, wid)
        commentary_root = {"id": root_id, "title": _shared.work_title(root_id)} if root_id else None
        commentaries = [{"id": cid, "title": _shared.work_title(cid)}
                        for cid in acc.works.reads.commentaries_of(wid)]
        return dict(w=w, aliases=aliases, schemes=schemes, title=title,
                    primary_alias_id=primary_alias_id,
                    contributors=contributors, editions=editions,
                    commentary_root=commentary_root, commentaries=commentaries,
                    subjects=S.subjects_for(g.db, "work", wid),
                    traditions=acc.vocab.traditions(),
                    genre_opts=acc.vocab.field_values("work", "genre"),
                    tenet_opts=acc.vocab.field_values("work", "tenet_system"))

    @app.get("/work/<int:wid>")
    def work_detail(wid):
        work_ctx = _work_card_context(wid)
        if work_ctx is None:
            abort(404)
        return render_template("work_detail.html", **work_ctx)

    @app.get("/work/<int:wid>/card")
    def work_card(wid):
        """The work's full editable detail as a chrome-less fragment the book browser
        injects inline (Review→Works pane, etc.) so a work never forces a page jump."""
        work_ctx = _work_card_context(wid)
        if work_ctx is None:
            abort(404)
        return render_template("_work_card.html", **work_ctx)

    @app.get("/work/<int:wid>/summary")
    def work_summary_card(wid):
        """Read-only work summary fragment for the Search page (/library) detail pane —
        basics + native titles + subjects, with a link to the Review page to edit. The
        read-only twin of /work/<id>/card."""
        from catalogue.services import library as library_mod
        w = library_mod.work_summary(g.db, wid)
        if w is None:
            abort(404)
        return render_template("_work_summary.html", w=w)

    @app.post("/work/<int:wid>/edit")
    def work_edit(wid):
        # work_type is NOT edited here — the work card sets it via the Root text / Commentary
        # checkboxes (/work/<wid>/set-type), so this Save never clobbers it. genre + tenet_system
        # are controlled-vocab selects, validated against the CategoricalField registry in the store.
        fields = ("original_language", "era",
                  "canonical_system", "canonical_number", "notes", "tradition",
                  "genre", "tenet_system")
        _acc(g.db).works.writes.set_scalars(
            wid, {f: (request.form.get(f) or None) for f in fields})
        g.db.commit()
        return redirect(url_for("work_detail", wid=wid))

    @app.post("/work/<int:wid>/set-type")
    def work_set_type(wid):
        """Set a work's root/commentary classification from the work card's two checkboxes:
        work_type = 'root' | 'commentary' | None (neither). They're mutually exclusive. If the
        work is no longer a commentary, its commentary→root link is dropped too."""
        from catalogue.services import work_identity
        wt = (request.form.get("work_type") or "").strip() or None
        if wt not in (None, "root", "commentary"):
            wt = None
        work_identity.set_work_type(g.db, wid, wt)
        if wt != "commentary":            # dropped 'commentary' → no root link applies
            _acc(g.db).works.writes.unrelate_commentary(wid)
        g.db.commit()
        return redirect(url_for("work_detail", wid=wid))

    @app.post("/work/<int:wid>/commentary-of")
    def work_set_commentary_root(wid):
        """Record this work as a commentary on the chosen ROOT work (FRBR commentary_on):
        set its type to 'commentary' and store the relationship. The root is an existing
        work picked by id. Per-work (not edition-bound), so one edition can hold any number
        of root↔commentary pairs — each commentary points to its own root. Shown on the
        work card, so it works inline in the review panes AND on the /work page."""
        from catalogue.services import work_identity
        root_wid = request.form.get("work_id", type=int)
        if root_wid and root_wid != wid:
            work_identity.set_work_type(g.db, wid, "commentary")
            work_identity.relate_commentary(g.db, wid, root_wid)
            g.db.commit()
        return redirect(url_for("work_detail", wid=wid))

    @app.post("/work/<int:wid>/commentary-of/clear")
    def work_clear_commentary_root(wid):
        """Remove this work's commentary→root link (both works are kept; the type is left
        as-is — change it in the Fields section if it's no longer a commentary)."""
        _acc(g.db).works.writes.unrelate_commentary(wid)
        g.db.commit()
        return redirect(url_for("work_detail", wid=wid))

    @app.post("/work/<int:wid>/alias/add")
    def work_add_alias(wid):
        text = (request.form.get("text") or "").strip()
        if not text:
            abort(400)
        from catalogue.services import work_identity
        scheme = request.form.get("scheme") or "other"
        try:
            _add_alias(g.db, "work", wid, text, scheme)
            # Keep the denormalized native-title columns (read by search / work-review) in
            # step with the aliases — adding a wylie/iast alias IS how you set the title.
            work_identity.resync_native_titles(g.db, wid)
            g.db.commit()
        except sqlite3.IntegrityError:
            abort(400)  # unknown scheme code, etc.
        return redirect(url_for("work_detail", wid=wid))

    @app.post("/work/<int:wid>/alias/<int:aid>/delete")
    def work_delete_alias(wid, aid):
        from catalogue.services import work_identity
        _acc(g.db).works.writes.delete_alias(aid)
        work_identity.resync_native_titles(g.db, wid)   # clear the column if its alias is gone
        g.db.commit()
        return redirect(url_for("work_detail", wid=wid))

    @app.post("/work/<int:wid>/alias/<int:aid>/rename")
    def work_rename_alias(wid, aid):
        """Edit an alias's text in place. normalized_key is re-folded (the §4.2 invariant);
        resync keeps the work's denormalized native-title columns in step."""
        from catalogue.services import work_identity
        text = (request.form.get("text") or "").strip()
        if not text:
            abort(400)
        if not _acc(g.db).works.writes.rename_alias_checked(aid, wid, text):
            abort(404)
        work_identity.resync_native_titles(g.db, wid)
        g.db.commit()
        return redirect(url_for("work_detail", wid=wid))

    @app.post("/work/<int:wid>/alias/<int:aid>/primary")
    def work_alias_primary(wid, aid):
        """Make an alias the work's PRIMARY (display) title. The display title is the
        lowest-id alias (every read does `ORDER BY id LIMIT 1`), so rather than migrate a
        flag we move the chosen alias's content into the lowest-id row — ids stay stable
        (nothing references an alias id) and the chosen text becomes the title everywhere."""
        from catalogue.services import work_identity
        acc = _acc(g.db)
        rows = acc.works.reads.aliases_full(wid)
        if aid not in [r[0] for r in rows]:
            abort(404)
        top = rows[0]
        if top[0] != aid:                       # already primary → nothing to do
            chosen = next(r for r in rows if r[0] == aid)
            acc.works.writes.set_alias_fields(top[0], chosen[1], chosen[2])
            acc.works.writes.set_alias_fields(aid, top[1], top[2])
            work_identity.resync_native_titles(g.db, wid)
            g.db.commit()
        return redirect(url_for("work_detail", wid=wid))

    _WORK_AUTHOR_ROLES = ("author", "attributed", "compiler", "reviser")

    @app.post("/work/<int:wid>/author/add")
    def work_add_author(wid):
        """Link a person as an author of this work (work_author). The person is chosen from
        the /picker/person/search typeahead on the work page. If it's a NEW author (no match),
        the picker sends new_author_name: we create the person, link them, and redirect to
        their (editable) page so the operator can fill in dates / aliases / authority id."""
        role = request.form.get("role") or "author"
        if role not in _WORK_AUTHOR_ROLES:
            abort(400)
        pid = request.form.get("pid", type=int)
        new_name = (request.form.get("new_author_name") or "").strip()
        if not pid and new_name:
            pid = _acc(g.db).persons.writes.insert_person(new_name)
            _add_alias(g.db, "person", pid, new_name, "english")
            cs.add_work_author(g.db, wid, pid, role)
            g.db.commit()
            return redirect(url_for("person_detail", pid=pid, from_work=wid))   # → edit the new author
        if not pid:
            abort(400)
        if _acc(g.db).persons.reads.get(pid) is None:
            abort(404)
        cs.add_work_author(g.db, wid, pid, role)
        g.db.commit()
        return redirect(url_for("work_detail", wid=wid))

    @app.post("/work/<int:wid>/author/remove")
    def work_remove_author(wid):
        pid = request.form.get("pid", type=int)
        role = request.form.get("role") or "author"
        _acc(g.db).works.writes.remove_author(wid, pid, role)
        g.db.commit()
        return redirect(url_for("work_detail", wid=wid))

    # ── Work merge (FRBR dedup) — the twin of /picker/person/<pid>/merge. Folds a
    #    duplicate work into a canonical one via work_merge.apply_work_merge. ──────
    @app.get("/work/<int:wid>/merge/candidates")
    def work_merge_candidates(wid):
        """Works this one could be folded into: any work sharing a fold-key with one
        of this work's alias keys (the same exact-title signal the dedup pass uses),
        excluding itself. Each candidate carries its title + author names so the
        operator can judge before previewing."""
        acc = _acc(g.db)
        keys = acc.works.reads.alias_keys(wid)
        if not keys:
            return jsonify([])
        cand_ids = acc.works.reads.ids_by_alias_keys(keys, wid)
        out = []
        for cid in cand_ids:
            title = acc.works.reads.representative_title(cid)
            authors = [nm for _pid, _role, nm in acc.works.reads.author_rows_named(cid)]
            out.append({"work_id": cid, "title": title or f"work#{cid}",
                        "authors": authors})
        return jsonify(out)

    @app.get("/work/<int:wid>/merge")
    def work_merge_plan(wid):
        from catalogue.services import work_merge as WM
        into = request.args.get("into", type=int)
        if not into:
            abort(400)
        return jsonify(WM.plan_merge(g.db, wid, into))

    @app.post("/work/<int:wid>/merge")
    def work_merge_apply(wid):
        from catalogue.services import entity_undo as EU
        body = request.get_json(silent=True) or request.form
        into = body.get("into")
        if not into:
            abort(400)
        try:                                   # reversible (entity_undo journals it)
            return jsonify(EU.merge_works(g.db, wid, int(into)))
        except IntegrityError as e:
            return jsonify({"error": f"Merge rolled back (would leave the catalogue "
                                     f"inconsistent): {e}"}), 500

    @app.post("/edition/<int:eid>/merge")
    def edition_merge_apply(eid):
        """Fold this edition into another (re-points holdings/works/contributors/
        subjects, then deletes it). Reversible via entity_undo. The edition twin of
        /work/<id>/merge + /picker/person/<id>/merge."""
        from catalogue.services import entity_undo as EU
        body = request.get_json(silent=True) or request.form
        into = body.get("into")
        if not into:
            abort(400)
        try:
            return jsonify(EU.merge_editions(g.db, eid, int(into)))
        except IntegrityError as e:
            return jsonify({"error": f"Merge rolled back (would leave the catalogue "
                                     f"inconsistent): {e}"}), 500

    @app.get("/works/search")
    def works_search():
        """Local catalogue works matching a title fragment — the works twin of
        /picker/person/search, feeding the 'attach an existing work' typeahead on
        the review pane. Returns each work's authors (display only; the attach is
        by id, so it never disturbs the work's contributors)."""
        from catalogue.db_store import fold_key
        from catalogue.services.work_canonical_resolver import parse_lang_prefix
        lang, q = parse_lang_prefix(request.args.get("q") or "")   # e.g. 'tib:' / 'skt:' scope
        exclude_eid = request.args.get("exclude_edition", type=int)
        if not q:
            return jsonify({"matches": []})
        acc = _acc(g.db)
        attached_set = set(acc.works.reads.ids_in_edition(exclude_eid)) if exclude_eid else set()
        works = []
        for wid, csys, cnum, title in acc.works.reads.search_hits(fold_key(q), limit=20):
            authors = [nm for _pid, _role, nm in acc.works.reads.author_rows_named(wid)]
            works.append({"kind": "work", "work_id": wid,
                          "canonical_system": csys, "canonical_number": cnum,
                          "title": title or f"work #{wid}",
                          "authors": authors, "attached": wid in attached_set})
        # Works carrying an authority id (Toh/BDRC/…) first; the picker appends that id at the
        # end of the row. Stable sort keeps id order within each group.
        works.sort(key=lambda m: 0 if m["canonical_number"] else 1)
        # ?editions=1 → also offer EDITIONS by title (prefixed 'Edition:' in the picker)
        # so you can find a text via the book it's in; picking one uses the edition's work.
        editions = []
        if request.args.get("editions"):
            # Same diacritic-/digraph-insensitive fold as the rest of search — BOTH sides
            # folded in Python (the edition title has no stored fold column). A previous
            # LOWER()-only column compare here silently missed diacritic/aspirate titles.
            from catalogue.services import search as SEARCH
            ids = [i for i in SEARCH._editions_by_book_title(g.db, fold_key(q))
                   if i != exclude_eid]
            if ids:
                for e, t, _isbn, _yr in _acc(g.db).editions.reads.titled_by_ids(ids):
                    editions.append({"kind": "edition", "edition_id": e, "title": t or f"edition #{e}"})
        # ?authority=1 → 84000/Toh authority matches (by English / Sanskrit / Tibetan
        # title). Listed AFTER the already-saved works/editions (operators usually want
        # the existing record); picking one creates the work from its canonical# + titles.
        authority = []
        if request.args.get("authority"):
            from catalogue.services.work_canonical_resolver import shared_84000_index
            idx = shared_84000_index()           # cached: parsed once, not per keystroke
            if idx.available():
                for m in idx.search(q, limit=10, lang=lang):
                    authority.append({"authority": True, "system": "toh", "number": str(m["toh"]),
                                      "title": m.get("english") or f"Toh {m['toh']}",
                                      "english": m.get("english"), "sanskrit": m.get("sanskrit"),
                                      "tibetan": m.get("tibetan")})
            # ?live=1 → also LIVE authority searches (BDRC + Wikidata, in parallel,
            # best-effort under a deadline). Offline 84000 first, then the live hits.
            if request.args.get("live"):
                for m in _live_authorities(q):
                    authority.append({"authority": True, "system": m["system"],
                                      "number": m["number"], "title": m["title"],
                                      "english": m["english"], "sanskrit": m["sanskrit"],
                                      "tibetan": m["tibetan"]})
        # Order: already-saved DB matches first (local works → editions), THEN the
        # authority candidates (84000 / BDRC / Wikidata). The picker prefixes the saved
        # ones "Saved Work/Edition:" so an existing record is the obvious first choice.
        return jsonify({"matches": works + editions + authority})

    @app.get("/editions/search")
    def editions_search():
        """Editions matching a title/ISBN fragment — the merge-target typeahead for the
        works review pane AND the Browse book-title box.

        Title is matched on the shared diacritic-/digraph-insensitive fold, with BOTH
        sides folded the same way (`search._editions_by_book_title` folds the column in
        Python — the edition title has no stored fold column). A previous version folded
        only the needle and compared it to a merely lower-cased column, so a query like
        "Buddha" (folds to "budda") never matched a title containing "Buddha"/"Buddhā".
        """
        from catalogue.db_store import fold_key
        from catalogue.services import search as SEARCH
        q = (request.args.get("q") or "").strip()
        exclude = request.args.get("exclude", type=int)
        if not q:
            return jsonify({"matches": []})
        ids = set(SEARCH._editions_by_book_title(g.db, fold_key(q)))
        # Edition-number jump: "#N" / a bare integer surfaces that edition directly
        # (parity with the old unified /find box), so the Browse book-title box can be
        # used to look an edition up by its catalogue number.
        acc = _acc(g.db)
        eid_q = SEARCH._as_id(q)
        if eid_q is not None and acc.editions.reads.get(eid_q) is not None:
            ids.add(eid_q)
        else:
            eid_q = None   # not an exact edition-number hit; nothing to pin first
        # ISBN fragment (raw digits) — independent of the title fold.
        digits = "".join(ch for ch in q if ch.isdigit())
        if digits:
            ids |= acc.editions.reads.ids_by_isbn_like(digits)
        if exclude is not None:
            ids.discard(exclude)
        if not ids:
            return jsonify({"matches": []})
        # An exact "#N" hit is pinned to the top so the operator's explicit
        # edition-number jump is the first match, not buried among title hits.
        rows = acc.editions.reads.titled_by_ids(ids, eid_q)
        out = [{"edition_id": e, "title": t or f"edition #{e}", "isbn": isbn, "year": yr}
               for (e, t, isbn, yr) in rows]
        return jsonify({"matches": out})

    @app.get("/works/authority/search")
    def works_authority_search():
        """Search the 84000/Toh authority index by title in any script (English /
        Sanskrit IAST / Tibetan Wylie) so the operator can fill a work's canonical# by
        picking, instead of looking it up by hand. The works twin of the person
        authority-candidate search."""
        from catalogue.services.work_canonical_resolver import (
            shared_84000_index, parse_lang_prefix)
        lang, q = parse_lang_prefix(request.args.get("q") or "")
        if not q:
            return jsonify({"matches": []})
        idx = shared_84000_index()               # cached: parsed once, not per keystroke
        matches = idx.search(q, lang=lang) if idx.available() else []
        out = [{"system": "toh", "number": str(m["toh"]), "toh": m["toh"], "english": m.get("english"),
                "sanskrit": m.get("sanskrit"), "tibetan": m.get("tibetan")} for m in matches]
        if request.args.get("live"):           # live BDRC + Wikidata alongside offline 84000
            out += [{"system": m["system"], "number": m["number"], "english": m["english"],
                     "sanskrit": m["sanskrit"], "tibetan": m["tibetan"]} for m in _live_authorities(q)]
        return jsonify({"matches": out, "unavailable": not idx.available()})

    @app.get("/works/authority/resolve")
    def works_authority_resolve():
        """Resolve a PASTED authority id → the work's titles, so an id alone creates a
        work: `bdr:WA…` (live BDRC), `Toh 3824` / `toh:3824` / a bare number (84000),
        `wd:Q…` / `Q…` (live Wikidata). Returns {system, number, english, sanskrit,
        tibetan} or {error}."""
        import re
        raw = (request.args.get("id") or "").strip()
        if not raw:
            return jsonify({"error": "no id"})
        low = raw.lower()
        if low.startswith("bdr:") or re.match(r"^w[a-z0-9]+$", low):
            from catalogue.services import bdrc
            m = bdrc.work_by_id(raw)
            if m:                              # titles go to their own script field, never english
                return jsonify({"system": "bdrc", "number": m["number"],
                                "english": m.get("english"), "sanskrit": m.get("sanskrit"),
                                "tibetan": m.get("tibetan")})
            return jsonify({"error": bdrc.describe_unresolved(raw)})   # say WHY it failed
        elif low.startswith("wd:") or re.match(r"^q\d+$", low):
            from catalogue.services.wikidata import WikidataClient, labels_and_aliases
            try:
                ent = WikidataClient().entity(raw.split(":", 1)[-1].upper())
                if ent:
                    name, _a = labels_and_aliases(ent)
                    return jsonify({"system": "wikidata", "number": raw.split(":", 1)[-1].upper(),
                                    "english": name, "sanskrit": None, "tibetan": None})
            except Exception:
                pass
        else:
            num = re.sub(r"^toh[:\s]*", "", low).strip()
            if num:
                from catalogue.services.work_canonical_resolver import shared_84000_index
                e = shared_84000_index().by_toh(num)
                if e:
                    return jsonify({"system": "toh", "number": str(e["toh"]),
                                    "english": e.get("english"), "sanskrit": e.get("sanskrit"),
                                    "tibetan": e.get("tibetan")})
        return jsonify({"error": f"could not resolve {raw!r}"})

    # ── Mark editions single- vs multi-work (drives which detection runs). A
    # checkbox over all editions: ticked = multi_work (gets segmentation), unticked
    # = single_work (gets single-text autodetect). Persists edition.structure. ──
    @app.get("/editions/structure")
    def editions_structure():
        from catalogue.services import edition_structure as ES
        return render_template("editions_structure.html", editions=ES.list_editions(g.db))

    @app.post("/editions/structure")
    def editions_structure_save():
        from catalogue.services import edition_structure as ES
        checked = {int(x) for x in request.form.getlist("multi")}
        for e in ES.list_editions(g.db):
            ES.set_structure(g.db, e["id"], "multi_work" if e["id"] in checked else "single_work")
        g.db.commit()
        return redirect(url_for("editions_structure"))

    @app.post("/editions/structure/seed")
    def editions_structure_seed():
        from catalogue.services import edition_structure as ES
        ES.seed_from_proposals(g.db, only_unset=True)
        g.db.commit()
        return redirect(url_for("editions_structure"))
