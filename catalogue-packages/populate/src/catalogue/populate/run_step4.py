"""Run Step 4 (classify + live resolver) over all clean holdings with
per-holding progress output.

Usage:
    CATALOGUE_RESOLVER=live python3 run_step4.py
    CATALOGUE_RESOLVER=live python3 run_step4.py --limit 10
    CATALOGUE_RESOLVER=live python3 run_step4.py --db catalogue-db/catalogue.db
"""
from __future__ import annotations

import argparse
import random
import sys
import time
import traceback

import os

# Run from anywhere: this script lives two levels under the repo root (scripts/<bucket>/).
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from catalogue.db_store import connect
from catalogue.services.llm import ensure_ollama
from catalogue.services.process import process_holding, ProcessConfig, apply_volume_preset
from catalogue.services.skip import is_skipped
from catalogue.db_store import default_db_path


def _boolflag(s) -> bool:
    return str(s).strip().lower() not in ("false", "0", "no", "off")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N holdings (for smoke testing)")
    ap.add_argument("--status", default="ocr_good",
                    help="text_status filter (default: ocr_good)")
    ap.add_argument("--skip", default="",
                    help="comma-separated holding IDs to skip")
    ap.add_argument("--only", default="",
                    help="comma-separated holding IDs to process (overrides --status)")
    ap.add_argument("--shuffle", action="store_true",
                    help="randomize order (useful with --limit to avoid retrying the same stuck holding)")
    ap.add_argument("--seed", type=int, default=None,
                    help="seed for --shuffle (omit for true randomness each run)")
    ap.add_argument("--enable-verse-gate", action="store_true",
                    help="conservative auto-detection: gate contained-works on verse "
                         "form + strong onset (default OFF — see multi_work_segmentation.md)")
    ap.add_argument("--toc-hierarchy", action="store_true",
                    help="group nested TOC chapters under their top-level part (one work "
                         "per part) instead of one work per chapter (default OFF)")
    ap.add_argument("--title-by-author", nargs="?", const=True, type=_boolflag, default=True,
                    help="parse '<title> by <Skt/Tib author>' from a section title (default true)")
    ap.add_argument("--title-with-possessive", nargs="?", const=True, type=_boolflag, default=True,
                    help="parse \"<Skt/Tib author>'s <title>\" from a section title (default true)")
    preset = ap.add_mutually_exclusive_group()
    preset.add_argument("--single-author-multi-work", action="store_true",
                        help="PRESET: one author, chapters grouped into parts "
                             "(toc_hierarchy on; per-work author parsing off)")
    preset.add_argument("--multi-author", action="store_true",
                        help="PRESET: flat anthology, each section names its author "
                             "(title-by-author/possessive on; hierarchy off; implies multi-work)")
    args = ap.parse_args()

    db = connect(args.db)

    # State for the per-holding live status line.
    state = {"prefix": "", "t0": 0.0}

    def progress(stage: str, cur: int, total: int):
        elapsed = time.time() - state["t0"]
        if total:
            tail = f"{stage} {cur}/{total}"
        else:
            tail = stage
        line = f"{state['prefix']} [{elapsed:5.1f}s] {tail}"
        # Pad to clear any leftover chars from a longer previous line.
        sys.stdout.write("\r" + line.ljust(120))
        sys.stdout.flush()

    cfg = ProcessConfig(progress_cb=progress, use_text_layer_toc=True, analyze_book=True,
                        enable_verse_gate=args.enable_verse_gate,
                        toc_hierarchy=args.toc_hierarchy,
                        title_by_author=args.title_by_author,
                        title_with_possessive=args.title_with_possessive)  # [v14]
    apply_volume_preset(cfg, single_author_multi_work=args.single_author_multi_work,
                        multi_author=args.multi_author)

    skip_ids = {int(x) for x in args.skip.split(",") if x.strip()}
    only_ids = {int(x) for x in args.only.split(",") if x.strip()}

    if only_ids:
        placeholders = ",".join("?" * len(only_ids))
        rows = db.execute(
            f"SELECT h.id, e.title, h.file_path "
            f"FROM holding h JOIN edition e ON e.id = h.edition_id "
            f"WHERE h.id IN ({placeholders}) ORDER BY h.id",
            tuple(only_ids),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT h.id, e.title, h.file_path "
            "FROM holding h JOIN edition e ON e.id = h.edition_id "
            "WHERE h.text_status = ? ORDER BY h.id",
            (args.status,),
        ).fetchall()
    if skip_ids:
        rows = [r for r in rows if r[0] not in skip_ids]
    # Standing rule: skip ANNOTATED books (folder name or title) — catalogue.skip.
    rows = [r for r in rows if not is_skipped(r[1], r[2])]
    if args.shuffle:
        rng = random.Random(args.seed) if args.seed is not None else random.Random()
        rows = list(rows)
        rng.shuffle(rows)
    if args.limit:
        rows = rows[: args.limit]

    total = len(rows)
    if total == 0:
        print(f"no holdings with text_status={args.status!r}")
        return 0

    print(f"step4: {total} holding(s) to process "
          f"(text_status={args.status!r}, db={args.db})")
    print(f"step4: resolver = {type(cfg.resolver).__name__}")
    print("-" * 72)

    # Start Ollama if needed and warm the local model so the first holding
    # doesn't pay the cold-load.
    base_url = os.environ.get("CATALOGUE_LLM_BASE_URL", "http://localhost:11434/v1")
    warm = os.environ.get("CATALOGUE_LLM_MODELS", "gemma3:12b").split(",")[0].strip()
    ensure_ollama(base_url, warm_model=warm, log=lambda m: print(m, flush=True))

    started = time.time()
    cached = extracted = queued_dig = queued_low = errors = 0
    total_classifications = 0

    for i, (hid, title, file_path) in enumerate(rows, 1):
        label = (title or "").strip() or (file_path or "").rsplit("/", 1)[-1] or f"holding {hid}"
        if len(label) > 72:
            label = label[:69] + "..."

        state["prefix"] = f"[{i:>4}/{total}] holding={hid} {label}"
        state["t0"] = time.time()
        print(f"{state['prefix']} starting...", flush=True)
        t0 = state["t0"]
        try:
            rep = process_holding(db, hid, cfg)
        except Exception as exc:
            errors += 1
            elapsed = time.time() - t0
            sys.stdout.write("\r" + " " * 120 + "\r")  # clear status line
            print(f"[{i:>4}/{total}] holding={hid} ERROR in {elapsed:5.1f}s: "
                  f"{exc.__class__.__name__}: {exc}", flush=True)
            traceback.print_exc(limit=2)
            continue

        elapsed = time.time() - t0
        if rep.cached_toc:
            cached += 1
        if rep.queued_for_digitization:
            queued_dig += 1
        if rep.queued_low_confidence:
            queued_low += 1
        total_classifications += len(rep.classifications)
        extracted += rep.extracted_entries

        if rep.queued_for_digitization:
            status = "QUEUED-DIGITIZE"
        elif rep.queued_low_confidence:
            status = "DONE (low-confidence flagged)"
        elif rep.cached_toc:
            status = "DONE (cached)"
        else:
            status = "DONE"

        wall = time.time() - started
        rate = i / wall if wall > 0 else 0.0
        eta_s = (total - i) / rate if rate > 0 else 0.0
        eta_min = eta_s / 60

        sys.stdout.write("\r" + " " * 120 + "\r")  # clear status line
        print(f"[{i:>4}/{total}] holding={hid} {status} in {elapsed:5.1f}s: "
              f"entries={rep.extracted_entries} "
              f"classified={len(rep.classifications)} "
              f"structure={rep.book_structure} works={rep.n_works} "
              f"| eta {eta_min:5.1f}m", flush=True)
        # Per-work listing (title + author), so the segmentation is visible per book.
        for w in rep.works:
            auths = ", ".join(w.get("authors") or [])
            tag = " (book-level)" if w.get("author_inherited") and auths else \
                  (" (no author)" if not auths else "")
            kind = w.get("kind") or ""
            print(f"        • {(w.get('title') or '')[:60]:60}  "
                  f"[{kind}] {auths}{tag}", flush=True)

    wall = time.time() - started
    print("-" * 72)
    print(f"step4 done: {total} holdings in {wall/60:.1f}m")
    print(f"  cached-toc:       {cached}")
    print(f"  total entries:    {extracted}")
    print(f"  classifications:  {total_classifications}")
    print(f"  queued digitize:  {queued_dig}")
    print(f"  queued lowconf:   {queued_low}")
    print(f"  errors:           {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
