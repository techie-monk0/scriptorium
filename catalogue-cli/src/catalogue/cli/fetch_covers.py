"""Pre-fetch book covers into the cover cache — by ISBN, no book downloads.

Covers come from Open Library → Google Books (both keyed by ISBN). With the embedded
fallback on (default), books with no online cover have their EPUB's embedded cover pulled
over WebDAV (small EPUBs only; big PDFs skipped). Warms the cache so the shelves render
instantly; the /edition/<id>/cover.jpg route does the same per book on demand.

    python -m catalogue.cli.fetch_covers                  # OL/GB + embedded fallback
    python -m catalogue.cli.fetch_covers --no-embedded    # ISBN-only, never read files
    python -m catalogue.cli.fetch_covers --refresh        # re-try previously-missed covers
    python -m catalogue.cli.fetch_covers --spines         # also warm the spine cache (reuses covers)

The --spines pass builds a constructed spine SVG (spine-e<id>) for every edition, derived
from the cover it just warmed (reused with no extra network), so spine-view shelves render
instantly too. The /edition/<id>/spine.svg route does the same per book on demand.
"""
from __future__ import annotations

import argparse
import os
import sys

from catalogue.db_store import connect
from catalogue.services import covers
from catalogue.db_store import default_db_path


def _acc(db):
    """A system Access over this connection — edition/holding/person reads, engine-routed."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def _first_file_path(acc, eid: int):
    """The edition's first holding that has a file path, or None."""
    for h in acc.holdings.reads.by_edition(eid):
        if h.file_path:
            return h.file_path
    return None


def _default_cache(db_path: str) -> str:
    return os.path.abspath(os.path.join(os.path.dirname(db_path) or ".", ".cover-cache"))


def _default_webdav_cache(db_path: str) -> str:
    return os.path.abspath(os.path.join(os.path.dirname(db_path) or ".", ".webdav-cache"))


def refresh_local(db, cover_cache: str, webdav_cache: str, *, on_progress=None) -> dict:
    """For every book whose file is already on disk (hydrated) or already in the WebDAV
    cache (previously opened), derive its cover from the file's FIRST PAGE — filling a
    missing cover or upgrading a low-res one. Never downloads. Returns counts."""
    from catalogue.services import cloudsync, webdav, covers
    acc = _acc(db)
    rows = [(e.id, e.title) for e in acc.editions.reads.all()]
    s = {"total": len(rows), "filled": 0, "upgraded": 0, "kept": 0, "no_local": 0}
    for i, (eid, title) in enumerate(rows):
        key = f"e{eid}"
        path = _first_file_path(acc, eid)
        readable = None
        if path:
            if os.path.exists(path) and not cloudsync.is_online_only(path):
                readable = path
            else:
                readable = webdav.cached_file(path, webdav_cache)
        if not readable:
            s["no_local"] += 1
            tag = "no-local-file"
        else:
            had = bool(covers.cached_path(cover_cache, key))
            res = covers.refresh_from_file(cover_cache, key, readable)
            if res and had:
                s["upgraded"] += 1; tag = "upgraded"
            elif res:
                s["filled"] += 1; tag = "filled"
            else:
                s["kept"] += 1; tag = "kept (good cover / unrenderable)"
        if on_progress:
            on_progress(i + 1, s["total"], title, tag)
    return s


def _edition_inputs(db, eid: int, isbn, embedded: bool):
    """(isbn, author, file_path) for an edition's cover/spine lookup: ISBN from the edition
    or an edition_isbn alias, the first author's name (for the text placeholder), and the
    first holding's file path — only when embedded-cover sources are allowed."""
    from catalogue.db_store import contributor_store as cs
    acc = _acc(db)
    if not isbn:
        isbn = acc.editions.reads.first_isbn(eid)
    author = ""
    author_ids = cs.edition_author_ids(db, eid)
    if author_ids:
        p = acc.persons.reads.get(author_ids[0])
        author = p.primary_name if p else ""
    path = _first_file_path(acc, eid) if embedded else None
    return isbn, author, path


def fetch_all(db, cache_dir: str, *, embedded: bool = True, refresh: bool = False,
              mounts=None, on_progress=None) -> dict:
    """Fetch a cover for every edition into cache_dir. Returns counts by source."""
    if embedded and mounts is None:
        from catalogue.services import webdav
        mounts = webdav.load_mounts()
    rows = [(e.id, e.title, e.isbn) for e in _acc(db).editions.reads.all()]
    s = {"total": len(rows), "cached": 0, "miss": 0}
    for i, (eid, title, isbn) in enumerate(rows):
        key = f"e{eid}"
        tag = None
        if covers.cached_path(cache_dir, key):
            s["cached"] += 1
            tag = "cached"
        else:
            isbn, author, path = _edition_inputs(db, eid, isbn, embedded)
            if refresh:
                covers._clear_miss(cache_dir, key)
            got = (None if covers.is_missed(cache_dir, key) and not refresh
                   else covers.fetch_cover(isbn, title=title, author=author,
                                           local_path=path, mounts=mounts))
            if got:
                covers.write_cache(cache_dir, key, got[0])
                s[got[1]] = s.get(got[1], 0) + 1
                tag = got[1]
            else:
                covers.mark_miss(cache_dir, key)
                s["miss"] += 1
                tag = "miss"
        if on_progress:
            on_progress(i + 1, s["total"], title, tag)
    return s


