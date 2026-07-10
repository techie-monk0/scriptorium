"""Parallel Step-4 resolver → per-book staging files (no DB writes).

Each book takes minutes (LLM ladder + BDRC/84000 HTTP) and SQLite has a
single writer, so writing straight to the DB serializes everything and
locks out manual entry. Instead, N worker processes resolve disjoint shards
of holdings and dump each book's writes to `staging/holding_<id>.json`.
Cache lookups still read the live DB (free under WAL). Nothing here takes
the write lock.

Load the results into the DB afterwards with `run_load.py`.

Usage:
    CATALOGUE_RESOLVER=live python3 run_resolve.py --workers 4
    CATALOGUE_RESOLVER=live python3 run_resolve.py --workers 4 --limit 40 --shuffle
    python3 run_resolve.py --workers 4 --force        # re-stage even if a file exists
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import random
import time
from types import SimpleNamespace

import os

# Run from anywhere: this script lives two levels under the repo root (scripts/<bucket>/).
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from catalogue.services.classify import default_ladder
from catalogue.db_store import connect
from catalogue.services.llm import ensure_ollama
from catalogue.services.process import ProcessConfig, process_holding, apply_volume_preset
from catalogue.services.skip import is_skipped
from catalogue.services.staging import StagingConn, artifact_path, write_artifact
from catalogue.db_store import default_db_path


def select_holdings(db, *, status, only_ids, skip_ids, shuffle, seed, limit):
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
            (status,),
        ).fetchall()
    rows = [tuple(r) for r in rows
            if r[0] not in skip_ids and not is_skipped(r[1], r[2])]
    if shuffle:
        rng = random.Random(seed) if seed is not None else random.Random()
        rng.shuffle(rows)
    if limit:
        rows = rows[:limit]
    return rows


def _label(title, file_path, hid):
    label = (title or "").strip() or (file_path or "").rsplit("/", 1)[-1] or f"holding {hid}"
    return label[:69] + "..." if len(label) > 72 else label


def _boolflag(s) -> bool:
    return str(s).strip().lower() not in ("false", "0", "no", "off")


def worker(worker_id: int, shard: list[tuple], db_path: str,
           staging_dir: str, force: bool, enable_verse_gate: bool = False,
           toc_hierarchy: bool = False, title_by_author: bool = True,
           title_with_possessive: bool = True) -> None:
    conn = connect(db_path)          # WAL reader; StagingConn blocks all writes
    # Built per-process (LLM clients aren't picklable). The ladder MUST be set
    # explicitly: book analysis + the contributor title-page verifier gate the
    # LLM on a non-None ladder (unlike classify, which defaults internally), so
    # without this the anchor author-peek and title-page reconciliation silently
    # no-op and every book falls back to filename hints (verified=False).
    cfg = ProcessConfig(use_text_layer_toc=True, analyze_book=True,
                        ladder=default_ladder(), enable_verse_gate=enable_verse_gate,
                        toc_hierarchy=toc_hierarchy, title_by_author=title_by_author,
                        title_with_possessive=title_with_possessive)
    n = len(shard)
    for i, (hid, title, file_path) in enumerate(shard, 1):
        if artifact_path(staging_dir, hid).exists() and not force:
            print(f"[w{worker_id} {i}/{n}] holding={hid} skip (already staged)", flush=True)
            continue
        label = _label(title, file_path, hid)
        t0 = time.time()
        print(f"[w{worker_id} {i}/{n}] holding={hid} {label} starting...", flush=True)
        sc = StagingConn(conn)
        try:
            rep = process_holding(sc, hid, cfg)
        except Exception as exc:  # noqa: BLE001 — one bad book must not kill the worker
            print(f"[w{worker_id} {i}/{n}] holding={hid} ERROR in "
                  f"{time.time() - t0:5.1f}s: {exc.__class__.__name__}: {exc}", flush=True)
            continue
        write_artifact(staging_dir, hid, sc.writes, report={
            "extracted_entries": rep.extracted_entries,
            "classifications": len(rep.classifications),
            "queued_for_digitization": rep.queued_for_digitization,
            "queued_low_confidence": rep.queued_low_confidence,
            "cached_toc": rep.cached_toc,
        })
        print(f"[w{worker_id} {i}/{n}] holding={hid} STAGED {len(sc.writes)} writes "
              f"(entries={rep.extracted_entries}) in {time.time() - t0:5.1f}s", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--staging-dir", default="staging")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--status", default="ocr_good")
    ap.add_argument("--skip", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--shuffle", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="re-stage holdings even if an artifact already exists")
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

    # Resolve a volume-type preset into the three effective flags before spawning
    # workers (don't build a full ProcessConfig in the parent — that would init a
    # resolver). apply_volume_preset is duck-typed, so a SimpleNamespace suffices.
    eff = SimpleNamespace(toc_hierarchy=args.toc_hierarchy,
                          title_by_author=args.title_by_author,
                          title_with_possessive=args.title_with_possessive)
    apply_volume_preset(eff, single_author_multi_work=args.single_author_multi_work,
                        multi_author=args.multi_author)

    skip_ids = {int(x) for x in args.skip.split(",") if x.strip()}
    only_ids = {int(x) for x in args.only.split(",") if x.strip()}

    db = connect(args.db)
    rows = select_holdings(db, status=args.status, only_ids=only_ids,
                           skip_ids=skip_ids, shuffle=args.shuffle,
                           seed=args.seed, limit=args.limit)
    db.close()

    if not rows:
        print(f"no holdings to resolve (text_status={args.status!r})")
        return 0

    # Start Ollama if needed and warm the local model once, up front, so the
    # workers don't each pay the cold-load (and don't race to spawn servers).
    base_url = os.environ.get("CATALOGUE_LLM_BASE_URL", "http://localhost:11434/v1")
    warm = os.environ.get("CATALOGUE_LLM_MODELS", "gemma3:12b").split(",")[0].strip()
    ensure_ollama(base_url, warm_model=warm, log=lambda m: print(m, flush=True))

    workers = max(1, args.workers)
    shards = [rows[i::workers] for i in range(workers)]
    shards = [s for s in shards if s]

    print(f"resolve: {len(rows)} holding(s) across {len(shards)} worker(s) "
          f"→ {args.staging_dir}/ (db={args.db})")
    for i, s in enumerate(shards):
        print(f"  worker {i}: {len(s)} holding(s)")
    print("-" * 72)

    started = time.time()
    procs = [
        mp.Process(target=worker,
                   args=(i, shard, args.db, args.staging_dir, args.force,
                         args.enable_verse_gate, eff.toc_hierarchy,
                         eff.title_by_author, eff.title_with_possessive),
                   name=f"resolve-w{i}")
        for i, shard in enumerate(shards)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    failed = [p.name for p in procs if p.exitcode not in (0, None)]
    print("-" * 72)
    print(f"resolve done: {len(rows)} holding(s) in {(time.time() - started) / 60:.1f}m")
    print(f"  staged artifacts in: {args.staging_dir}/")
    print(f"  next: python3 run_load.py --db {args.db} --staging-dir {args.staging_dir}")
    if failed:
        print(f"  WARNING: workers exited non-zero: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
