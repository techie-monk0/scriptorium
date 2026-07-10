"""Sandbox promote / discard — swap the forked copy DB into live.

See [[sandbox-swap-for-live-writes]]: when running against `…/catalogue.db_store.sandbox`,
these actions either promote the experimental copy over the live DB (with a
backup) or discard it. Both close the request handle first so the file swap is safe.
"""
from __future__ import annotations

from flask import abort, render_template, request, g


def register(app, ctx):
    # ── Sandbox promote / discard (swap the copy into live) ───────────────
    @app.post("/sandbox/promote")
    def sandbox_promote():
        from catalogue.services import sandbox as sb
        if not app.config["SANDBOX"]:
            abort(400)
        live = app.config["DB_PATH"][:-len(".sandbox")]
        try:
            g.db.close()
        except Exception:
            pass
        try:
            res = sb.promote(live, force=request.form.get("force") == "1")
        except sb.SandboxError as e:
            return render_template("sandbox_done.html", ok=False, message=str(e),
                                   live=live), 409
        return render_template("sandbox_done.html", ok=True, live=live,
                               backup=res["backup"])

    @app.post("/sandbox/discard")
    def sandbox_discard():
        from catalogue.services import sandbox as sb
        if not app.config["SANDBOX"]:
            abort(400)
        live = app.config["DB_PATH"][:-len(".sandbox")]
        try:
            g.db.close()
        except Exception:
            pass
        sb.discard(live)
        return render_template("sandbox_done.html", ok=True, live=live,
                               discarded=True)
