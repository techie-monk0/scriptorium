"""Multi-work segmentation dry-run → the /works/detect cache (kind='multi').

Over the operator-marked `multi_work` editions, runs the deterministic segmenter +
the local-model grouping pass + the configured cloud-model grouping pass (claude or
gemini, per vocab.json `_external_llm`), so you can compare them. WRITES NOTHING canonical.

  caffeinate -i -s python3 -m catalogue.cli.segment_detect private/catalogue-db/catalogue.db
  ... --no-cloud       # skip the cloud model (claude/gemini, per vocab.json _external_llm)
  ... --no-local       # skip gemma (e.g. Ollama not running)
  ... --offline        # skip BDRC per-work canonical lookups (network)
  ... --eid 4 | --limit 5

Live runs hit Ollama + Haiku + BDRC — wrap in `caffeinate -i -s`, and don't run
heavy OCR at the same time as the local model (24 GB swap risk, §4.9).
"""
import argparse

from catalogue.db_store import init_db
from catalogue.services import segment as SEG, work_detect as WD
from catalogue.db_store import default_db_path


def build_clients(*, local=True, cloud=True):
    from catalogue.services.llm import LLMClient, local_llm_config, external_llm_config
    clients = {}
    if local:
        lc = local_llm_config()        # model/base_url from vocab.json `_local_llm`
        try:
            from catalogue.services.llm import ensure_ollama
            ensure_ollama(base_url=lc["base_url"], warm_model=lc["model"])
        except Exception:
            pass
        clients[lc["model"]] = LLMClient(model=lc["model"], base_url=lc["base_url"])
    if cloud:                          # the configured cloud model (claude or gemini)
        from catalogue.cli.work_detect import _external_key_ready
        ec = external_llm_config()
        if _external_key_ready(ec):
            clients[ec["provider"]] = LLMClient(model=ec["model"], base_url=ec["base_url"])
    return clients


def run(db, *, only=None, limit=None, local=True, cloud=True, offline=False, clients=None):
    if only is not None:
        eids = [only]
    else:
        from catalogue.access_api import system_conn
        eids = system_conn(db).editions.reads.multi_work_ids()[: limit or None]
    if clients is None:
        clients = build_clients(local=local, cloud=cloud)
    bdrc = None
    if not offline:
        from catalogue.services.bdrc import BdrcWorkSearch
        bdrc = BdrcWorkSearch().work_search
    classical = WD.live_classical(bdrc_work_search=bdrc)
    # Reuse the same models to gloss each contained work's native title (cached).
    glossers = {label: (lambda text, lang, c=c, lbl=label:
                        WD.cached_gloss(db, text, lang=lang, model_label=lbl, client=c))
                for label, c in clients.items()}
    for eid in eids:
        res = SEG.segment_edition(db, eid, clients=clients, classical=classical, glossers=glossers)
        WD.store_detection(db, eid, "multi", res, commit=False)
    db.commit()
    return len(eids), list(clients)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--eid", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-local", action="store_true", help="skip gemma3:12b")
    ap.add_argument("--no-cloud", action="store_true",
                    help="skip the cloud model (claude/gemini per vocab.json _external_llm)")
    ap.add_argument("--offline", action="store_true", help="skip BDRC canonical lookups")
    ap.add_argument("--force", action="store_true",
                    help="run even though multi_work_detection is off in vocab.json _features")
    args = ap.parse_args(argv)
    from catalogue.services.features import feature_enabled
    if not feature_enabled("multi_work_detection") and not args.force:
        print("multi-work detection is OFF (vocab.json _features.multi_work_detection). "
              "Enable it there, or pass --force to run anyway.")
        return 2
    db = init_db(args.db)
    n, used = run(db, only=args.eid, limit=args.limit, local=not args.no_local,
                  cloud=not args.no_cloud, offline=args.offline)
    print(f"segmented {n} multi-work edition(s) → work_detection "
          f"[methods: deterministic{', ' + ', '.join(used) if used else ''}]")
    print("review at  /works/detect")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
