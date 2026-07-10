"""One-time (re)preparation of the on-disk cover stores — apply the SAME pipeline the fetch and
upload paths now use (catalogue.services.covers.prepare_cover) to every already-cached cover, in
place: trim a baked publisher frame, then downscale oversized covers and re-encode bloated ones
to lean JPEGs (cap the long side at COVER_MAX_DIM, JPEG q82, bake in EXIF orientation, flatten
transparency). This is the backfill for covers cached before trimming existed. Covers that the
pipeline leaves byte-for-byte unchanged (already frameless + lean) are skipped.

    python -m catalogue.cli.normalize_covers                     # dry run — report changes, touch nothing
    python -m catalogue.cli.normalize_covers --apply             # rewrite changed covers in place
    python -m catalogue.cli.normalize_covers --apply --backup    # ...keeping a <file>.orig of each original
    python -m catalogue.cli.normalize_covers --verbose           # also list each cover that changes

Reversibility (the pinned store is irreplaceable; the cache is regenerable):
    python -m catalogue.cli.normalize_covers --restore           # roll every cover back to its <file>.orig
    python -m catalogue.cli.normalize_covers --cleanup           # delete the <file>.orig backups (commit to the new art)

Scans both the auto cover cache (.cover-cache) and the pinned overrides (covers-pinned).
Spines (spine-*.svg) and miss-markers are skipped. Idempotent — safe to re-run.
"""
from __future__ import annotations

import argparse
import os
import shutil

from catalogue.services import covers
from catalogue.db_store import default_db_path

_RASTER = (".jpg", ".jpeg", ".png", ".gif")
_BACKUP_SUFFIX = ".orig"          # a pre-trim original is kept beside its cover as <file>.orig
_EXTS = (".jpg", ".png", ".gif", ".svg")


def _cover_files(d: str) -> dict:
    """key -> path for raster covers in dir `d` (skips spine SVGs, .miss/.orig markers, .part)."""
    out = {}
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        base, ext = os.path.splitext(name)
        if ext.lower() in _RASTER and not base.startswith("spine-"):
            out[base] = os.path.join(d, name)
    return out


def _backups(d: str) -> dict:
    """key -> backup path for every <file>.orig in `d` (the key is the cover's edition key,
    e.g. 'e5' from 'e5.jpg.orig'), so a backup is found regardless of the original extension."""
    out = {}
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if name.endswith(_BACKUP_SUFFIX):
            key = os.path.splitext(name[: -len(_BACKUP_SUFFIX)])[0]   # 'e5.jpg.orig' -> 'e5'
            out[key] = os.path.join(d, name)
    return out


def normalize_dir(d: str, *, apply: bool, backup: bool = False, on_change=None) -> dict:
    """Run `prepare_cover` over every raster cover in `d`; rewrite in place when `apply`. A
    trim is adopted whenever it changes the bytes (even if it doesn't shrink the file), so the
    gate is "did the pipeline change anything", not "did it save bytes". When `backup`, the
    original bytes are copied to <file>.orig before the first rewrite (never overwriting an
    existing .orig, so the EARLIEST original survives repeated runs). Counts + bytes."""
    s = {"scanned": 0, "changed": 0, "before": 0, "after": 0, "errors": 0}
    has_backup = _backups(d) if backup else {}
    for key, path in _cover_files(d).items():
        s["scanned"] += 1
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            s["errors"] += 1
            continue
        new = covers.prepare_cover(data)
        if new == data:                              # pipeline left it untouched → nothing to do
            continue
        s["changed"] += 1
        s["before"] += len(data)
        s["after"] += len(new)
        if on_change:
            on_change(key, len(data), len(new))
        if apply:
            if backup and key not in has_backup:
                shutil.copy2(path, path + _BACKUP_SUFFIX)   # e5.jpg -> e5.jpg.orig
            covers.write_cache(d, key, new)          # atomic; clears the old extension if it changed
    return s


