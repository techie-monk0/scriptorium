"""The library mount root(s) — single source of truth + safe re-pointing.

The library can live under one OR MORE on-disk roots. Each root is a folder the
books live under, plus a flag for whether its sub-folders become subjects. The
list is stored in vocab.json as `_library_roots` (a JSON array of
`{id, path, derive_subject}`); `_library_root` is kept in sync as the *primary*
(first) root's path so any legacy reader still works. They are read by everything
that scans the tree or shows file paths (see `reconcile.library_roots`,
`sweep.default_mount_root`, `features.library_root`) and edited from /settings.

Each holding records which root owns it (`holding.root_id`), set at ingest by a
longest-prefix match. Roots are validated never to nest, overlap, or even share a
string prefix, and to all sit on the same server, so that prefix match is
unambiguous and a per-root repoint/remove touches exactly its own files.

When a mount MOVES on disk — a cloud client re-syncs to a renamed folder
(`kDrive` → `kDrive 2`), an external drive remounts elsewhere — the stored
absolute holding paths go stale and every book looks "new" to Scan. `repoint`
fixes that without re-ingesting: it prefix-swaps each stored path onto the new
root and (optionally) re-hashes the bytes now on disk, so files whose content is
unchanged are recognised as unchanged even if a cloud client rewrote their
container bytes. Nothing here re-extracts text, so it is cheap and lossless."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass

from catalogue.db_store.db import VOCAB_PATH


def _acc(db):
    """A system Access over this connection — engine-routed holding reads/writes, the sweep-resume
    cache (`acc.sweep_state`) + the review queue. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)

LEGACY_KEY = "_library_root"     # primary root path, kept in sync for old readers
ROOTS_KEY = "_library_roots"     # the canonical [{id, path, derive_subject}, …]
TRASH_KEY = "_trash_dir"         # folder deleted book files are MOVED to (recoverable)

# Where book files land when an edition/holding is deleted with "move to Trash".
# A real folder rather than the system Trash so it sits inside the synced library.
DEFAULT_TRASH_DIR = os.environ.get("CATALOGUE_TRASH_DIR", "")


class RootError(ValueError):
    """A library-root edit that violates an invariant (overlap/prefix, different
    server, non-existent dir, or removing a root that still owns holdings). Web
    routes turn this into an inline message; the message is operator-facing."""


@dataclass(frozen=True)
class Root:
    """One configured library root: a stable id (never reused), an on-disk path,
    and whether its folder names derive subjects (off → ingested files fall to the
    `Uncategorized` safety-net subject)."""
    id: int
    path: str
    derive_subject: bool = True


# ── path normalisation / matching ─────────────────────────────────────────────
def _norm(p) -> str:
    """Canonical stored form of a root/path: forward slashes, no trailing slash."""
    return str(p or "").replace("\\", "/").rstrip("/")


def _key(p) -> str:
    """Comparison key: normalised + case-folded (APFS is case-insensitive, so
    `/Lib` and `/lib` are the same directory and must match)."""
    return _norm(p).casefold()


# ── reading the configured roots ──────────────────────────────────────────────
def _vocab() -> dict:
    try:
        return json.loads(VOCAB_PATH.read_text("utf-8"))
    except Exception:
        return {}


def library_roots() -> list[Root]:
    """Every configured library root. Reads `_library_roots`; falls back to the
    legacy single `_library_root` string as a stable id=1 root (so a DB migrated
    before this feature, and its root_id backfill, agree on the id). [] if unset.
    Read fresh from vocab.json each call (the file is tiny) so a direct edit / a
    monkeypatched VOCAB_PATH takes effect with no cache to bust."""
    data = _vocab()
    raw = data.get(ROOTS_KEY)
    if isinstance(raw, list) and raw:
        out = []
        for r in raw:
            if isinstance(r, dict) and r.get("path"):
                out.append(Root(int(r.get("id") or 0), _norm(r["path"]),
                                bool(r.get("derive_subject", True))))
        if out:
            return out
    legacy = data.get(LEGACY_KEY)
    if legacy:
        return [Root(1, _norm(legacy), True)]
    return []


def reload() -> None:
    """Drop dependent caches (after editing vocab.json directly, e.g. in tests)."""
    _bust_cache()


def primary_root() -> Root | None:
    """The first/primary root, or None when none configured."""
    roots = library_roots()
    return roots[0] if roots else None


def current_mount_root() -> str:
    """The primary mount root's path ('' if unset) — the legacy single-root reader,
    kept for callers (sweep default, tests) that still want one path."""
    r = primary_root()
    return r.path if r else ""


