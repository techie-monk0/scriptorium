"""End-to-end: structure-aware section location → peek_section → contained
texts + authors. Covers all three locators (epub-nav, pdf-bookmark, pdf-textlayer).

Run: python3 peek_sections.py            # books 39, 60, 155
"""
from __future__ import annotations
import os, sys
from pathlib import Path

# Run from anywhere: this script lives two levels under the repo root (scripts/<bucket>/).
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from catalogue.db_store import connect
from catalogue.services.process import _read_raw_text
from catalogue.services.locator import extract_sections
from catalogue.services.book_analysis import peek_section
from catalogue.services.classify import default_ladder, parse_toc_region
from catalogue.services.toc import locate_toc_region
from catalogue.services.llm import ensure_ollama


def _p(s=""):
    sys.stdout.write(s + "\n"); sys.stdout.flush()


def run(db, hid, ladder, expect):
    fp, fh = db.execute(
        "SELECT file_path, file_hash FROM holding WHERE id=?", (hid,)).fetchone()
    secs = extract_sections(Path(fp))
    if secs is None:                       # link-less PDF → text-layer locator
        raw = _read_raw_text(db, fh)
        region = locate_toc_region(raw)
        toc = parse_toc_region(region, ladder=ladder) if region else None
        if toc:
            secs = extract_sections(Path(fp), toc_entries=toc)
    _p(f"\n========= holding {hid} =========")
    _p(f"  EXPECT: {expect}")
    if not secs:
        _p("  !! no sections (would need vision, §15)"); return
    _p(f"  {len(secs)} sections via {secs[0].source}")
    contained = []
    for s in secs:
        v = peek_section(s, ladder=ladder)
        tag = {"root": "ROOT", "commentary": "COMM"}.get(v.kind)
        if tag:
            contained.append(v)
            _p(f"    [{tag}] {v.title[:46]:<46} author={v.author!r}  "
               f"(verse={v.verse:.2f}, via={v.via})")
    if not contained:
        _p("    (no reproduced texts → modern_study / teaching)")
    _p(f"  → {len(contained)} contained text(s)")


def main():
    base = os.environ.get("CATALOGUE_LLM_BASE_URL", "http://localhost:11434/v1")
    warm = os.environ.get("CATALOGUE_LLM_MODELS", "gemma3:12b").split(",")[0].strip()
    ensure_ollama(base, warm_model=warm, log=_p)
    ladder = default_ladder()
    db = connect("catalogue-db/catalogue.db")
    run(db, 39, ladder, "Wheel-Weapon + Poison-Peacock → Dharmarakṣita (epub-nav)")
    run(db, 60, ladder, "no contained texts → modern_study (pdf-bookmark)")
    run(db, 155, ladder, "Precious Garland → Nāgārjuna (pdf-textlayer)")


if __name__ == "__main__":
    main()
