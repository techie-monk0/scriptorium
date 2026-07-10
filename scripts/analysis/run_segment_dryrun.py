"""Dry-run the gate-OFF segmentation on a curated/representative set of holdings.

Reads each book's sections and runs analyze_book_sections with the verse gate OFF
(the new default) — the labeled-segmentation behaviour — and, for contrast, with the
gate ON (the old auto-detection). Prints the resulting structure + work list.

NO DB WRITES: this only reads files + caches; it never queues or promotes anything.

    python3 run_segment_dryrun.py                 # the curated sample below
    python3 run_segment_dryrun.py 403 45 70       # specific holding ids
"""
import sys
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

# Run from anywhere: this script lives two levels under the repo root (scripts/<bucket>/).
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from catalogue.services.locator import extract_sections
from catalogue.services.book_analysis import analyze_book_sections

DB = "catalogue-db/catalogue.db"

# Curated representative sample, grouped by expected shape × format. The verse
# profile printed per book (mean verse score / fraction of sections scoring ≥0.5)
# shows the actual form, so a label can be sanity-checked against the data.
SAMPLE = {
    "MULTI · root+commentary":           [230, 4, 51],   # 230 verse, 4 mixed, 51 prose sa-bcad
    "MULTI · anthology distinct texts":  [403, 182, 45, 418],
    "MULTI · verse":                     [39, 422],
    "MULTI · mixed prose+verse":         [211, 405],
    "SINGLE · prose":                    [274, 171, 275],
    "SINGLE · verse":                    [57, 344, 400],  # no-TOC standalone → 1 whole-book work
    "SINGLE · mixed prose+verse":        [70, 72, 371],
}
CATEGORY = {hid: cat for cat, hids in SAMPLE.items() for hid in hids}


def toc_entries_for(conn, fh):
    r = conn.execute(
        "SELECT parsed_json FROM parsed_toc_cache WHERE file_hash=? "
        "ORDER BY parse_version DESC LIMIT 1", (fh,)).fetchone()
    if not r or not r[0]:
        return None
    d = json.loads(r[0])
    raw = d.get("entries") if isinstance(d, dict) else d
    if not raw:
        return None
    return [SimpleNamespace(title=e.get("title", ""), page=e.get("page"))
            if isinstance(e, dict) else e for e in raw]


def run(conn, hid):
    row = conn.execute(
        "SELECT h.file_path, h.file_hash, e.title FROM holding h "
        "JOIN edition e ON e.id=h.edition_id WHERE h.id=?", (hid,)).fetchone()
    if not row:
        print(f"  h{hid}: not found"); return
    fp, fh, title = row
    cat = CATEGORY.get(hid, "?")
    basename = Path(fp).name if fp else "(no file)"
    print(f"\n── holding={hid} [{cat}]")
    print(f"   file: {basename}")
    try:
        secs = extract_sections(Path(fp), toc_entries=toc_entries_for(conn, fh)) if fp else None
    except Exception as e:
        print(f"   extract error: {type(e).__name__}: {e}"); return
    if not secs:
        print("   no sections located → would be 1 whole-book work"); return

    verses = [s.verse for s in secs]
    mean_v = sum(verses) / len(verses)
    frac_v = sum(1 for v in verses if v >= 0.5) / len(verses)
    form = ("verse" if frac_v >= 0.66 else "prose" if frac_v <= 0.15 else "mixed")

    off = analyze_book_sections(secs, edition_title=title, ladder=None, enable_verse_gate=False)
    on = analyze_book_sections(secs, edition_title=title, ladder=None, enable_verse_gate=True)
    print(f"   sections={len(secs)} source={secs[0].source}  "
          f"verse≈{mean_v:.2f} (≥0.5 in {frac_v*100:.0f}% → {form})")
    print(f"   gate-ON: {on.structure} ({len(on.contained_texts)} works)  "
          f"| gate-OFF: {off.structure} ({len(off.contained_texts)} works)")
    works = off.contained_texts
    if not works:
        print("   gate-OFF → 1 whole-book work (no distinct-titled sections)")
        return
    for w in works[:14]:
        auth = ", ".join(w.authors) if w.authors else "—"
        nmem = len(w.section_titles)
        print(f"     • {w.title[:58]:<58}  author={auth[:22]:<22} "
              f"kind={w.kind:<10} secs={nmem} @ {w.locator}")
    if len(works) > 14:
        print(f"     … +{len(works) - 14} more works")


def main():
    ids = [int(x) for x in sys.argv[1:]] or [h for hs in SAMPLE.values() for h in hs]
    conn = sqlite3.connect(DB)
    print(f"Dry run (NO writes): gate-OFF segmentation on {len(ids)} holding(s)")
    print("=" * 78)
    for hid in ids:
        run(conn, hid)
    print("\n" + "=" * 78)
    print("Note: authors here come only from deterministic title/attribution parsing")
    print("(ladder=None). The LLM cleanup pass (G3) would enrich authors + merge")
    print("sub-chapters; nav-depth grouping (G2) is not applied, so nested books")
    print("over-segment into chapters. Nothing was written to the DB.")


if __name__ == "__main__":
    main()
