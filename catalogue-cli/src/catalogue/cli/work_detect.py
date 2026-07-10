"""Dry-run works detection → the /works/detect review cache. WRITES NOTHING to the
canonical works; only fills the read-only `work_detection` table you then verify in
the web report before anything is applied.

Part B (single-work editions) runs here. Part C (multi-work segmentation) is added
by catalogue/cli/segment_detect.py.

  python3 -m catalogue.cli.work_detect DB                 # all single_work editions
  python3 -m catalogue.cli.work_detect DB --limit 20      # first 20 (a quick look)
  python3 -m catalogue.cli.work_detect DB --eid 312       # one edition

Long live runs hit the 84000 Toh index (+ BDRC for Tibetan); wrap in
`caffeinate -i -s` so the Mac doesn't sleep.
"""
import argparse
import os
from pathlib import Path

from catalogue.db_store import init_db
from catalogue.services import work_detect as WD
from catalogue.db_store import default_db_path

def _load_api_key():
    """Anthropic key via the shared resolver: ANTHROPIC_API_KEY env, else the git-ignored
    api_key.txt (a bare single line OR an `ANTHROPIC_API_KEY=...` line). Never logged."""
    from catalogue.services import apikeys
    k = apikeys.get("ANTHROPIC_API_KEY")
    return k.strip() if k else None


def _external_key_ready(ec):
    """Ensure the chosen provider's API key is in env; return True if usable.
    Claude falls back to api_key.txt; Gemini accepts GOOGLE_API_KEY or GEMINI_API_KEY
    (e.g. exported in .zshrc)."""
    if ec["provider"] == "gemini":
        return bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        k = _load_api_key()
        if k:
            os.environ["ANTHROPIC_API_KEY"] = k
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _build_glossers(db, *, local=True, external=True, model=None):
    """A `{label: glosser_fn}` dict — the local model + the configured cloud model
    (Claude or Gemini, per vocab.json `_external_llm`), so you can compare their
    Tibetan/Sanskrit renderings. Each glosser is CACHE-BACKED (gloss_cache), so a
    title is glossed once per model. A model that can't be reached (Ollama down /
    no API key) is silently dropped."""
    from catalogue.services.llm import LLMClient, local_llm_config, external_llm_config
    clients = {}
    if local:
        lc = local_llm_config()
        try:
            from catalogue.services.llm import ensure_ollama
            ensure_ollama(base_url=lc["base_url"], warm_model=lc["model"])
            clients[lc["model"]] = LLMClient(model=lc["model"], base_url=lc["base_url"])
        except Exception:
            pass
    if external:
        ec = external_llm_config()
        if _external_key_ready(ec):
            clients[ec["provider"]] = LLMClient(model=model or ec["model"],
                                                base_url=ec["base_url"])
    return {label: (lambda text, lang, c=c, lbl=label:
                    WD.cached_gloss(db, text, lang=lang, model_label=lbl, client=c))
            for label, c in clients.items()}


def run(db, *, only=None, limit=None, offline=False, gloss=False,
        gloss_model=None, glossers=None):
    if only is not None:
        eids = [only]
    else:
        from catalogue.access_api import system_conn
        eids = system_conn(db).editions.reads.single_work_ids()[: limit or None]
    # Live: 84000 Toh index (offline, if the snapshot is present) + BDRC work search
    # (network). `--offline` drops BDRC, so it runs from the Toh snapshot + title text
    # only (no network).
    bdrc_search = None
    if not offline:
        from catalogue.services.bdrc import BdrcWorkSearch
        bdrc_search = BdrcWorkSearch().work_search
    resolve = WD.live_classical(bdrc_work_search=bdrc_search)
    pidx = WD.build_proposal_index(db)         # 'detected from book' contributor column
    if glossers is None and gloss:
        glossers = _build_glossers(db, model=gloss_model)
    counts = {"classical": 0, "modern": 0, "low_conf": 0}
    for eid in eids:
        res = WD.detect_single(db, eid, classical=resolve, proposal_index=pidx, glossers=glossers)
        if res is None:
            continue
        WD.store_detection(db, eid, "single", res, commit=False)
        counts[res["determination"]] += 1
        if res["confidence"] < 0.5:
            counts["low_conf"] += 1
    db.commit()
    return len(eids), counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--eid", type=int, default=None, help="detect a single edition id")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--offline", action="store_true",
                    help="skip BDRC (network); use the 84000 Toh snapshot + title text only")
    ap.add_argument("--gloss", action="store_true",
                    help="rough English gloss of the Tibetan/Sanskrit title when the authority "
                         "gives no English — runs the local model + the configured cloud model "
                         "(vocab.json _external_llm: claude or gemini) so you can compare them")
    ap.add_argument("--gloss-model", default=None,
                    help="override the cloud model id (else vocab.json _external_llm)")
    args = ap.parse_args(argv)
    db = init_db(args.db)
    n, counts = run(db, only=args.eid, limit=args.limit, offline=args.offline,
                    gloss=args.gloss, gloss_model=args.gloss_model)
    print(f"detected {n} single-work edition(s) → work_detection "
          f"[{counts['classical']} classical · {counts['modern']} modern · "
          f"{counts['low_conf']} low-confidence]")
    print("review at  /works/detect")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
