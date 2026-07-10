"""Settings: the library mount root(s) — add / remove / repoint, and per-root
folder→subject derivation.

See [[mount-root-settings]] and [[sensitive-settings-localhost-only]]. The library
can live under one or more roots (vocab `_library_roots`); moving one triggers a
prefix-swap + rehash repoint rather than a re-ingest. Every mutating route here is
LOCALHOST-ONLY — sensitive settings can corrupt/relocate the catalogue and the
Finder "Browse…" picker only makes sense on the server's own screen, so a remote
phone client sees the roots read-only.
"""
from __future__ import annotations

import os
import subprocess

from flask import abort, jsonify, redirect, render_template, request, url_for, g

from catalogue.webui.routes._shared import _acc

from catalogue.access_api import system_access
from catalogue.cli import backup as backup_mod
from catalogue.cli import exclude_purge
from catalogue.services import mount as mount_mod
from catalogue.services import reconcile as reconcile_mod
from catalogue.services import skip as skip_mod
from catalogue.services import filing as filing_mod


def _is_local() -> bool:
    """True when the request comes from the machine running the catalogue. Only
    then may sensitive settings be changed (or the native folder picker invoked)."""
    return (request.remote_addr or "") in ("127.0.0.1", "::1", "localhost")


def _within_root(root_path: str, path: str) -> bool:
    """True if `path` is the root itself or lives beneath it — guards the folder
    browser/exclude routes against escaping the configured root via `..`."""
    base = os.path.realpath(root_path)
    rp = os.path.realpath(path)
    return rp == base or rp.startswith(base + os.sep)


def _child_dirs(path: str) -> list:
    """Immediate visible (non-dot) subdirectories of `path`, name-sorted."""
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    out.append(e.path)
    except OSError:
        pass
    return sorted(out, key=lambda p: os.path.basename(p).lower())


def _has_subdir(path: str) -> bool:
    """Cheap 'is this folder expandable?' — stops at the first visible subdir."""
    return bool(_child_dirs(path)[:1])


