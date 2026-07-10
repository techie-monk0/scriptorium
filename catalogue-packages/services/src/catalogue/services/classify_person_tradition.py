"""Person-lineage classifier — READ-ONLY roster pass (writes NOTHING to the DB).

Classifies every authoring person into a tradition from their name, dates, and the
publishers / subjects / titles of the works they authored, then prints the roster
grouped by tradition with an `Unclassified` bucket for anyone the model isn't sure of.
Lineage is a property of the *person*, so one classification settles all their works —
this is the natural complement to the per-work Phase 2 pass, and the right way to reach
authors with no external_id (modern teachers, translators) that the rule map can't key.

It is a pure REPORT: no `person.tradition` / `work_tradition` writes. Reuses the §4.9
ladder (`services.classify.default_ladder`) and does NOT cache (a roster is one-shot).

CLI:
    python -m catalogue.services.classify_person_tradition [db] [--min-works N]
"""
from __future__ import annotations

import argparse
from typing import Optional

from catalogue.db_store.db import init_db
from .classify import Rung, default_ladder, _lenient_json
from .classify_tradition import VOCAB, WRITE_THRESHOLD
from .llm import BudgetExceeded
from catalogue.db_store import default_db_path

UNCLASSIFIED = "Unclassified"

_SYSTEM = (
    "You assign the Buddhist TRADITION / LINEAGE of ONE author, for a Gelug-leaning "
    "Tibetan Buddhist library. Use the author's name, dates, and the publishers, "
    "subjects, and titles of the books they wrote.\n"
    "Choose EXACTLY ONE from this list, or 'Unclassified':\n"
    "  Gelug, Kagyu, Sakya, Nyingma, Shangpa Kagyu, Kadam, Jonang, Common, Unclassified.\n"
    "Guidance:\n"
    "- 'Common' = an Indian source predating the Tibetan schools (Nāgārjuna, Śāntideva, "
    "the mahāsiddhas / dohā authors), or the Buddha — claimed by all schools.\n"
    "- Judge by the author's own lineage, not a book's topic. Publisher is a strong "
    "modern signal (Wisdom/FPMT→Gelug, Rangjung Yeshe/Shambhala→Nyingma or Kagyu, …).\n"
    "- Kadam = Atisha's lojong lineage (a Gelug forerunner); keep it distinct.\n"
    "- Use 'Unclassified' for secular/academic authors, translators with no clear "
    "personal lineage, or anyone you genuinely can't place — do NOT guess Gelug.\n"
    "Respond with ONLY a JSON object, exactly like:\n"
    '{"tradition": "Kagyu", "confidence": 0.8, "evidence": "brief why"}'
)


def _authors(db, *, min_works: int = 1) -> list[dict]:
    """Every authoring person + the signals used to classify them (no writes). Reads via
    the access-API (`acc.tradition_classify`), so this service holds no SQL."""
    from catalogue.access_api import system_conn
    acc = system_conn(db)
    out = []
    for pid, name, dates, xid, trad, n in acc.tradition_classify.authoring_persons(min_works):
        out.append({"id": pid, "name": name, "dates": dates, "external_id": xid,
                    "current": trad, "n_works": n,
                    "publishers": acc.tradition_classify.person_publishers(pid),
                    "subjects": acc.tradition_classify.person_subjects(pid)})
    return out


def _prompt(a: dict) -> str:
    def line(label, v):
        return f"{label}: {v if v else '(none)'}"
    return "\n".join([
        line("Author", a["name"]),
        line("Dates", a["dates"]),
        line("Publishers", ", ".join(a["publishers"])),
        line("Subjects", ", ".join(a["subjects"])),
        f"Works authored: {a['n_works']}",
        "\nWhich tradition?",
    ])


def _parse(content: str) -> tuple[str, float]:
    data = _lenient_json(content)
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict) and d.get("tradition")), None)
    if not isinstance(data, dict):
        return (UNCLASSIFIED, 0.0)
    trad = data.get("tradition")
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    if trad not in VOCAB or conf < WRITE_THRESHOLD:
        return (UNCLASSIFIED, conf)
    return (trad, conf)


def classify_all(db, *, min_works: int = 1,
                 ladder: Optional[list[Rung]] = None) -> dict[str, list[dict]]:
    """Classify every author → {tradition: [ {name, confidence, ...}, … ]} (+ Unclassified).
    Pure read: touches no table beyond SELECTs."""
    ladder = ladder if ladder is not None else default_ladder()
    groups: dict[str, list[dict]] = {t: [] for t in VOCAB}
    groups[UNCLASSIFIED] = []
    for a in _authors(db, min_works=min_works):
        messages = [{"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _prompt(a)}]
        trad, conf = UNCLASSIFIED, 0.0
        for rung in ladder:
            if not rung.available():
                continue
            try:
                resp = rung.client.chat(messages, max_tokens=200)
            except BudgetExceeded:
                raise
            except Exception:
                continue
            trad, conf = _parse(resp["content"])
            if conf >= WRITE_THRESHOLD:
                break
        groups[trad].append({"name": a["name"], "confidence": round(conf, 2),
                             "n_works": a["n_works"], "current": a["current"]})
    return groups


def print_roster(groups: dict[str, list[dict]]) -> None:
    order = VOCAB + [UNCLASSIFIED]
    for trad in order:
        members = groups.get(trad, [])
        if not members:
            continue
        print(f"\n## {trad} ({len(members)})")
        for m in sorted(members, key=lambda x: (-x["n_works"], x["name"])):
            print(f"  - {m['name']}  ({m['n_works']} works, conf {m['confidence']})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--min-works", type=int, default=1)
    args = ap.parse_args(argv)
    db = init_db(args.db)
    print_roster(classify_all(db, min_works=args.min_works))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
