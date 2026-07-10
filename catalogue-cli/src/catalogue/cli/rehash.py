"""Recompute holding.file_hash from REAL file content.

After a cloud re-sync left the library as online-only placeholders, an earlier repoint
hashed zero-content (a dehydrated placeholder reads as all zeros), so holding.file_hash is
wrong for those rows. This re-derives each hash from the real bytes — read from disk when
the file is hydrated, else fetched over WebDAV (catalogue.services.webdav) — and updates the
rows that changed.

    python -m catalogue.cli.rehash                 # live DB (snapshots first)
    python -m catalogue.cli.rehash --db path.db --no-backup

Reads are streamed in chunks; WebDAV fetches the whole file (transient, not cached here),
so a full run pulls roughly the library's size over the network if nothing is hydrated.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

from catalogue.db_store import connect
from catalogue.db_store import default_db_path


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rehash(db, *, mounts=None, on_progress=None) -> dict:
    """Recompute file_hash for every holding with a path. Hydrated files are hashed from
    disk; online-only placeholders are fetched over WebDAV. Returns a summary; commits the
    rows that changed. `mounts` defaults to webdav.load_mounts()."""
    from catalogue.services import cloudsync, webdav
    if mounts is None:
        mounts = webdav.load_mounts()
    from catalogue.access_api import system_conn
    acc = system_conn(db)
    rows = [(hid, fp, fh) for hid, _eid, fp, fh, _ch in acc.holdings.reads.with_files()]
    s = {"total": len(rows), "rehashed": 0, "changed": 0,
         "from_disk": 0, "from_webdav": 0, "failed": []}
    for i, (hid, fp, old) in enumerate(rows):
        new = None
        try:
            if fp and os.path.exists(fp) and not cloudsync.is_online_only(fp):
                new = _sha256_file(fp)
                s["from_disk"] += 1
            else:
                data = webdav.fetch_local(fp, mounts=mounts)
                if data is not None:
                    new = _sha256_bytes(data)
                    s["from_webdav"] += 1
        except OSError:
            new = None
        if new is None:
            s["failed"].append(hid)
        else:
            s["rehashed"] += 1
            if new != old:
                acc.holdings.writes.set_file_hash(hid, new)
                s["changed"] += 1
        if on_progress:
            on_progress(i + 1, s)
    db.commit()
    return s


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Recompute holding.file_hash from real content "
                                             "(disk when hydrated, else WebDAV).")
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--no-backup", action="store_true", help="skip the pre-run DB snapshot")
    args = ap.parse_args(argv)
    if not args.no_backup:
        from catalogue.cli.backup import backup
        dest = backup(args.db)
        print(f"snapshot → {dest}", file=sys.stderr)

    def _progress(n, s):
        print(f"\r{n}/{s['total']}  changed={s['changed']}  disk={s['from_disk']}  "
              f"webdav={s['from_webdav']}  failed={len(s['failed'])}", end="", file=sys.stderr)

    conn = connect(args.db)
    try:
        s = rehash(conn, on_progress=_progress)
    finally:
        conn.close()
    print(f"\nrehashed {s['rehashed']}/{s['total']}  (changed {s['changed']}; "
          f"disk {s['from_disk']}, webdav {s['from_webdav']}; failed {len(s['failed'])})")
    if s["failed"]:
        print("failed holding ids:", s["failed"][:50])


if __name__ == "__main__":
    main()
