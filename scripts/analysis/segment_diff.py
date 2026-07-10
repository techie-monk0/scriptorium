#!/usr/bin/env python3
"""Dry-run diff: how a book is CURRENTLY catalogued vs how the segmentation engine
would re-segment it (optionally with --toc-hierarchy). Writes nothing.

For each holding it prints:
  • CURRENT  — the work(s) promoted for the holding's edition (edition_work → work).
  • PROPOSED — re-running the engine fresh (degenerate-outline → printed-Contents
    parse, folio/section location, container analysis), no LLM, no cache.

Usage:
  python3 segment_diff.py 45 4 51 230            # holding IDs
  python3 segment_diff.py --toc-hierarchy 4 230  # apply the hierarchy signal
  python3 segment_diff.py --file ~/foo           # read IDs from a file (holding=NN)
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

# Run from anywhere: this script lives two levels under the repo root (scripts/<bucket>/).
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from catalogue.services.book_analysis import analyze_book_sections
from catalogue.services.locator import extract_sections
from catalogue.services.toc import (extract_structured_outline, is_degenerate_outline,
                           parse_contents_index)

DB = "catalogue-db/catalogue.db"


def _front_matter(path: Path, n: int = 30) -> str:
    try:
        import fitz
        doc = fitz.open(path)
        return "\n".join(doc.load_page(i).get_text("text")
                         for i in range(min(n, doc.page_count)))
    except Exception:
        return ""


def _toc_entries(path: Path):
    """Mirror process.py's TOC acquisition: structured outline unless it's degenerate;
    then the deterministic printed-Contents parse (the LLM fallback is skipped here)."""
    entries = extract_structured_outline(path)
    if entries and is_degenerate_outline([e.title for e in entries]):
        entries = None
    if not entries and path.suffix.lower() == ".pdf":
        entries = parse_contents_index(_front_matter(path)) or None
    return entries


def _proposed(path: Path, toc_hierarchy: bool):
    secs = extract_sections(path, toc_entries=_toc_entries(path))
    a = analyze_book_sections(secs or [], ladder=None, toc_hierarchy=toc_hierarchy)
    works = [(w.title, list(w.authors)) for w in a.contained_texts]
    return a.structure, a.source or (secs[0].source if secs else ""), works


def _current(conn, hid: int):
    row = conn.execute("SELECT edition_id FROM holding WHERE id=?", (hid,)).fetchone()
    if not row:
        return []
    ew = conn.execute(
        "SELECT work_id FROM edition_work WHERE edition_id=? ORDER BY sequence",
        (row[0],)).fetchall()
    out = []
    for (wid,) in ew:
        t = conn.execute(
            "SELECT text FROM work_alias WHERE work_id=? "
            "ORDER BY (scheme='english') DESC, id LIMIT 1", (wid,)).fetchone()
        auth = [r[0] for r in conn.execute(
            "SELECT p.primary_name FROM work_contributor wc JOIN person p "
            "ON p.id=wc.person_id WHERE wc.work_id=? AND wc.role='author'", (wid,))]
        out.append((t[0] if t else f"(work {wid})", auth))
    return out


def _fmt(works, limit=None):
    rows = works if limit is None else works[:limit]
    lines = [f"    • {t[:52]:52}  {', '.join(a) if a else '—'}" for t, a in rows]
    if limit is not None and len(works) > limit:
        lines.append(f"    … +{len(works) - limit} more")
    return lines or ["    (none)"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", type=int)
    ap.add_argument("--toc-hierarchy", action="store_true")
    ap.add_argument("--file", help="read 'holding=NN' IDs from a file")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--limit", type=int, default=12, help="max works shown per side")
    args = ap.parse_args()

    ids = list(args.ids)
    if args.file:
        txt = Path(args.file).expanduser().read_text(errors="replace")
        ids += [int(m) for m in re.findall(r"holding=(\d+)", txt)]
    ids = list(dict.fromkeys(ids))
    if not ids:
        ap.error("no holding IDs (pass them as args or via --file)")

    conn = sqlite3.connect(args.db)
    print(f"segment diff  (toc_hierarchy={args.toc_hierarchy})  db={args.db}\n")
    for hid in ids:
        row = conn.execute("SELECT file_path FROM holding WHERE id=?", (hid,)).fetchone()
        if not row or not row[0]:
            print(f"── holding {hid}: no file\n"); continue
        path = Path(row[0])
        cur = _current(conn, hid)
        if not path.exists():
            print(f"── holding {hid}: {path.name}\n   FILE MISSING — current "
                  f"catalogue has {len(cur)} work(s)\n"); continue
        try:
            structure, source, prop = _proposed(path, args.toc_hierarchy)
        except Exception as e:
            print(f"── holding {hid}: {path.name}\n   ENGINE ERROR: {e}\n"); continue

        cur_titles = {t.strip().lower() for t, _ in cur}
        prop_titles = {t.strip().lower() for t, _ in prop}
        added = len(prop_titles - cur_titles)
        removed = len(cur_titles - prop_titles)
        verdict = "SAME" if cur_titles == prop_titles else f"DIFF (+{added}/-{removed})"

        print(f"── holding {hid}: {path.name}")
        print(f"   {verdict}   current={len(cur)} work(s)  →  "
              f"proposed={len(prop) or 1} ({structure}, {source})")
        print("   CURRENT:")
        print("\n".join(_fmt(cur, args.limit)))
        print("   PROPOSED:")
        print("\n".join(_fmt(prop, args.limit) if prop
                        else ["    • [whole book — single work]"]))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