def owning_root(path, roots=None) -> Root | None:
    """The root that owns `path` — the longest-prefix match on a slash boundary.
    Overlaps are disallowed, so at most one matches; longest-match is belt-and-
    braces. None when the path sits under no configured root."""
    if not path:
        return None
    if roots is None:
        roots = library_roots()
    pk = _key(path)
    best = None
    for r in roots:
        rk = _key(r.path)
        if rk and (pk == rk or pk.startswith(rk + "/")):
            if best is None or len(rk) > len(_key(best.path)):
                best = r
    return best


def owning_root_id(path, roots=None) -> int | None:
    """The id of the root that owns `path`, or None (used to set holding.root_id)."""
    r = owning_root(path, roots)
    return r.id if r else None


def holdings_under(db, root_id: int) -> int:
    """How many holdings the catalogue attributes to root `root_id`."""
    return _acc(db).holdings.reads.count_by_root(root_id)


def backfill_holding_root_ids(conn) -> int:
    """Populate holding.root_id for pre-existing rows by longest-prefix root match.

    Legacy one-off: when `root_id` was added, db_store's migration used to do this
    inline — but it needs library-root resolution (a business/filesystem concern), which
    must not be imported down into db_store. So it lives here. Normal ingest
    (sweep.py) already sets root_id on insert, so a live DB needs this only if it
    predates the column. Best-effort; returns the number of rows updated.
    """
    roots = library_roots()
    if not roots:
        return 0
    acc = _acc(conn)
    updated = 0
    for hid, fp in acc.holdings.reads.with_file_path():
        rid = owning_root_id(fp, roots)
        if rid is not None:
            acc.holdings.writes.set_root(hid, rid)
            updated += 1
    return updated


# ── validation ────────────────────────────────────────────────────────────────
def _server_of(path) -> str:
    """Which 'server' a path lives on: the name of the WebDAV mount that covers it,
    else 'local'. Used to forbid roots that span different backends."""
    try:
        from . import webdav
        for m in webdav.load_mounts():
            if m.covers(path):
                return m.name
    except Exception:
        pass
    return "local"


def _overlaps(a: str, b: str) -> bool:
    """True if two roots nest, are equal, or merely share a string prefix
    (`/Lib` vs `/Library`) — all disallowed so prefix attribution stays clean."""
    ka, kb = _key(a), _key(b)
    return bool(ka) and bool(kb) and (ka.startswith(kb) or kb.startswith(ka))


def validate_new_root(path, *, existing=None) -> str:
    """Vet a candidate root; return its normalised path or raise RootError. Enforces:
    absolute, exists on disk, no overlap/shared-prefix with existing roots, and same
    server as the others."""
    path = _norm(path)
    if not path:
        raise RootError("Root path can't be empty.")
    if not os.path.isabs(path):
        raise RootError("Give a full absolute path (e.g. /Users/you/Library).")
    if not os.path.isdir(path):
        raise RootError(f"No such directory on disk: {path}")
    existing = library_roots() if existing is None else existing
    for r in existing:
        if _overlaps(path, r.path):
            raise RootError(
                f"“{path}” overlaps or shares a path prefix with the existing root "
                f"“{r.path}”. Library roots must be distinct, non-nested folders.")
    others = {_server_of(r.path) for r in existing}
    if others and others != {_server_of(path)}:
        raise RootError(
            "All library roots must sit on the same server/mount; "
            f"“{path}” is on a different one.")
    return path


# ── writing the configured roots (minimal-diff, value-surgical) ───────────────
def _scan_value(raw: str, i: int) -> int:
    """Index just past the JSON value starting at/after `i` (string, array, object,
    or scalar) — so a single value can be replaced without reformatting the whole
    file (vocab.json's compact inline objects would otherwise blow up the diff)."""
    n = len(raw)
    while i < n and raw[i] in " \t\r\n":
        i += 1
    if i >= n:
        return i
    c = raw[i]
    if c == '"':                                    # string
        i += 1
        while i < n:
            if raw[i] == '\\':
                i += 2
                continue
            if raw[i] == '"':
                return i + 1
            i += 1
        return i
    if c in "[{":                                   # array / object (balanced)
        depth = 0
        while i < n:
            ch = raw[i]
            if ch == '"':                           # skip strings inside
                i += 1
                while i < n and raw[i] != '"':
                    i += 2 if raw[i] == '\\' else 1
                i += 1
                continue
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return i
    while i < n and raw[i] not in ",}\n":           # scalar
        i += 1
    return i


