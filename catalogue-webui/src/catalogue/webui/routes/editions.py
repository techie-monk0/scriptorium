"""Editions + holdings: view, edit, delete, and edition_work links.

`_edition_card_context` / `_holding_card_context` are shared by the full record
pages and the chrome-less `/card` fragments the book browser injects inline, so
the same editors render wherever a book or copy appears. Deletes detach every
edition→x link EXPLICITLY ([[integrity-and-foreign-keys]]) rather than leaning on
a cascade, and only touch files after the DB commit succeeds.
"""
from __future__ import annotations

import sqlite3

from flask import abort, flash, redirect, render_template, request, url_for, g

from catalogue.services import covers as covers_mod
from catalogue.services import mount as mount_mod
from catalogue.webui.routes._shared import _acc
from catalogue.services import reconcile as reconcile_mod
from catalogue.services.isbn import normalize_isbn


def register(app, ctx):
    # ── Editions: view + edit + edition_work links ──────────────────────
    def _edition_card_context(eid):
        """Gather the data the edition record renders (shared by the full page
        and the /card fragment the book browser injects). Returns None if the
        edition doesn't exist."""
        acc = _acc(g.db)
        e = acc.editions.reads.record_card(eid)
        if not e:
            return None
        contained = acc.editions.reads.contained_works(eid)
        holdings = acc.holdings.reads.edition_card_rows(eid)
        works = acc.works.reads.recent_labels(100)
        people = acc.persons.reads.directory_named(100)
        # Formats this book is available in — the distinct holding_type across
        # all its holdings (e.g. pdf + epub + physical). Drives the "Available as"
        # line so the book shows its list of types (§ holding_type facet).
        formats = acc.holdings.reads.formats(eid)
        # Locator kinds offered per contained work (open vocab — page/chapter/section).
        locator_types = acc.vocab.locator_types()
        # Tradition vocabulary (config-driven, from vocab.json `_tradition`) for the
        # editable tradition field's datalist suggestions.
        traditions = acc.vocab.traditions()
        return dict(e=e, contained=contained, holdings=holdings, works=works,
                    people=people, formats=formats, locator_types=locator_types,
                    traditions=traditions)

    @app.get("/edition/<int:eid>")
    def edition_detail(eid):
        ed_ctx = _edition_card_context(eid)
        if ed_ctx is None:
            abort(404)
        return render_template("edition_detail.html", **ed_ctx)

    @app.get("/edition/<int:eid>/card")
    def edition_card(eid):
        """HTML fragment of the edition record (no page chrome). The book browser
        fetches this into the detail pane so the edition record shows inline
        instead of navigating away."""
        ed_ctx = _edition_card_context(eid)
        if ed_ctx is None:
            abort(404)
        return render_template("_edition_card.html", **ed_ctx)

    @app.post("/edition/<int:eid>/edit")
    def edition_edit(eid):
        fields = ("title", "publisher", "year", "isbn", "language", "notes", "tradition")
        values = [request.form.get(f) or None for f in fields]
        # Canonicalize the ISBN to digits-only so it can be entered with or without
        # dashes/spaces and still match — the matching layer (capture verdict,
        # intake_match Layer 1) compares against `normalize_isbn(query)`, so the
        # STORED value must equal that same canonical form or it never matches.
        isbn_i = fields.index("isbn")
        if values[isbn_i]:
            values[isbn_i] = normalize_isbn(values[isbn_i]) or None
        _acc(g.db).editions.writes.set_columns(eid, dict(zip(fields, values)))
        g.db.commit()
        return redirect(url_for("edition_detail", eid=eid))

    @app.post("/edition/<int:eid>/delete")
    def edition_delete(eid):
        """Move an edition to Trash. The whole UI shares ONE delete behavior
        (`entity_undo.delete_edition`, the same call the bulk + detect-pane deletes use):
        cascade-delete the edition's holdings + every edition→x link, MOVE each holding's
        file (source + archival) into the configured Trash folder, and bust the cached
        cover art. Reversible — the rows restore via the ↩ Undo token; the files wait in
        Trash. `work` rows are deliberately preserved (a Work can live in many editions)."""
        if _acc(g.db).editions.reads.get(eid) is None:
            abort(404)
        from catalogue.services import entity_undo as EU
        EU.delete_edition(g.db, eid,
                          cover_cache=app.config["COVERS_CACHE"],
                          cover_pinned=app.config["COVERS_PINNED"])
        return redirect(url_for("dashboard"))

    @app.post("/holding/<int:hid>/delete")
    def holding_delete(hid):
        """Drop one physical/electronic copy and move its file(s) to Trash (recoverable).
        Edition + work metadata untouched — useful when only this copy is wrong (bad scan,
        moved path). A file shared with another holding is kept, not trashed."""
        acc = _acc(g.db)
        row = acc.holdings.reads.read_target(hid)   # (file_path, archival, edition_id, title)
        if not row:
            abort(404)
        file_path, archival, edition_id = row[0], row[1], row[2]
        acc.journal.clear("holding", "id", [hid])
        g.db.commit()
        # Move this copy's files to Trash — but only the ones no OTHER holding still
        # references (deleting one copy must not strand a file a sibling copy shares).
        for p in (file_path, archival):
            if not p:
                continue
            if acc.holdings.reads.shares_file(p, hid):
                continue
            try:
                mount_mod.move_to_trash(p)  # recoverable, not unlinked
            except OSError:
                pass
        return redirect(url_for("edition_detail", eid=edition_id))

    # ── Holding fields: view + edit (per-copy facts: format, text status,
    #    shelf, OCR score, notes) ──────────────────────────────────────────
    def _holding_card_context(hid):
        """Data for the per-holding fields editor (the /card fragment the book
        browser injects into the detail pane). Returns None if absent."""
        acc = _acc(g.db)
        h = acc.holdings.reads.fields_card(hid)
        if not h:
            return None

        def codes(table):
            return acc.vocab.codes(table)

        # Broken-link flag: the file path is recorded but the file is gone on disk
        # (deleted/renamed). Surfaced as an error banner on the copy card.
        file_path = h[8]
        file_missing = reconcile_mod.file_state(file_path) == "missing"
        return dict(h=h, text_statuses=codes("text_status"),
                    forms=codes("form_type"), holding_types=codes("holding_type"),
                    file_path=file_path, file_missing=file_missing)

    @app.get("/holding/<int:hid>/card")
    def holding_card(hid):
        """HTML fragment of one holding's editable fields (no page chrome).
        Mirrors edition_card — the book browser fetches it into the detail pane
        and re-fetches it after an inline save."""
        h_ctx = _holding_card_context(hid)
        if h_ctx is None:
            abort(404)
        return render_template("_holding_fields.html", **h_ctx)

    @app.post("/holding/<int:hid>/edit")
    def holding_edit(hid):
        """Update one copy's facts. Edition/work metadata untouched (that's the
        edition card). Empty selects clear the column; lookup FKs guard the
        value, so an unknown code can't be stored. `holding_type` options come
        from the open vocabulary (catalogue/vocab.json)."""
        acc = _acc(g.db)
        h = acc.holdings.reads.get(hid)
        if not h:
            abort(404)
        acc.holdings.writes.set_columns(hid, {
            "form": request.form.get("form") or None,
            "text_status": request.form.get("text_status") or None,
            "holding_type": request.form.get("holding_type") or None,
            "shelf_location": (request.form.get("shelf_location") or "").strip() or None,
            "ocr_quality_score": request.form.get("ocr_quality_score", type=float),
            "notes": (request.form.get("notes") or "").strip() or None,
        })
        g.db.commit()
        return redirect(url_for("edition_detail", eid=h.edition_id))

    @app.post("/edition/<int:eid>/work/add")
    def edition_add_work(eid):
        work_id = request.form.get("work_id", type=int)
        translator = request.form.get("translator_id", type=int) or None
        section = (request.form.get("section_locator") or "").strip() or None
        locator_type = request.form.get("locator_type") or None
        note = (request.form.get("note") or "").strip() or None
        # The Work picker offers inline create when nothing matched: a typed title arrives as
        # new_work_title (no work_id). create_work de-dupes onto an existing canonical#/title.
        new_title = (request.form.get("new_work_title") or "").strip()
        if not work_id and new_title:
            from catalogue.services import work_identity
            # The picker's new-work form carries a (required) Subjects field — pass it
            # through so the work is born categorized. Blank → create_work falls back to
            # the Uncategorized placeholder and we warn the operator.
            subjects = [s.strip() for s in (request.form.get("subjects") or "").split(",")
                        if s.strip()]
            work_id = work_identity.create_work(
                g.db, english_title=new_title, subjects=subjects)[0]
            if not subjects:
                flash("New work tagged “Uncategorized” — assign a real subject; it can’t "
                      "be marked reviewed until then.", "warn")
        if not work_id:
            abort(400)
        # Sequence is the link's position in the book; it's auto-assigned (append
        # to the end) rather than entered by hand. Still honored if a caller
        # passes one explicitly (e.g. the promoter / API).
        sequence = request.form.get("sequence", type=int)
        if sequence is None:
            sequence = _acc(g.db).editions.reads.next_work_sequence(eid)
        try:
            _acc(g.db).editions.writes.add_contained(
                eid, work_id, sequence, translator, section, locator_type, note)
            g.db.commit()
        except sqlite3.IntegrityError:
            # Duplicate (edition, work, sequence) — surface a 409, don't 500.
            abort(409)
        return redirect(url_for("edition_detail", eid=eid))

    @app.post("/edition/<int:eid>/work/remove")
    def edition_remove_work(eid):
        work_id = request.form.get("work_id", type=int)
        sequence = request.form.get("sequence", type=int)
        if work_id is None or sequence is None:
            abort(400)
        _acc(g.db).editions.writes.remove_contained(eid, work_id, sequence)
        g.db.commit()
        return redirect(url_for("edition_detail", eid=eid))

    @app.post("/edition/<int:eid>/work/update")
    def edition_update_work(eid):
        """Edit one already-linked contained work in place: its sequence,
        translator, section locator, and per-appearance note. The row is keyed by
        (edition, work, old_sequence) since sequence is part of the PK and may
        itself change. A sequence that collides with another link of the SAME
        work → 409."""
        work_id = request.form.get("work_id", type=int)
        old_seq = request.form.get("old_sequence", type=int)
        if work_id is None or old_seq is None:
            abort(400)
        new_seq = request.form.get("sequence", type=int)
        if new_seq is None:
            new_seq = old_seq
        translator = request.form.get("translator_id", type=int) or None
        section = (request.form.get("section_locator") or "").strip() or None
        locator_type = request.form.get("locator_type") or None
        note = (request.form.get("note") or "").strip() or None
        try:
            rowcount = _acc(g.db).editions.writes.update_contained(
                eid, work_id, old_seq, new_seq, translator, section, locator_type, note)
        except sqlite3.IntegrityError:
            abort(409)
        if rowcount == 0:
            abort(404)
        g.db.commit()
        return redirect(url_for("edition_detail", eid=eid))