def restore_dir(d: str) -> dict:
    """Roll every cover in `d` back to its <file>.orig backup: drop the current (trimmed) cover
    of whatever extension and move the .orig back to the original name. Counts."""
    s = {"restored": 0, "errors": 0}
    for key, orig in _backups(d).items():
        target = orig[: -len(_BACKUP_SUFFIX)]        # e5.jpg.orig -> e5.jpg (original name+ext)
        try:
            for ext in _EXTS:                        # remove the current cover of any extension
                p = os.path.join(d, key + ext)
                if p != target and os.path.exists(p):
                    os.remove(p)
            os.replace(orig, target)                 # move the backup back, consuming the .orig
            s["restored"] += 1
        except OSError:
            s["errors"] += 1
    return s


def cleanup_dir(d: str) -> dict:
    """Delete every <file>.orig backup in `d` (commit to the trimmed art). Counts."""
    s = {"removed": 0, "errors": 0}
    for orig in _backups(d).values():
        try:
            os.remove(orig)
            s["removed"] += 1
        except OSError:
            s["errors"] += 1
    return s


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Trim/normalize covers in place, with backup/restore.")
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--cache", default=None, help="cover cache dir (default: <db dir>/.cover-cache)")
    ap.add_argument("--pinned", default=None, help="pinned overrides dir (default: <db dir>/covers-pinned)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--backup", action="store_true",
                    help="with --apply: keep each pre-trim original as <file>.orig (for --restore)")
    ap.add_argument("--restore", action="store_true",
                    help="roll every cover back to its <file>.orig backup, then drop the backup")
    ap.add_argument("--cleanup", action="store_true",
                    help="delete the <file>.orig backups (commit to the trimmed covers)")
    ap.add_argument("--verbose", action="store_true", help="list each cover that changes")
    args = ap.parse_args(argv)

    if args.restore and args.cleanup:
        ap.error("--restore and --cleanup are mutually exclusive")

    dbdir = os.path.dirname(args.db) or "."
    cache = args.cache or os.path.join(dbdir, ".cover-cache")
    pinned = args.pinned or os.path.join(dbdir, "covers-pinned")
    dirs = (("cache ", cache), ("pinned", pinned))

    if args.restore:
        total = {"restored": 0, "errors": 0}
        for label, d in dirs:
            s = restore_dir(d)
            for k in total:
                total[k] += s[k]
            print(f"[{label}] {d}: restored {s['restored']}, errors {s['errors']}")
        print(f"\nRestored {total['restored']} cover(s) from .orig backups.")
        return

    if args.cleanup:
        total = {"removed": 0, "errors": 0}
        for label, d in dirs:
            s = cleanup_dir(d)
            for k in total:
                total[k] += s[k]
            print(f"[{label}] {d}: removed {s['removed']} backup(s), errors {s['errors']}")
        print(f"\nDeleted {total['removed']} .orig backup(s).")
        return

    if args.backup and not args.apply:
        print("note: --backup only takes effect with --apply (a dry run writes nothing).")

    def show(key, before, after):
        if args.verbose:
            print(f"  {key:16} {before/1024:7.0f} KB -> {after/1024:6.0f} KB  "
                  f"(-{(1 - after / before) * 100:4.1f}%)")

    total = {"scanned": 0, "changed": 0, "before": 0, "after": 0, "errors": 0}
    for label, d in dirs:
        print(f"\n[{label}] {d}")
        s = normalize_dir(d, apply=args.apply, backup=args.backup, on_change=show)
        for k in total:
            total[k] += s[k]
        print(f"  scanned {s['scanned']}, {'changed' if args.apply else 'would change'} "
              f"{s['changed']}, errors {s['errors']}")

    before_mb, after_mb = total["before"] / 1e6, total["after"] / 1e6
    saved = before_mb - after_mb
    pct = (saved / before_mb * 100) if before_mb else 0.0
    verb = "Rewrote" if args.apply else "Would rewrite"
    print(f"\n{verb} {total['changed']} of {total['scanned']} covers: "
          f"{before_mb:.1f} MB -> {after_mb:.1f} MB  (saved {saved:.1f} MB, {pct:.0f}%)")
    if args.apply and args.backup and total["changed"]:
        print("Backups kept as <file>.orig — `--restore` to roll back, `--cleanup` to discard them.")
    if not args.apply and total["changed"]:
        print("Dry run — re-run with --apply to write the changes.")


if __name__ == "__main__":
    main()