def _write_vocab_value(key: str, value, path=None) -> None:
    """Persist `key: value` into vocab.json, surgically replacing just that key's
    value (or appending via a full round-trip if the key is absent). `path` defaults to
    this module's VOCAB_PATH; callers in other modules pass their own binding so reads
    and writes stay on the same file under test."""
    path = path or VOCAB_PATH
    raw = path.read_text("utf-8")
    encoded = json.dumps(value, ensure_ascii=False)
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*', raw)
    if m:
        end = _scan_value(raw, m.end())
        path.write_text(raw[:m.start()] + '"' + key + '": ' + encoded + raw[end:],
                        "utf-8")
    else:
        data = json.loads(raw)
        data[key] = value
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")


def _bust_cache() -> None:
    try:                                            # features.library_root is lru_cached
        from .features import library_root
        library_root.cache_clear()
    except Exception:
        pass


def save_roots(roots: list[Root]) -> None:
    """Persist the full root list, keeping `_library_root` in sync as the primary."""
    payload = [{"id": r.id, "path": r.path, "derive_subject": r.derive_subject}
               for r in roots]
    _write_vocab_value(ROOTS_KEY, payload)
    _write_vocab_value(LEGACY_KEY, roots[0].path if roots else "")
    _bust_cache()


def set_mount_root(root: str) -> None:
    """Set a single primary root (legacy/back-compat path, used by tests and the
    first-time set). Surgically updates `_library_root` (trailing slash trimmed) to
    preserve vocab.json formatting; only materialises `_library_roots` when it is
    already present, so a legacy single-root file stays single-key and clean."""
    root = _norm(root)
    _write_vocab_value(LEGACY_KEY, root)
    if ROOTS_KEY in _vocab():
        _write_vocab_value(ROOTS_KEY, [{"id": 1, "path": root, "derive_subject": True}])
    _bust_cache()


# ── Trash folder (deleted book files are MOVED here, not unlinked) ─────────────
def trash_dir() -> str:
    """The folder deleted book files are moved into (vocab `_trash_dir`). Falls back
    to DEFAULT_TRASH_DIR when unset so a fresh install still has a safe landing spot."""
    return _norm(_vocab().get(TRASH_KEY) or DEFAULT_TRASH_DIR)


def set_trash_dir(path: str) -> None:
    """Persist the Trash folder. Stored normalised (forward slashes, no trailing /)."""
    _write_vocab_value(TRASH_KEY, _norm(path))


def move_to_trash(path, trash: str | None = None) -> str | None:
    """Move one file into the Trash folder, returning its new path (or None if the
    source is missing). The Trash dir is created on demand; a name collision gets a
    ` (2)`, ` (3)`… suffix so an earlier trashed file is never overwritten."""
    if not path or not os.path.exists(path):
        return None
    trash = trash or trash_dir()
    if not trash:
        raise RuntimeError(
            "No Trash folder is configured, so a file cannot be moved to Trash. Set "
            "$CATALOGUE_TRASH_DIR, or set the Trash folder in the web Settings page."
        )
    os.makedirs(trash, exist_ok=True)
    base = os.path.basename(str(path).rstrip("/"))
    dest = os.path.join(trash, base)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(trash, f"{stem} ({n}){ext}")
            n += 1
    shutil.move(str(path), dest)
    return dest


def _next_id(roots: list[Root]) -> int:
    return max((r.id for r in roots), default=0) + 1


def add_root(path, *, derive_subject: bool = True) -> Root:
    """Validate and append a new root. Raises RootError on any invariant violation."""
    roots = library_roots()
    path = validate_new_root(path, existing=roots)
    new = Root(_next_id(roots), path, bool(derive_subject))
    save_roots(roots + [new])
    return new


def set_derive_subject(root_id: int, value: bool) -> None:
    """Toggle folder→subject derivation for one root."""
    save_roots([Root(r.id, r.path, bool(value)) if r.id == root_id else r
                for r in library_roots()])


