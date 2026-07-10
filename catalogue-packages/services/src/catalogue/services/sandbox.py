"""Copy-DB sandbox: experiment against a COPY of the catalogue, then swap it in.

The catalogue is mutated through a UI/CLI that an operator drives by hand
(review, curation, triage). `CATALOGUE_DRY_RUN`/`DryRunConnection` rolls every
write back, which is fine for "what would this do?" but useless for "experiment,
then keep it" — the edits are discarded. This module gives the other half:

    fork    live → live.sandbox          (a real, writable copy)
    …run the app/CLI against the sandbox; edits persist to the copy…
    promote sandbox → live               (atomic swap, after a backup)
    discard sandbox                       (throw the copy away)

`promote` refuses to clobber live if live changed since the fork (a *freeze
check*), unless forced. The swap is whole-DB, so any edits made to LIVE during a
sandbox session are lost on promote — the model assumes a single operator with a
frozen live DB for the duration of a session. The freeze check + a mandatory
pre-swap backup are the guard rails, not a merge.

CLI:
    python -m catalogue.services.sandbox fork    [live] [--force]
    python -m catalogue.services.sandbox status  [live]
    python -m catalogue.services.sandbox promote [live] [--force]
    python -m catalogue.services.sandbox discard [live]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from catalogue.db_store import connect
from catalogue.db_store import default_db_path

# Same source of truth as web.DEFAULT_DB: the live catalogue path, overridable via
# CATALOGUE_DB. So `sandbox.fork()` (no arg) forks whatever DB the app actually
# serves, and the test suite (which sets CATALOGUE_DB to a throwaway) can't fork/
# promote against the real live DB by accident.
DEFAULT_LIVE = default_db_path()
SANDBOX_SUFFIX = ".sandbox"
META_SUFFIX = ".sandbox.meta.json"


class SandboxError(RuntimeError):
    """Fork/promote/discard precondition failed (missing/extant sandbox, drift)."""


# ── paths ───────────────────────────────────────────────────────────────────
def sandbox_path(live: str | os.PathLike) -> str:
    return str(live) + SANDBOX_SUFFIX


def meta_path(live: str | os.PathLike) -> str:
    return str(live) + META_SUFFIX


def _sidecars(db_path: str) -> list[str]:
    """The WAL/SHM sidecars SQLite may leave next to a db file."""
    return [db_path + "-wal", db_path + "-shm"]


# ── helpers ───────────────────────────────────────────────────────────────────
def _checkpoint(db_path: str) -> None:
    """Fold the WAL back into the main db file so it is self-contained — and so
    a content hash of the .db reflects every committed write. TRUNCATE also
    removes the -wal file. Best-effort: a held read lock can block a full
    checkpoint, which is acceptable for the single-operator model."""
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _live_marker(live: str) -> dict:
    """A fingerprint of live's current committed state. Checkpoint first so the
    -wal contents are folded in and the hash is stable."""
    _checkpoint(live)
    st = os.stat(live)
    return {"size": st.st_size, "sha256": _sha256(live)}


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# ── operations ──────────────────────────────────────────────────────────────
def fork(live: str | os.PathLike = DEFAULT_LIVE, *, force: bool = False) -> dict:
    """Create live.sandbox as a clean standalone copy of live (`VACUUM INTO`)
    and record a fork marker (live's fingerprint at fork time) in a sidecar
    JSON. Refuses if a sandbox already exists unless `force` (which discards the
    old one first)."""
    live = str(live)
    if not os.path.exists(live):
        raise SandboxError(f"live DB not found: {live}")
    sb = sandbox_path(live)
    if os.path.exists(sb):
        if not force:
            raise SandboxError(
                f"sandbox already exists: {sb} — promote/discard it first, or --force"
            )
        discard(live)

    marker = _live_marker(live)
    # VACUUM INTO writes a fresh, defragmented copy with no WAL baggage. It fails
    # if the target exists, so the discard above (or the not-exists check) matters.
    conn = connect(live)
    try:
        conn.execute("VACUUM INTO ?", (sb,))
    finally:
        conn.close()

    meta = {
        "live_path": os.path.abspath(live),
        "forked_at": _ts(),
        "live_at_fork": marker,
    }
    Path(meta_path(live)).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"sandbox": sb, **meta}


def _read_meta(live: str) -> dict | None:
    mp = meta_path(live)
    if not os.path.exists(mp):
        return None
    return json.loads(Path(mp).read_text(encoding="utf-8"))


def status(live: str | os.PathLike = DEFAULT_LIVE) -> dict:
    """Report whether a sandbox exists, when it was forked, and whether LIVE has
    drifted since the fork (which would make a promote lossy)."""
    live = str(live)
    sb = sandbox_path(live)
    meta = _read_meta(live)
    out = {
        "live": live,
        "sandbox": sb,
        "sandbox_exists": os.path.exists(sb),
        "forked_at": (meta or {}).get("forked_at"),
        "live_drifted": None,
    }
    if meta and os.path.exists(live):
        cur = _live_marker(live)
        out["live_drifted"] = cur != meta["live_at_fork"]
    return out


def promote(live: str | os.PathLike = DEFAULT_LIVE, *, force: bool = False) -> dict:
    """Swap the sandbox in as the new live DB. Order: (1) freeze-check — refuse
    if live changed since fork unless `force`; (2) back up live; (3) checkpoint
    the sandbox so it is self-contained; (4) atomic os.replace; (5) clear stale
    live -wal/-shm. Returns the backup path."""
    live = str(live)
    sb = sandbox_path(live)
    if not os.path.exists(sb):
        raise SandboxError(f"no sandbox to promote: {sb} — fork first")
    meta = _read_meta(live)

    if os.path.exists(live):
        if meta and not force:
            cur = _live_marker(live)
            if cur != meta["live_at_fork"]:
                raise SandboxError(
                    "live DB changed since the sandbox was forked — promoting "
                    "would discard those changes. Re-fork to fold them in, or "
                    "--force to overwrite live with the sandbox."
                )
        backup = f"{live}.pre-swap-{_ts()}.bak"
        # Checkpoint live too so the .bak is a complete copy (no orphan -wal).
        _checkpoint(live)
        _copy_file(live, backup)
    else:
        backup = None

    # Make the sandbox self-contained, then atomically move it into place.
    _checkpoint(sb)
    os.replace(sb, live)
    for s in _sidecars(live):
        if os.path.exists(s):
            os.remove(s)
    # Sandbox is gone (moved); drop its now-stale sidecars + marker.
    _cleanup_sandbox_sidecars(live)
    return {"live": live, "backup": backup, "promoted_at": _ts()}


def discard(live: str | os.PathLike = DEFAULT_LIVE) -> dict:
    """Delete the sandbox copy and its sidecars/marker. No-op if absent."""
    live = str(live)
    sb = sandbox_path(live)
    removed = []
    for p in [sb, *_sidecars(sb)]:
        if os.path.exists(p):
            os.remove(p)
            removed.append(p)
    _cleanup_sandbox_sidecars(live)
    return {"removed": removed}


def _cleanup_sandbox_sidecars(live: str) -> None:
    for p in _sidecars(sandbox_path(live)) + [meta_path(live)]:
        if os.path.exists(p):
            os.remove(p)


def _copy_file(src: str, dst: str) -> None:
    # Plain byte copy (post-checkpoint the file is complete). Avoids importing
    # shutil for one call and keeps the copy explicit.
    with open(src, "rb") as a, open(dst, "wb") as b:
        for chunk in iter(lambda: a.read(1 << 20), b""):
            b.write(chunk)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fork", "status", "promote", "discard"):
        p = sub.add_parser(name)
        p.add_argument("live", nargs="?", default=DEFAULT_LIVE)
        if name in ("fork", "promote"):
            p.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "fork":
            res = fork(args.live, force=args.force)
            print(f"forked {args.live} → {res['sandbox']} (at {res['forked_at']})")
            print(f"  run the app/CLI against: {res['sandbox']}")
        elif args.cmd == "status":
            res = status(args.live)
            if not res["sandbox_exists"]:
                print(f"no sandbox for {args.live}")
            else:
                drift = {True: "YES — promote would lose live edits",
                         False: "no", None: "unknown"}[res["live_drifted"]]
                print(f"sandbox: {res['sandbox']}\n  forked_at: {res['forked_at']}"
                      f"\n  live drifted since fork: {drift}")
        elif args.cmd == "promote":
            res = promote(args.live, force=args.force)
            print(f"promoted sandbox → {res['live']} (backup: {res['backup']})")
        elif args.cmd == "discard":
            res = discard(args.live)
            print(f"discarded {len(res['removed'])} file(s)")
    except SandboxError as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