def fetch_spines_all(db, cache_dir: str, *, embedded: bool = True, refresh: bool = False,
                     mounts=None, on_progress=None) -> dict:
    """Build a constructed spine SVG for every edition into cache_dir (key spine-e<id>).
    Reuses an already-cached cover (e<id>) with NO network when present; otherwise fetches a
    cover via the same cover layer to derive its colour/art (palette fallback if none). The
    spine cache is independent of the cover cache's lifecycle. Returns counts."""
    if embedded and mounts is None:
        from catalogue.services import webdav
        mounts = webdav.load_mounts()
    rows = [(e.id, e.title, e.isbn) for e in _acc(db).editions.reads.all()]
    s = {"total": len(rows), "cached": 0, "from_cover": 0, "fetched": 0, "palette": 0}
    for i, (eid, title, isbn) in enumerate(rows):
        key = f"spine-e{eid}"
        if covers.cached_path(cache_dir, key) and not refresh:
            s["cached"] += 1
            tag = "cached"
        else:
            isbn2, author, path = _edition_inputs(db, eid, isbn, embedded)
            cover_bytes, cover_path = None, covers.cached_path(cache_dir, f"e{eid}")
            if cover_path:
                try:
                    with open(cover_path, "rb") as f:
                        cover_bytes = f.read()
                except OSError:
                    cover_bytes = None
            if cover_bytes is not None:
                tag = "from-cover"
                s["from_cover"] += 1
            else:                                          # no cached cover → derive one
                got = covers.fetch_cover(isbn2, title=title, author=author,
                                         local_path=path, mounts=mounts)
                cover_bytes = got[0] if got else None
                tag = "fetched" if cover_bytes else "palette"
                s["fetched" if cover_bytes else "palette"] += 1
            covers.write_cache(cache_dir, key,
                               covers.make_spine(title or "Untitled", cover_bytes))
        if on_progress:
            on_progress(i + 1, s["total"], title, tag)
    return s


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Pre-fetch book covers by ISBN (no book downloads).")
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--cache", default=None, help="cover cache dir (default: <db dir>/.cover-cache)")
    ap.add_argument("--no-embedded", action="store_true",
                    help="ISBN sources only; never fetch files for embedded covers")
    ap.add_argument("--refresh", action="store_true", help="re-try previously-missed covers")
    ap.add_argument("--from-files", action="store_true",
                    help="render the first page as the cover for books ALREADY downloaded "
                         "(hydrated or in the WebDAV cache) — fills missing + upgrades "
                         "low-res; no new downloads")
    ap.add_argument("--spines", action="store_true",
                    help="also warm the spine cache (constructed spine SVGs), reusing the "
                         "covers warmed in the same run; no extra network per cached cover")
    args = ap.parse_args(argv)
    cache = args.cache or _default_cache(args.db)

    def _progress(n, total, title, tag):
        print(f"{n}/{total}  [{tag}]  {(title or '')[:55]}", flush=True)

    conn = connect(args.db)
    try:
        if args.from_files:
            s = refresh_local(conn, cache, _default_webdav_cache(args.db), on_progress=_progress)
            print(f"\nfrom-files: filled {s['filled']}, upgraded {s['upgraded']}, "
                  f"kept {s['kept']}, no local file {s['no_local']} (of {s['total']})")
        else:
            s = fetch_all(conn, cache, embedded=not args.no_embedded, refresh=args.refresh,
                          on_progress=_progress)
            have = sum(v for k, v in s.items() if k not in ("total", "miss"))
            by_src = ", ".join(f"{k} {v}" for k, v in sorted(s.items())
                               if k not in ("total", "miss") and v)
            print(f"\ncovers: {have}/{s['total']}  ({by_src}; no cover {s['miss']})")
        if args.spines:
            sp = fetch_spines_all(conn, cache, embedded=not args.no_embedded,
                                  refresh=args.refresh, on_progress=_progress)
            print(f"\nspines: {sp['total'] - sp['cached']}/{sp['total']} built "
                  f"(from cover {sp['from_cover']}, fetched {sp['fetched']}, "
                  f"palette {sp['palette']}; already cached {sp['cached']})")
    finally:
        conn.close()
    print(f"cache → {cache}")


if __name__ == "__main__":
    main()