def remove_root(db, root_id: int) -> None:
    """Drop a root from the configuration. Refused while it still owns holdings
    (repoint or detach those first) so removal never silently orphans files."""
    n = holdings_under(db, root_id)
    if n:
        raise RootError(
            f"{n} catalogued file{'s' if n != 1 else ''} still live under this root. "
            "Repoint it, or remove those holdings, before deleting the root.")
    save_roots([r for r in library_roots() if r.id != root_id])


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:                 # read-only — never mutate the mount
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_repoint(db, old_root: str, new_root: str, *, root_id: int | None = None) -> dict:
    """Read-only preview of `repoint`: how many stored holdings sit under old_root,
    how many of those now exist under new_root, and how many would be missing
    there. Lets the UI warn before committing a move. With `root_id`, scoped to that
    root's holdings only."""
    old = _norm(old_root) + "/"
    new = _norm(new_root) + "/"
    under = [fp for fp in _acc(db).holdings.reads.file_paths(root_id)
             if fp and fp.startswith(old)]
    present, missing, sample_missing = 0, 0, []
    for fp in under:
        np = new + fp[len(old):]
        if os.path.exists(np):
            present += 1
        else:
            missing += 1
            if len(sample_missing) < 10:
                sample_missing.append(np)
    return {"old_root": old.rstrip("/"), "new_root": new.rstrip("/"),
            "matched": len(under), "present": present, "missing": missing,
            "sample_missing": sample_missing}


def repoint(db, old_root: str, new_root: str, *, rehash: bool = True,
            drop_pending: bool = True, commit: bool = True,
            root_id: int | None = None) -> dict:
    """Move stored holding paths from old_root onto new_root (prefix swap) — for when
    the SAME files now live under a different root (a moved/renamed mount). With
    `root_id`, only that root's holdings move (a per-root repoint).

    rehash=True recomputes each file's byte-hash from disk, so a cloud client that
    rewrote container bytes on re-sync doesn't make unchanged books look new.
    `content_hash` (the text fingerprint) is left intact — the text is unchanged, so
    it stays valid and no re-extraction is needed. Stale sweep-resume rows under the
    old root are dropped (pure cache), and superseded pending-ingest items for the
    moved files are cleared so Scan reads clean. Returns a summary."""
    old = _norm(old_root) + "/"
    new = _norm(new_root) + "/"
    s = {"matched": 0, "repointed": 0, "rehashed": 0, "bytes_changed": 0,
         "missing": [], "sweep_state_cleared": 0, "pending_dropped": 0}

    acc = _acc(db)
    for hid, fp, fh, rid in acc.holdings.reads.relocation_rows():
        if not fp or not fp.startswith(old):
            continue
        if root_id is not None and rid != root_id:
            continue
        s["matched"] += 1
        np = new + fp[len(old):]
        new_hash = fh
        if rehash and os.path.exists(np) and os.path.getsize(np) > 0:
            try:
                d = _sha256(np)
                if d != fh:
                    s["bytes_changed"] += 1
                new_hash = d
                s["rehashed"] += 1
            except OSError:
                pass
        if not os.path.exists(np):
            s["missing"].append(np)
        acc.holdings.writes.set_location(hid, np, new_hash)
        s["repointed"] += 1

    # Stale sweep-resume rows for the old root: a pure (path, size, mtime, hash)
    # cache, so dropping them just makes a future sweep re-stat from disk. Match in
    # Python to sidestep LIKE wildcard escaping on paths.
    stale = [p for p in acc.sweep_state.reads.paths() if p and p.startswith(old)]
    for p in stale:
        acc.sweep_state.writes.delete_path(p)
    s["sweep_state_cleared"] = len(stale)

    # Pending 'new' ingest items for the moved files are superseded by the repoint
    # (the file is catalogued again under the new path) — clear them.
    if drop_pending:
        drop_ids = []
        for rid, pj in acc.review.reads.pending_items("ingest"):
            try:
                path = (json.loads(pj) or {}).get("path") or ""
            except Exception:
                path = ""
            if path.startswith(new) or path.startswith(old):
                drop_ids.append(rid)
        for rid in drop_ids:
            acc.review.writes.delete(rid)
        s["pending_dropped"] = len(drop_ids)

    if commit:
        db.commit()
    return s


def repoint_root(db, root_id: int, new_root: str, *, rehash: bool = True,
                 commit: bool = True) -> dict:
    """Per-root repoint: move root `root_id` to `new_root`, swapping only that root's
    holdings and updating its stored path. The new path is re-validated against the
    OTHER roots (overlap + same-server) first."""
    roots = library_roots()
    cur = next((r for r in roots if r.id == root_id), None)
    if cur is None:
        raise RootError(f"No such root #{root_id}.")
    new_path = validate_new_root(new_root, existing=[r for r in roots if r.id != root_id])
    summary = repoint(db, cur.path, new_path, rehash=rehash, drop_pending=True,
                      commit=commit, root_id=root_id)
    save_roots([Root(r.id, new_path, r.derive_subject) if r.id == root_id else r
                for r in roots])
    return summary
