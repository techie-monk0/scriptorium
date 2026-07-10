"""Load staged Step-4 artifacts into the DB.

Replays each `staging/holding_<id>.json` (a journal of INSERTs produced by
run_resolve.py) into the DB, one short transaction per file, then moves the
file to `staging/loaded/`. `busy_timeout` (set in catalogue.db_store.connect) lets
this coexist with manual entry in the web app — the load grabs the write
lock only for the brief replay of each book.

Usage:
    python3 run_load.py
    python3 run_load.py --db catalogue-db/catalogue.db --staging-dir staging
"""
from __future__ import annotations

import argparse

# Run from anywhere: this script lives two levels under the repo root (scripts/<bucket>/).
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from catalogue.db_store import connect
from catalogue.services.staging import load_artifacts
from catalogue.db_store import default_db_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--staging-dir", default="staging")
    ap.add_argument("--loaded-dir", default=None,
                    help="where to move loaded files (default: <staging-dir>/loaded)")
    args = ap.parse_args()

    conn = connect(args.db)
    try:
        result = load_artifacts(conn, args.staging_dir, args.loaded_dir)
    finally:
        conn.close()

    if result["loaded"] == 0 and not result["errors"]:
        # Distinguish "nothing staged" from "already done" — a bare "0 writes"
        # reads like a failure, but the usual cause is a re-run: load_artifacts
        # moved every artifact to loaded/ the first time so it can't double-apply.
        from pathlib import Path
        done = Path(args.loaded_dir) if args.loaded_dir else Path(args.staging_dir) / "loaded"
        n_done = len(list(done.glob("holding_*.json"))) if done.is_dir() else 0
        if n_done:
            print(f"load: nothing new to load — {n_done} artifact(s) already applied "
                  f"(in {done}/). Re-run is a no-op by design.")
        else:
            print(f"load: nothing to load — no artifacts in {args.staging_dir}/ "
                  f"(did run_resolve write to a different --staging-dir?)")
        return 0

    print(f"load: {result['loaded']} artifact(s), {result['writes']} write(s) applied")
    if result["errors"]:
        print(f"  {result['errors']} FAILED (left in {args.staging_dir}/):")
        for name, err in result["failures"]:
            print(f"    {name}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
