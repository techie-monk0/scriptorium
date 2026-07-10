"""People (authors/translators): list, detail, edit, aliases.

The full person detail (`_person_card_context`) is shared by the `/person/<pid>`
page and the chrome-less `/person/<pid>/card` fragment the book browser injects
inline, so the same detail renders wherever a person appears. Authority ids
click through via [[name-denormalization-sync]]'s `authority_url`.
"""
from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for, g

from catalogue.access_api import system_access
from catalogue.contracts import NotFound, Ref
from catalogue.db_store import add_alias as _add_alias
from catalogue.db_store import contributor_store as cs
from catalogue.webui.routes import _shared
from catalogue.webui.routes._shared import _acc


def register(app, ctx):
    # ── People (minimal — translators referenced by edition_work) ────────
    @app.get("/people")
    def people_list():
        # ?q=… filters to persons with ANY matching alias (search is over every alias, not
        # just the primary name). No query → list ALL persons. Routed through the access-API
        # person directory; DTOs are mapped back to the row tuple the template already renders.
        q = (request.args.get("q") or "").strip()
        with system_access(app.config["DB_PATH"]) as acc:
            persons = acc.persons.reads.directory(q or None)
            rows = [(p.id, p.primary_name, p.role_hint, p.verification_status) for p in persons]
            total = acc.persons.reads.count()
            tenet_opts = acc.vocab.field_values("person", "tenet_system")
        return render_template("people.html", rows=rows, q=q, total=total, tenet_opts=tenet_opts)

    def _person_card_context(pid):
        """Everything the person-detail view renders (fields, authority identities,
        aliases, the works they contributed to). Shared by the full `/person/<pid>`
        page AND the `/person/<pid>/card` fragment the book browser injects inline,
        so the SAME person detail shows wherever a person appears. None if gone."""
        # The whole person detail now reads through the access-API: fields / aliases /
        # external-ids (person aggregate) + the cross-entity works the person contributed to
        # and editions they appear in. Only the alias_schemes vocab stays on g.db.
        with system_access(app.config["DB_PATH"]) as acc:
            dto = acc.persons.reads.get(pid)
            if dto is None:                       # absent or tombstoned → 404 upstream
                return None
            p = (dto.id, dto.primary_name, dto.role_hint, dto.dates, dto.external_id,
                 dto.verification_status, dto.notes, dto.tradition, dto.tenet_system)
            aliases = acc.persons.reads.aliases(pid)
            ext = acc.persons.reads.external_ids(pid)
            works = acc.persons.reads.contributed_works(pid)
            editions = acc.persons.reads.appearing_editions(pid)
            traditions = acc.vocab.traditions()
            tenet_opts = acc.vocab.field_values("person", "tenet_system")
        alias_schemes = acc.vocab.alias_schemes()
        authority_ids = [
            {"scheme": s, "value": v, "url": _shared.authority_url(v)} for s, v in ext
        ]
        return dict(p=p, aliases=aliases, alias_schemes=alias_schemes,
                    authority_ids=authority_ids, hub_url=_shared.authority_url(p[4]),
                    works=works, editions=editions, traditions=traditions,
                    tenet_opts=tenet_opts)

    @app.get("/person/<int:pid>")
    def person_detail(pid):
        person_ctx = _person_card_context(pid)
        if person_ctx is None:
            abort(404)
        return render_template("person_detail.html",
                               from_work=request.args.get("from_work", type=int), **person_ctx)

    @app.get("/person/<int:pid>/card")
    def person_card(pid):
        """The person's full editable detail as a chrome-less fragment the book browser
        injects inline (Browse person-pick, person review) — no page jump."""
        person_ctx = _person_card_context(pid)
        if person_ctx is None:
            abort(404)
        return render_template("_person_card.html", from_work=None, **person_ctx)

    @app.post("/person/<int:pid>/edit")
    def person_edit(pid):
        """Edit an author's fields from the person page (the twin of /work/<wid>/edit).
        The field update routes through the access-API (integrity gate + audit trail + rev);
        the new spelling is still seeded as an alias so the person stays searchable."""
        name = (request.form.get("primary_name") or "").strip()
        if not name:
            abort(400)
        values = {
            "primary_name": name,
            "role_hint": (request.form.get("role_hint") or "").strip() or None,
            "dates": (request.form.get("dates") or "").strip() or None,
            "external_id": (request.form.get("external_id") or "").strip() or None,
            "verification_status": (request.form.get("verification_status") or "").strip() or None,
            "notes": (request.form.get("notes") or "").strip() or None,
            "tradition": (request.form.get("tradition") or "").strip() or None,
            "tenet_system": (request.form.get("tenet_system") or "").strip() or None,
        }
        with system_access(app.config["DB_PATH"]) as acc:
            imp = acc.persons.writes.plan_update(Ref("person", pid), values)
            if not imp.appliable:
                abort(404 if any(b.code == "not_found" for b in imp.blocks) else 400)
            acc.persons.writes.apply(imp)
            acc.persons.writes.add_alias(pid, name, "english", if_absent=True)  # keep searchable
        return redirect(url_for("person_detail", pid=pid))

    @app.post("/person/<int:pid>/alias/add")
    def person_add_alias(pid):
        text = (request.form.get("text") or "").strip()
        if not text:
            abort(400)
        scheme = (request.form.get("scheme") or "english").strip() or "english"
        with system_access(app.config["DB_PATH"]) as acc:
            try:
                acc.persons.writes.add_alias(pid, text, scheme)
            except NotFound:
                abort(404)
        return redirect(url_for("person_detail", pid=pid))

    @app.post("/person/<int:pid>/alias/<int:aid>/delete")
    def person_delete_alias(pid, aid):
        with system_access(app.config["DB_PATH"]) as acc:
            acc.persons.writes.remove_alias(pid, aid)
        return redirect(url_for("person_detail", pid=pid))

    @app.post("/person/<int:pid>/alias/<int:aid>/primary")
    def person_alias_primary(pid, aid):
        """Promote an existing alias to be the person's PRIMARY (display) name, keeping the OLD
        primary name as a searchable alias (a person's primary_name is its own column). Routed
        through the access-API set_primary command (authorized + audited)."""
        with system_access(app.config["DB_PATH"]) as acc:
            try:
                acc.persons.writes.set_primary(pid, aid)
            except NotFound:
                abort(404)
        return redirect(url_for("person_detail", pid=pid))

    @app.get("/person/<int:pid>/treasuryoflives")
    def person_treasuryoflives(pid):
        """Find the person on Treasury of Lives (classical Tibetan authors) by name.

        We deliberately do NOT try to build a direct biography URL: Treasury of Lives sits
        behind a bot wall (so a canonical, title-slugged URL can't be resolved server-side),
        and its id-only URLs — e.g. /biographies/view/<id> reconstructed from a Wikidata
        P4138 id — 404 in the browser without the title slug. So we send the operator to a
        site-scoped web search of treasuryoflives.org for the name, which reliably lands on
        the right biography. Name-based → works for every person, bound to an authority or
        not (the old P4138 path silently failed for the majority)."""
        from urllib.parse import quote_plus
        p = _acc(g.db).persons.reads.get(pid)
        if not p:
            abort(404)
        name = (p.primary_name or "").strip()
        return redirect("https://www.google.com/search?q=" +
                        quote_plus(f"site:treasuryoflives.org {name}"))

    @app.post("/people/new")
    def person_new():
        name = (request.form.get("primary_name") or "").strip()
        if not name:
            abort(400)
        role_hint = (request.form.get("role_hint") or "").strip() or None
        dates = (request.form.get("dates") or "").strip() or None
        scheme = request.form.get("scheme") or "english"
        tenet = (request.form.get("tenet_system") or "").strip() or None

        def _apply_tenet(pid):
            """Persist the operator's tenet_system on a freshly-created person (via the
            audited entity-API update, like the edit form). No-op when left blank."""
            if not tenet:
                return
            acc = _acc(g.db)
            imp = acc.persons.writes.plan_update(Ref("person", pid), {"tenet_system": tenet})
            if imp.appliable:
                acc.persons.writes.apply(imp)
        # The authority pick (hub id) the operator auto-filled — bdr:P… / wikidata:Q… /
        # viaf:…. Optional / hand-editable.
        pick = (request.form.get("external_id") or "").strip() or None

        if pick:
            from catalogue.services import picker as P
            from catalogue.services import person_dedup as PD
            # Climb the pick to its Wikidata hub + harvest its full cross-link set, then
            # look for an existing person already carrying ANY of those keys. This dedups
            # cross-scheme: a BDRC pick collapses onto a record bound under Wikidata.
            try:
                _n, _a, extra = P._harvest_extra(pick)
            except Exception:
                extra = {P._person_scheme(pick): pick}
            hub = extra.get("wikidata", pick)
            keys = {v for k, v in extra.items() if k != "_incomplete"}
            keys.add(pick)
            matches = PD.persons_with_keys(g.db, keys)
            # A confident, single, non-conflated match → reuse it instead of forging a
            # duplicate. (Conflation = the candidate's key-set already spans >1 hub id;
            # ambiguity = >1 match. Both are punted to the review queue below.)
            if len(matches) == 1:
                existing = next(iter(matches))
                hubs = {k for k in (keys | PD.key_set(g.db, existing))
                        if k.startswith(PD.HUB_PREFIX)}
                if len(hubs) <= 1:
                    _add_alias(g.db, "person", existing, name, scheme)  # keep this spelling
                    g.db.commit()
                    flash(f"“{name}” already exists as person #{existing} "
                          f"(same authority id) — added your spelling as an alias.")
                    return redirect(url_for("person_detail", pid=existing))
            # No confident match: create PROVISIONAL with the pick parked as a suggestion
            # (external_id stays NULL) so the row enters the review worklist, where
            # acceptance re-runs on-bind dedup before it's finalized.
            new_pid = _acc(g.db).persons.writes.insert_person(
                name, role_hint, dates, suggested_external_id=hub)
            _add_alias(g.db, "person", new_pid, name, scheme)
            _apply_tenet(new_pid)
            g.db.commit()
            flash(f"Created “{name}” (provisional) with a suggested binding {hub} — "
                  "confirm it on the People review page to dedup + verify.")
            return redirect(url_for("people_list"))

        # No authority pick: a plain provisional person (a worklist entry to bind later).
        new_pid = _acc(g.db).persons.writes.insert_person(name, role_hint, dates)
        _add_alias(g.db, "person", new_pid, name, scheme)
        _apply_tenet(new_pid)
        g.db.commit()
        return redirect(url_for("people_list"))