def register(app, ctx):
    def _require_local():
        if not _is_local():
            abort(403)

    def _roots_ctx():
        return [{"id": r.id, "path": r.path, "derive_subject": r.derive_subject,
                 "count": mount_mod.holdings_under(g.db, r.id)}
                for r in mount_mod.library_roots()]

    def _purge_access():
        """A system Access for the exclusion purge, pointed at the webui's cover stores so an
        edition delete trashes the right `e<id>` art. Caller closes it (use as a context manager)."""
        acc = system_access(app.config["DB_PATH"])
        acc.cover_cache = app.config["COVERS_CACHE"]
        acc.cover_pinned = app.config["COVERS_PINNED"]
        return acc

    def _excluded_count():
        """How many already-catalogued files now sit under an excluded folder
        (the purge banner's headline number)."""
        with system_access(app.config["DB_PATH"]) as acc:    # read-only plan, no writes
            excl, _del = exclude_purge.plan(acc)
            return len(excl)

    def _settings_page(**kw):
        ctx_ = {"roots": _roots_ctx(), "is_local": _is_local(),
                "mount_root": mount_mod.current_mount_root(),
                "trash_dir": mount_mod.trash_dir(),
                "inbox_dirs": filing_mod.inbox_dirs(),
                "excluded_count": _excluded_count(),
                "holding_count": _acc(g.db).holdings.reads.total()}
        ctx_.update(kw)
        return render_template("settings.html", **ctx_)

    @app.get("/settings")
    def settings_view():
        return _settings_page()

    # ── add / remove / derive-toggle ─────────────────────────────────────────
    @app.post("/settings/roots/add")
    def settings_root_add():
        _require_local()
        path = (request.form.get("path") or "").strip()
        derive = bool(request.form.get("derive_subject"))
        try:
            mount_mod.add_root(path, derive_subject=derive)
        except mount_mod.RootError as e:
            return _settings_page(error=str(e))
        return _settings_page(info="Root added. Run a Scan to ingest the books under it.")

    @app.post("/settings/roots/<int:root_id>/derive")
    def settings_root_derive(root_id):
        _require_local()
        mount_mod.set_derive_subject(root_id, bool(int(request.form.get("value") or 0)))
        return _settings_page()

    @app.post("/settings/roots/<int:root_id>/remove")
    def settings_root_remove(root_id):
        _require_local()
        try:
            mount_mod.remove_root(g.db, root_id)
        except mount_mod.RootError as e:
            return _settings_page(error=str(e))
        return _settings_page(info="Root removed.")

    # ── Trash folder for deleted book files ───────────────────────────────────
    @app.post("/settings/trash-dir")
    def settings_trash_dir():
        _require_local()
        path = (request.form.get("path") or "").strip()
        if not path:
            return _settings_page(error="Trash folder can't be empty.")
        mount_mod.set_trash_dir(path)
        return _settings_page(info="Trash folder set. Deleted book files will be "
                                   "moved here.")

    # ── Inbox folders (scanned first, separate from library roots) ────────────
    # New books dropped here are walked + committed BEFORE the rest of the library
    # (reconcile is inbox-first), so they're reviewable in seconds, then filed OUT onto
    # their subject shelf on review. Inbox membership is by configured FOLDER (listed
    # here / vocab `_inbox_dirs`) — not a magic name; defaults to filing.DEFAULT_INBOX_DIR.
    @app.post("/settings/inbox-dirs/add")
    def settings_inbox_add():
        _require_local()
        path = (request.form.get("path") or "").strip().rstrip("/")
        if not path:
            return _settings_page(error="Inbox folder can't be empty.")
        if not os.path.isdir(path):
            return _settings_page(error=f"No such directory on disk: {path}")
        filing_mod.add_inbox_dir(path)
        return _settings_page(info="Inbox folder added. New books dropped here are "
                                   "scanned first on the next Scan.")

    @app.post("/settings/inbox-dirs/remove")
    def settings_inbox_remove():
        _require_local()
        filing_mod.remove_inbox_dir((request.form.get("path") or "").strip())
        return _settings_page(info="Inbox folder removed.")

    # ── repoint (a moved/renamed root) — preview then apply ───────────────────
    # One flow, single-root OR per-root: a `root_id` form field scopes it to that
    # root (its holdings, its stored path); absent, it moves the primary root and
    # the whole catalogue (the legacy single-root behaviour).
    @app.post("/settings/mount-root")
    def settings_mount_root():
        _require_local()
        root_id = request.form.get("root_id", type=int)
        new_root = ((request.form.get("mount_root") or request.form.get("new_root") or "")
                    .strip().rstrip("/"))
        if not new_root:
            return _settings_page(error="Mount root can't be empty.")
        if not os.path.isdir(new_root):
            return _settings_page(error=f"No such directory on disk: {new_root}")
        roots = mount_mod.library_roots()
        if root_id:
            cur = next((r for r in roots if r.id == root_id), None)
            if cur is None:
                abort(404)
            try:                                   # overlap / same-server vs the OTHER roots
                mount_mod.validate_new_root(
                    new_root, existing=[r for r in roots if r.id != root_id])
            except mount_mod.RootError as e:
                return _settings_page(error=str(e))
            old_root = cur.path
        else:
            old_root = mount_mod.current_mount_root()
        if new_root == old_root:
            return _settings_page(info="That's already the location — no change.")
        plan = mount_mod.plan_repoint(g.db, old_root, new_root, root_id=root_id) \
            if old_root else None
        if not plan or plan["matched"] == 0:       # nothing to move → just set the path
            if root_id:
                mount_mod.repoint_root(g.db, root_id, new_root)
            else:
                mount_mod.set_mount_root(new_root)
            return _settings_page(info="Mount root set. Run a Scan to ingest the library.")
        plan["root_id"] = root_id
        return _settings_page(pending=plan)

    @app.post("/settings/mount-root/apply")
    def settings_mount_root_apply():
        _require_local()
        mode = request.form.get("mode")
        root_id = request.form.get("root_id", type=int)
        new_root = (request.form.get("new_root") or "").strip().rstrip("/")
        if not new_root or not os.path.isdir(new_root):
            abort(400)
        if mode == "rescan":
            if root_id:
                mount_mod.repoint_root(g.db, root_id, new_root, rehash=False)
            else:
                mount_mod.set_mount_root(new_root)
            return redirect(url_for("reconcile_view"))
        if mode == "repoint":
            # Bulk write — snapshot the live DB first (skipped in dry-run sandboxes).
            if not app.config.get("DRY_RUN"):
                try:
                    backup_mod.backup(app.config["DB_PATH"])
                except SystemExit:
                    pass                       # a stale same-second backup shouldn't block
            try:
                if root_id:
                    summary = mount_mod.repoint_root(g.db, root_id, new_root, rehash=True)
                else:
                    old_root = mount_mod.current_mount_root()
                    summary = mount_mod.repoint(g.db, old_root, new_root,
                                                rehash=True, drop_pending=True)
                    mount_mod.set_mount_root(new_root)
            except mount_mod.RootError as e:
                return _settings_page(error=str(e))
            return _settings_page(repoint_summary=summary)
        abort(400)

    # ── native Finder folder picker (localhost-only) ──────────────────────────
    @app.post("/settings/browse")
    def settings_browse():
        """Pop a native macOS folder chooser on the server's screen and return the
        absolute path picked. Localhost-only: the dialog opens where the server
        runs, so a remote client could neither see nor use it."""
        _require_local()
        try:
            out = subprocess.run(
                ["osascript", "-e",
                 'POSIX path of (choose folder with prompt "Pick a library root")'],
                capture_output=True, text=True, timeout=300)
        except Exception as e:                     # osascript missing / not macOS
            return jsonify(error=str(e)), 500
        if out.returncode != 0:                    # user pressed Cancel
            return jsonify(cancelled=True)
        return jsonify(path=out.stdout.strip().rstrip("/"))

    # ── per-root subdirectory exclusion (the folder checkbox tree) ────────────
    # Lazily browse the folder tree under a root; uncheck a folder to keep it (and
    # everything beneath it) out of every future scan. Localhost-only — it reads
    # the server's filesystem and rewrites the catalogue's exclusion config.
    def _root_or_404(root_id):
        root = next((r for r in mount_mod.library_roots() if r.id == root_id), None)
        if root is None:
            abort(404)
        return root

    @app.get("/settings/roots/<int:root_id>/folders")
    def settings_root_folders(root_id):
        _require_local()
        root = _root_or_404(root_id)
        path = (request.args.get("path") or root.path).rstrip("/") or root.path
        if not _within_root(root.path, path) or not os.path.isdir(path):
            abort(400)
        locked = skip_mod.under_excluded(path)     # whole subtree already excluded above
        children = [{"name": os.path.basename(c), "path": c,
                     "excluded": True if locked else skip_mod.subdir_excluded(c),
                     "locked": locked, "has_children": _has_subdir(c)}
                    for c in _child_dirs(path)]
        return jsonify(path=path, locked=locked, children=children)

    @app.post("/settings/roots/<int:root_id>/exclude")
    def settings_root_exclude(root_id):
        _require_local()
        root = _root_or_404(root_id)
        path = (request.form.get("path") or "").strip()
        if not path or not _within_root(root.path, path):
            abort(400)
        excluded = bool(int(request.form.get("excluded") or 0))
        skip_mod.set_subdir_excluded(path, excluded)
        # Clear any already-scanned 'new' files now under an excluded folder from the
        # Scan page's pending queue (re-including doesn't re-add — a re-scan surfaces them).
        pending_removed = reconcile_mod.prune_excluded_ingest(g.db) if excluded else 0
        return jsonify(ok=True, path=skip_mod._norm_dir(path), excluded=excluded,
                       excluded_count=_excluded_count(), pending_removed=pending_removed)

    @app.post("/settings/exclusions/purge")
    def settings_exclusions_purge():
        _require_local()
        with _purge_access() as acc:
            excl, del_eds = exclude_purge.plan(acc)
            if not excl:
                return _settings_page(info="Nothing to remove — no catalogued files "
                                           "sit under excluded folders.")
            if app.config.get("DRY_RUN"):
                removed_works = 0                  # dry run: report the plan, write nothing
            else:
                try:                               # snapshot the live DB before deleting
                    backup_mod.backup(app.config["DB_PATH"])
                except SystemExit:
                    pass                           # a stale same-second backup shouldn't block
                removed_works = exclude_purge.apply(acc, excl, del_eds)
        bits = [f"{len(excl)} holding{'' if len(excl) == 1 else 's'}"]
        if del_eds:
            bits.append(f"{len(del_eds)} edition{'' if len(del_eds) == 1 else 's'}")
        if removed_works:
            bits.append(f"{removed_works} orphaned work{'' if removed_works == 1 else 's'}")
        return _settings_page(info="Removed " + ", ".join(bits)
                              + " under excluded folders.")
