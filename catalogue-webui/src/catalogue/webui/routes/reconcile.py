"""Reconcile (the "Scan" page): sync the library folder with the catalogue.

See [[scan-reconcile-page]] and [[mount-root-settings]] — visiting the page
auto-heals moved files (unique text-fingerprint match → silent repoint, since
kDrive re-syncs rewrite bytes and stale the byte-hash), prunes moot pending
items, and lists the remaining proposed dispositions for one-click apply.
"""
from __future__ import annotations

import json
import os

from flask import (
    abort, jsonify, redirect, render_template, request, url_for, g,
)

from catalogue.services import reconcile as reconcile_mod
from catalogue.services import relink as relink_mod
from catalogue.webui.routes._shared import _acc


# Bulk-apply ONE target-free action to many selected items at once. Only actions
# that need no per-item edition target are allowed here (Replace/Add-copy are
# excluded — they pick a specific edition per item); the UI offers only the action
# set common to every selected item, so each apply is valid for its kind.
_BULK_ACTIONS = {"ignore", "distinct", "accept", "remove"}


def register(app, ctx):
    @app.get("/reconcile")
    def reconcile_view():
        roots = reconcile_mod.library_roots()
        mount_root = roots[0] if roots else ""
        mount_roots = roots
        # Auto-heal moved files first: a unique text-fingerprint match is silently
        # repointed (kDrive re-syncs rewrite bytes, so the byte-hash is stale — text
        # identity is what survives); the rest become one-click suggestions below.
        relink = relink_mod.relink_moved(g.db, roots)
        # Then drop pending items that newer state (this relink, or a prior auto-move)
        # has made moot, so the page doesn't show stale 'new'/'missing'/'duplicate' cruft.
        reconcile_mod.prune_stale_ingest(g.db)
        # Drop any pending 'new' files that now sit under an excluded folder (the
        # /settings folder tree), so unchecking a folder clears them here too.
        reconcile_mod.prune_excluded_ingest(g.db)
        # Pending ingest items (proposed dispositions), newest first, with the
        # candidate editions resolved to titles for the picker.
        acc = _acc(g.db)
        items = []
        for rid, pj in acc.review.reads.pending_items("ingest"):
            p = json.loads(pj)
            for c in p.get("candidates") or []:
                ed = acc.editions.reads.get(c["edition_id"])
                c["title"] = ed.title if ed else f"edition {c['edition_id']}"
            p["id"] = rid
            items.append(p)
        resolved = acc.review.reads.status_count("ingest", "resolved")
        # Broken links — computed live every visit (a cheap stat per holding, no walk
        # needed): holdings whose file is gone, and editions with no holding at all.
        broken = reconcile_mod.broken_links(g.db)
        for b in broken["gone"]:                   # attach any one-click move suggestions
            b["suggestions"] = relink["suggestions"].get(b["holding_id"], [])
        return render_template("reconcile.html", items=items, mount_root=mount_root,
                               mount_roots=mount_roots,
                               scan_error=request.args.get("scan_error"),
                               resolved=resolved, last=request.args.get("summary"),
                               broken=broken, relinked=relink["relinked"],
                               undo_token=relink["undo_token"])

    @app.post("/reconcile/run")
    def reconcile_run():
        # The textarea holds sub-paths to scan (blank = the whole library). With a
        # SINGLE root, a relative line is joined onto it (change the root in /settings,
        # never re-type it here). With MULTIPLE roots a relative line is ambiguous —
        # which root? — so each line must be a full absolute path. Absolute lines are
        # always honoured as-is.
        roots_cfg = reconcile_mod.library_roots()
        subs = [r.strip() for r in (request.form.get("roots") or "").splitlines() if r.strip()]
        if subs:
            if len(roots_cfg) > 1:
                ambiguous = [s for s in subs if not os.path.isabs(s)]
                if ambiguous:
                    return redirect(url_for(
                        "reconcile_view", scan_error=(
                            "Multiple library roots are configured, so each scan line must "
                            "be a full absolute path. Ambiguous: " + ", ".join(ambiguous))))
                roots = subs
            else:
                base = roots_cfg[0] if roots_cfg else ""
                roots = [s if (os.path.isabs(s) or not base) else os.path.join(base, s)
                         for s in subs]
        else:
            # Whole-library scan: include any standalone inbox dir so a top-level
            # `_INBOX/` is picked up, and walk it FIRST (reconcile_stream is inbox-first).
            roots = reconcile_mod.scan_roots()
        # Stream the scan: fresh inbox drops are classified + committed first, so they're
        # reviewable within seconds instead of after a full-library walk.
        summary = reconcile_mod.reconcile_stream(g.db, roots)
        return redirect(url_for("reconcile_view", summary=json.dumps(summary)))

    @app.post("/reconcile/<int:item_id>/apply")
    def reconcile_apply(item_id):
        action = request.form.get("action") or "ignore"
        target = request.form.get("target_edition_id", type=int)
        try:
            reconcile_mod.apply_decision(g.db, item_id, action, target_edition_id=target)
        except ValueError:
            abort(400)
        return redirect(url_for("reconcile_view"))

    @app.post("/reconcile/bulk")
    def reconcile_bulk():
        action = request.form.get("action")
        ids = request.form.getlist("item_id", type=int)
        if action not in _BULK_ACTIONS or not ids:
            abort(400)
        done = 0
        for iid in ids:
            try:
                reconcile_mod.apply_decision(g.db, iid, action, commit=False)
                done += 1
            except (ValueError, KeyError):
                pass                               # skip items the action doesn't fit
        g.db.commit()
        return redirect(url_for("reconcile_view", summary=json.dumps(
            {"bulk": action, "applied": done})))

    @app.post("/reconcile/relink")
    def reconcile_relink():
        """Apply a one-click 'found at X' move suggestion: repoint a broken holding to
        the chosen file (rehash bytes, keep content_hash, journal undo)."""
        hid = request.form.get("holding_id", type=int)
        path = request.form.get("path")
        if not hid or not path:
            abort(400)
        try:
            relink_mod.relink_to(g.db, hid, path)
        except ValueError:
            abort(400)
        return redirect(url_for("reconcile_view"))

    @app.post("/reconcile/relink/undo")
    def reconcile_relink_undo():
        """Reverse an auto/confirmed relink via the shared undo journal."""
        from catalogue.services import contributor_undo as U
        token = (request.get_json(silent=True) or request.form).get("token")
        if token is None:
            abort(400)
        try:
            result = U.apply_undo(g.db, int(token))
        except (TypeError, ValueError):
            abort(400)
        if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
            return jsonify(result)
        return redirect(url_for("reconcile_view"))
