"""Build the full-text "Content search" index (`edition_text` + its FTS mirror).

The content search (`/search` → `edition_text_fts`) needs one `edition_text` row per
holding's body text. That text is ALREADY cached in `raw_extract_cache` (keyed by
`file_hash`, one row per extract version) from earlier sweeps — it was simply never
promoted into the search index. This CLI does that promotion **DB-to-DB only**: it copies
the latest cached extraction for each holding into `edition_text`, and the FTS mirror builds
itself via the `edition_text_ai` AFTER INSERT trigger. No file access, no re-OCR — so it
runs offline and fast.

Holdings whose `file_hash` has no cached `raw_text` are skipped (their text is captured when
the sweep/OCR pass next runs them). Resumable: an edition that already has `edition_text`
rows is left alone unless `--rebuild`, and each edition commits on its own.

    python -m catalogue.cli.build_content_index [--db PATH] [--rebuild] [--limit N]
"""
from __future__ import annotations

import argparse
import unicodedata
from collections import defaultdict
from typing import Optional

from catalogue.db_store import connect
from catalogue.db_store import default_db_path

# Chunking: the cached text is whole-file (and wildly inconsistent — some EPUBs are one
# multi-MB run with almost no line breaks, some PDFs have paragraphs, few have form-feeds),
# so we can't split on a delimiter. Fixed word-windows give a uniform ~paragraph-sized
# passage per row regardless of source, so a book yields MANY independent FTS hits (distinct
# sentences/paragraphs) instead of one giant document. A small overlap keeps a phrase that
# straddles a window boundary findable.
CHUNK_WORDS = 180
CHUNK_OVERLAP = 20


def _chunks(text: str, words: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping word-windows. Collapses all whitespace (fine for a
    search snippet); returns [] for empty/whitespace input."""
    toks = text.split()
    if not toks:
        return []
    step = max(1, words - overlap)
    out = []
    for i in range(0, len(toks), step):
        out.append(" ".join(toks[i:i + words]))
        if i + words >= len(toks):
            break
    return out


def build_index(conn, *, rebuild: bool = False, limit: Optional[int] = None) -> dict:
    """Promote cached extractions into `edition_text`. Returns a stats dict."""
    if rebuild:
        conn.execute("DELETE FROM edition_text")   # FTS clears via the AFTER DELETE trigger
        conn.commit()

    rows = conn.execute(
        "SELECT edition_id, file_hash FROM holding "
        "WHERE file_hash IS NOT NULL AND TRIM(file_hash) <> '' AND edition_id IS NOT NULL "
        "ORDER BY edition_id, id"
    ).fetchall()
    by_edition: dict[int, list[str]] = defaultdict(list)
    for eid, fh in rows:
        by_edition[eid].append(fh)

    stats = {"editions": len(by_edition), "indexed": 0, "already": 0,
             "no_cached_text": 0, "chunks": 0}
    for eid, hashes in by_edition.items():
        if limit is not None and stats["indexed"] >= limit:
            break
        if not rebuild and conn.execute(
                "SELECT 1 FROM edition_text WHERE edition_id = ? LIMIT 1", (eid,)).fetchone():
            stats["already"] += 1
            continue
        chunks: list[str] = []
        for fh in hashes:
            r = conn.execute(
                "SELECT raw_text FROM raw_extract_cache WHERE file_hash = ? "
                "ORDER BY extract_version DESC LIMIT 1", (fh,)).fetchone()
            if r and (r[0] or "").strip():
                chunks.extend(_chunks(unicodedata.normalize("NFC", r[0])))
        if not chunks:
            stats["no_cached_text"] += 1
            continue
        # Rewrite this edition's rows atomically (idempotent re-runs). `page` is the chunk
        # ordinal — a stable passage index, not a real printed page (the cache isn't
        # paginated); it just distinguishes the matching passages within a book.
        conn.execute("DELETE FROM edition_text WHERE edition_id = ?", (eid,))
        for i, chunk in enumerate(chunks, 1):
            conn.execute(
                "INSERT INTO edition_text (edition_id, page, content) VALUES (?, ?, ?)",
                (eid, i, chunk))
        conn.commit()                 # per-edition → resumable
        stats["indexed"] += 1
        stats["chunks"] += len(chunks)
    return stats


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Build the content-search FTS index from cached extractions (no re-OCR).")
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--rebuild", action="store_true",
                    help="clear and rebuild the whole index (otherwise resume: skip indexed editions)")
    ap.add_argument("--limit", type=int, default=None, help="index at most N new editions")
    args = ap.parse_args(argv)
    conn = connect(args.db)
    s = build_index(conn, rebuild=args.rebuild, limit=args.limit)
    print(f"editions with a file: {s['editions']}")
    print(f"  indexed now:     {s['indexed']}  ({s['chunks']} passages)")
    print(f"  already indexed: {s['already']}")
    print(f"  no cached text:  {s['no_cached_text']}  (need a sweep/OCR pass)")


if __name__ == "__main__":
    main()
