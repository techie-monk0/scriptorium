"""Tradition classifier — Phase 2 (Claude adjudication of the low-confidence tail).

Phase 1 (catalogue.db_store.migrate_tradition) tags every work by deterministic
rules; the works it could only reach via the presumed-Gelug fallback (source
'rule-default', confidence 0.5) — or any work whose best rule confidence is below
`threshold` — are the ambiguous tail this pass adjudicates with the LLM.

It reuses the §4.9 ladder (local Ollama rung → Claude Haiku, `services.classify.
default_ladder`) and the `classification_cache` memo, exactly like the TOC classifier
— cache-first, climb on low confidence, never re-bill a settled entry. The model picks
from the FLAT 9-term tradition vocabulary (never a nested path) and returns a scope
(school / common / cross) so the "Gelug-unless-marked" default is made explicit and
recorded. A confident verdict REPLACES the work's rule-% rows with `source='llm'`
rows (+ confidence + evidence); an unconfident one is left for Phase 3 (human review).

Safe to re-run: only rows the classifier itself wrote (`source='llm'`) or the rule
fallback are touched — `source='human'` verdicts are never overwritten.

CLI:
    python -m catalogue.services.classify_tradition report [db] [--threshold T]
    python -m catalogue.services.classify_tradition run    [db] [--threshold T] [--limit N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from typing import Optional

from catalogue.db_store.db import init_db
from catalogue.db_store.migrate_tradition import AUTHOR_TRADITION
from .classify import Rung, default_ladder, _lenient_json
from .llm import BudgetExceeded
from catalogue.db_store import default_db_path

# Distinct cache namespace from the TOC classifier (which uses version 1) so the two
# never collide on a shared content_hash. Bump when the prompt or vocab changes.
CLASSIFY_VERSION = 100
WRITE_THRESHOLD = 0.6      # below this the LLM verdict is left for human review, not written

VOCAB = ["Gelug", "Kagyu", "Sakya", "Nyingma", "Shangpa Kagyu",
         "Kadam", "Jonang", "Common", "Rimé"]

_SYSTEM = (
    "You assign the Tibetan Buddhist TRADITION(S) of ONE book from a Gelug-leaning "
    "library. Traditions are a separate axis from topic — a book on Emptiness, Lamrim, "
    "or Tantra still belongs to a school (or is common to all).\n"
    "\nDecide in two stages:\n"
    "1. SCOPE — is this school-specific, or shared?\n"
    "   - 'common': pan-Buddhist / foundational / an Indian source that predates the "
    "Tibetan schools (Nāgārjuna, Śāntideva, Asaṅga, most mahāsiddha dohās).\n"
    "   - 'cross': explicitly non-sectarian / Rimé (spans schools by design).\n"
    "   - 'school': belongs to one (or a few) specific schools.\n"
    "2. TRADITION(S) — choose from this EXACT list, nothing else:\n"
    "   Gelug, Kagyu, Sakya, Nyingma, Shangpa Kagyu, Kadam, Jonang, Common, Rimé.\n"
    "   Signals: author's lineage (strongest), deity/practice (Yamantaka/Lama Chöpa→Gelug; "
    "Mahāmudrā/Vajrayoginī→Kagyu; Lamdré/Hevajra→Sakya; Dzogchen→Nyingma), publisher, "
    "terminology. If scope is 'common' use tradition ['Common']; if 'cross' use ['Rimé'].\n"
    "   DEFAULT: this is a Gelug library — if it is clearly school-specific but no school "
    "signal points elsewhere, answer ['Gelug']. Do NOT default to Gelug for common/Indian "
    "source material.\n"
    "\nRespond with ONLY a JSON object, exactly like:\n"
    '{"traditions": ["Gelug"], "scope": "school", "confidence": 0.8, '
    '"evidence": "brief why"}\n'
    "confidence is 0.0-1.0 (below 0.6 = uncertain, a human will review). traditions is "
    "a non-empty array drawn ONLY from the list above."
)


# ── candidate selection ──────────────────────────────────────────────────────
def candidates(db, *, threshold: float = WRITE_THRESHOLD) -> list[int]:
    """work_ids whose tradition is still only a low-confidence RULE guess — every row
    is `source LIKE 'rule-%'` and the best confidence is < threshold. These are the
    presumed-Gelug (0.5) works plus any weak subject-only hits. Works carrying a
    human/llm verdict, or a confident rule, are settled and skipped."""
    from catalogue.access_api import system_conn
    return system_conn(db).tradition_classify.rule_only_candidates(threshold)


# ── context building ─────────────────────────────────────────────────────────
def _context(db, work_id: int) -> dict:
    """The signals fed to the model for one work: titles, subjects, authors (with any
    known lineage hint), publishers. Reads via the access-API; the author lineage hint
    (the curated `AUTHOR_TRADITION` map) is applied here, above the data layer."""
    from catalogue.access_api import system_conn
    sig = system_conn(db).tradition_classify.work_signals(work_id)
    authors = []
    for name, xid in sig["author_rows"]:
        hint = AUTHOR_TRADITION.get(xid or "")
        authors.append(f"{name} (lineage: {hint})" if hint else name)
    return {"titles": sig["titles"] + sig["natives"], "subjects": sig["subjects"],
            "authors": authors, "publishers": sig["publishers"]}


def _user_prompt(ctx: dict) -> str:
    def line(label, xs):
        return f"{label}: {', '.join(xs) if xs else '(none)'}"
    return "\n".join([
        line("Title(s)", ctx["titles"]),
        line("Author(s)", ctx["authors"]),
        line("Subjects", ctx["subjects"]),
        line("Publisher(s)", ctx["publishers"]),
        "\nAssign the tradition(s).",
    ])


def _content_hash(ctx: dict) -> str:
    return hashlib.sha256(
        json.dumps(ctx, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ── parse ────────────────────────────────────────────────────────────────────
def _parse(content: str) -> Optional[dict]:
    """Parse the LLM JSON → {traditions, scope, confidence, evidence}, keeping only
    tradition names in VOCAB. Returns None on unusable output so the caller advances."""
    data = _lenient_json(content)
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict) and d.get("traditions")), None)
    if not isinstance(data, dict):
        return None
    trads = [t for t in (data.get("traditions") or []) if t in VOCAB]
    if not trads:
        return None
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    return {"traditions": trads, "scope": data.get("scope", ""),
            "confidence": conf, "evidence": str(data.get("evidence", ""))[:200]}


# ── one work ─────────────────────────────────────────────────────────────────
def classify_work(db, work_id: int, *, ladder: Optional[list[Rung]] = None) -> Optional[dict]:
    """Cache-first, then climb the ladder for one work. Returns the parsed verdict
    (also cached), or None if no rung produced a usable answer."""
    from catalogue.access_api import system_conn
    ctx = _context(db, work_id)
    chash = _content_hash(ctx)
    acc = system_conn(db)
    cached = acc.classification_cache.get(chash, CLASSIFY_VERSION)
    if cached and cached[0]:
        raw = json.loads(cached[0])
        raw["confidence"] = cached[1] or raw.get("confidence", 0.0)
        return raw

    ladder = ladder if ladder is not None else default_ladder()
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _user_prompt(ctx)}]
    verdict, rung_name = None, "none"
    for rung in ladder:
        if not rung.available():
            continue
        try:
            resp = rung.client.chat(messages, max_tokens=256)
        except BudgetExceeded:
            raise
        except Exception:
            continue
        parsed = _parse(resp["content"])
        rung_name = rung.name
        if parsed:
            verdict = parsed
            if parsed["confidence"] >= WRITE_THRESHOLD:
                break            # confident — stop climbing
    if verdict is None:
        return None
    acc.classification_cache.put(chash, CLASSIFY_VERSION,
                                 json.dumps(verdict), verdict["confidence"], rung_name)
    return verdict


# ── write ────────────────────────────────────────────────────────────────────
def _tradition_ids(db) -> dict[str, int]:
    from catalogue.access_api import system_conn
    return system_conn(db).tradition_classify.tradition_name_ids()


def _write_verdict(db, work_id: int, verdict: dict, tids: dict[str, int]) -> int:
    """Replace the work's rule-% rows with the LLM verdict (one row per tradition).
    Preserves any human rows via INSERT OR IGNORE. Returns rows written. Stages via the
    access-API; the caller (`run`) owns the commit."""
    from catalogue.access_api import system_conn
    ev = f"scope={verdict['scope']}; {verdict['evidence']}"
    return system_conn(db).tradition_classify.replace_rule_rows_with_llm(
        work_id, [tids[t] for t in verdict["traditions"]], verdict["confidence"], ev)


def run(db, *, threshold: float = WRITE_THRESHOLD, limit: Optional[int] = None,
        ladder: Optional[list[Rung]] = None, commit: bool = True) -> dict:
    """Adjudicate the low-confidence tail. Confident verdicts (≥ WRITE_THRESHOLD) are
    written as source='llm'; unconfident ones are left in place for Phase 3."""
    cands = candidates(db, threshold=threshold)
    dropped = 0
    if limit is not None and len(cands) > limit:
        dropped = len(cands) - limit
        cands = cands[:limit]
    tids = _tradition_ids(db)
    written = attempted = confident = failed = 0
    for wid in cands:
        attempted += 1
        try:
            verdict = classify_work(db, wid, ladder=ladder)
        except BudgetExceeded:
            break                # hard stop — leave the rest for a later run
        if verdict is None:
            failed += 1
            continue
        if verdict["confidence"] >= WRITE_THRESHOLD:
            confident += 1
            written += _write_verdict(db, wid, verdict, tids)
    if commit:
        db.commit()
    out = {"candidates": len(cands), "attempted": attempted,
           "confident_written": confident, "rows_written": written,
           "left_for_review": attempted - confident - failed, "no_answer": failed}
    if dropped:
        out["skipped_over_limit"] = dropped     # never silently truncate
    return out


def report(db, *, threshold: float = WRITE_THRESHOLD) -> dict:
    """Dry preview: how many works the run would adjudicate (no LLM calls, no writes)."""
    from catalogue.access_api import system_conn
    cands = candidates(db, threshold=threshold)
    example = [dict(work_id=w, **_context(db, w)) for w in cands[:3]]
    return {"candidates": len(cands), "threshold": threshold,
            "cached": system_conn(db).tradition_classify.cached_count(CLASSIFY_VERSION),
            "examples": example}


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("report", "run"):
        p = sub.add_parser(name)
        p.add_argument("db", nargs="?", default=default_db_path())
        p.add_argument("--threshold", type=float, default=WRITE_THRESHOLD)
        if name == "run":
            p.add_argument("--limit", type=int, default=None,
                           help="cap works adjudicated this run (cost control)")
    args = ap.parse_args(argv)

    db = init_db(args.db)
    if args.cmd == "report":
        res = report(db, threshold=args.threshold)
    else:
        res = run(db, threshold=args.threshold, limit=args.limit)
    for k, v in res.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
