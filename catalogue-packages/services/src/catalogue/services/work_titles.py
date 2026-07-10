"""Re-derive real book/work titles from the cached title-page TEXT.

The ingest pass (catalogue/sweep.py) seeds `edition.title` from the FILENAME
(`path.stem`), so titles arrive full of junk — author names, publisher strings,
hashes, and library codes ("LTK", "HHDL", "LZR") — e.g.
  "LTK - Great Exposition of Secret Mantra vol 1"   (real title: that text,
   but the work's first alias was even wrong: "Reasons for Faith").
A clean, real title is what every downstream match keys on: the work→author
resolver searches it (catalogue/work_authority.py), the edition verifier diffs
it, and people read it. So we re-derive it from the actual title-page text we
ALREADY have cached in `raw_extract_cache` — no rescan, no re-OCR.

Reuses the SAME LLM ladder as the contributor pass (catalogue/classify.run_ladder
→ catalogue/llm.LLMClient; local gemma3:12b by default, Claude Haiku fallback).
The ladder is injectable so tests run a fake LLM offline.

Policy (locked with the user):
  • CONFIDENCE-GATED replace. High confidence (≥ threshold) → replace the title
    NOW and queue a `title_proposal` review item so it's still reviewable. Low
    confidence → queue ONLY, leave the title untouched until a human confirms.
  • Replace = set `edition.title` AND the work's primary `work_alias`; the old
    filename-derived title is KEPT as a `scheme='filename'` alias (no data loss,
    §4.2). A native-script title (Tibetan/Sanskrit) found on the page is added as
    an extra alias so the work→author search can match across scripts.
  • Accept = confirm (apply now if it was a low-confidence queue-only item).
    Reject = revert to the old title (undo an auto-applied replacement).
  • Processes EVERY edition: the 364 with a contained work (title + work alias)
    and the ~85 work-less books (edition.title only — there is no edition_alias
    table, so for those the old title is preserved in the review payload).

Mirrors the other authority passes: `(db, item_id, *, commit=True) -> bool`
accept/reject contract, a `main()` CLI, and a `derive_all_*` walk.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

# Mojibake signature: characters that don't belong in a romanized/IAST/Tibetan
# title and appear when a born-digital PDF's custom display font extracts as
# garbage — e.g. "TILOPA" → "TIδτPƖ" (Greek δ τ + Latin-Extended-B Ɩ). Ranges are
# Latin-Extended-B, Greek/Coptic, and the Unicode private-use area. Deliberately
# EXCLUDES Latin-1/Extended-A (IAST diacritics ā ī ṇ ś ṭ …) and the Tibetan block,
# so legitimate Sanskrit/Tibetan titles never trip it.
_MOJIBAKE_RE = re.compile(r"[ƀ-ɏͰ-Ͽ-]")


def looks_mojibake(text: str, *, threshold: int = 2) -> bool:
    """True if `text` carries ≥threshold mojibake glyphs — i.e. its source text
    layer is corrupt and any title read from it is garbage. Conservative: a lone
    stray glyph doesn't trip it."""
    return len(_MOJIBAKE_RE.findall(text or "")) >= threshold

from .classify import Rung, _lenient_json, _run_ladder, default_ladder
from catalogue.db_store import add_alias, fold_key, init_db


def _acc(db):
    """A system Access over this connection — engine-routed edition/work reads + writes + the
    review queue. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)

# Auto-replace at/above this LLM confidence; below it we queue the suggestion
# without touching the stored title.
THRESHOLD = 0.75

# The model reports native_script as a plain word; map it to an `alias_scheme`
# code (the open vocab seeded in schema.sql). Anything else → 'other'.
_NATIVE_SCHEME = {"tibetan": "bo", "bo": "bo",
                  "sanskrit": "sa", "devanagari": "sa", "sa": "sa"}
# The title page lives in the first pages; cap the text we feed the model.
MAX_FRONT_MATTER = 3500

_SYS = (
    "You are shown the OCR'd FRONT MATTER / TITLE PAGE of a book. The OCR may be "
    "garbled — letters spaced out ('T h e  G r e a t'), lines split, words "
    "hyphenated across lines. Read the book's TITLE off THE PAGE ONLY: the main "
    "title plus its subtitle if one is shown, joined naturally. Do NOT include the "
    "author, translator, editor, publisher, series blurb, dates, ISBN/charity "
    "boilerplate, or page furniture.\n"
    "You are given NOTHING but the page text — no filename, no metadata. Infer the "
    "title solely from what is printed on the page.\n"
    "If the printed title or subtitle itself includes a volume/part designation "
    "(e.g. 'Part One: The Preliminaries', 'Volume 2'), keep it as part of the title. "
    "But do NOT invent one from running headers, the table of contents, or a blurb "
    "about other volumes.\n"
    "If the title also appears in Tibetan or Sanskrit on the page, return that as "
    "native_title (set native_script to 'tibetan', 'sanskrit', or 'other'); "
    "otherwise native_title is null.\n"
    "Output ONLY JSON:\n"
    '{"title": "...", "native_title": null, "native_script": null, '
    '"confidence": 0.0, "evidence": "short quote from the page"}\n'
    "If the page shows no readable title at all, return an empty title with LOW "
    "confidence (≤ 0.4)."
)


@dataclass
class TitleSuggestion:
    title: str
    native_title: Optional[str] = None
    native_script: Optional[str] = None
    confidence: float = 0.0
    evidence: Optional[str] = None


def suggest_title(front_matter: str, *,
                  ladder: Optional[list[Rung]] = None) -> Optional[TitleSuggestion]:
    """Infer the title from the OCR'd page text ALONE — the filename is never an
    input (hard requirement: no inference from filenames). Returns None when there's
    no usable text or the model produces no title."""
    fm = (front_matter or "")[:MAX_FRONT_MATTER].strip()
    if not fm:
        return None
    user = f"TITLE-PAGE / FRONT-MATTER TEXT:\n{fm}"
    out = _lenient_json(_run_ladder(
        [{"role": "system", "content": _SYS},
         {"role": "user", "content": user}], ladder, max_tokens=220))
    if isinstance(out, list):
        out = next((o for o in out if isinstance(o, dict)), None)
    if not isinstance(out, dict):
        return None
    title = out.get("title")
    title = title.strip(" .,;:’'\"\n\t") if isinstance(title, str) else ""
    if not title:
        return None
    nt = out.get("native_title")
    nt = nt.strip() if isinstance(nt, str) and nt.strip() else None
    ns = out.get("native_script")
    ns = ns.strip().lower() if isinstance(ns, str) and ns.strip() else None
    try:
        conf = max(0.0, min(1.0, float(out.get("confidence", 0.0) or 0.0)))
    except (TypeError, ValueError):
        conf = 0.0
    ev = out.get("evidence")
    ev = ev.strip()[:300] if isinstance(ev, str) and ev.strip() else None
    return TitleSuggestion(title=title, native_title=nt, native_script=ns,
                           confidence=conf, evidence=ev)


# ── DB readers ───────────────────────────────────────────────────────────────────
def _title_page_text(db, edition_id: int) -> str:
    """The cached title-page/front-matter text for an edition: its holding's
    `file_hash` → the newest `raw_extract_cache` row, head-truncated. '' if none."""
    t = _acc(db).editions.reads.cached_extract_text(edition_id)
    return (t or "")[:MAX_FRONT_MATTER].strip()


def _edition_work(db, edition_id: int) -> Optional[int]:
    """The (single) work contained in an edition, or None (work-less book)."""
    ids = _acc(db).works.reads.ids_in_edition(edition_id)
    return ids[0] if ids else None


def _work_primary_alias(db, wid: int) -> Optional[tuple]:
    """(alias_id, text) of the work's primary title (its first alias), or None."""
    return _acc(db).works.reads.primary_alias(wid)


def _edition_filename(db, edition_id: int) -> str:
    """The basename of the book's file (for CLI output / identifying the book)."""
    p = _acc(db).editions.reads.first_file_path(edition_id)
    return os.path.basename(p) if p else f"edition {edition_id}"


def _assigned_name(db, edition_id: int, status: str) -> str:
    """The title to show on the CLI line. 'queued' suggestions aren't applied yet,
    so show the PROPOSED title from the review item; otherwise show the title now
    on the edition (the newly-assigned one for 'applied')."""
    if status == "queued":
        raw = _acc(db).review.reads.latest_pending_payload(
            "title_proposal", f'%"edition_id": {edition_id}%')
        if raw:
            return json.loads(raw).get("new_title") or ""
    ed = _acc(db).editions.reads.get(edition_id)
    return ed.title if ed and ed.title else ""


def _proposal_already_queued(db, edition_id: int) -> bool:
    return _acc(db).review.reads.exists_pending(
        "title_proposal", f'%"edition_id": {edition_id}%')


# ── apply / revert the title swap (one definition each) ────────────────────────────
def _apply_title(db, p: dict) -> None:
    """Replace the stored title with the proposed one. Mutates `p` in place to
    record what was changed (`applied`, `created_alias_ids`) so a later reject can
    cleanly revert. Edition title is always set; when the edition holds a work, its
    primary alias is rewritten in place (stays first), the old title is preserved
    as a 'filename' alias, and any native-script title is added as an alias."""
    eid, wid = p["edition_id"], p.get("work_id")
    acc = _acc(db)
    acc.editions.writes.set_columns(eid, {"title": p["new_title"]})
    created: list[int] = []
    if wid and p.get("primary_alias_id"):
        acc.works.writes.update_alias(p["primary_alias_id"], p["new_title"])
        old = p.get("old_work_title")
        if old and old != p["new_title"]:
            created.append(add_alias(db, "work", wid, old, "filename"))
        if p.get("native_title"):
            scheme = _NATIVE_SCHEME.get((p.get("native_script") or "").lower(), "other")
            created.append(add_alias(db, "work", wid, p["native_title"], scheme))
    p["applied"] = True
    p["created_alias_ids"] = created


def _revert_title(db, p: dict) -> None:
    """Undo `_apply_title`: restore the old edition title + primary work alias and
    drop the aliases the apply created."""
    eid, wid = p["edition_id"], p.get("work_id")
    acc = _acc(db)
    acc.editions.writes.set_columns(eid, {"title": p.get("old_title") or ""})
    if wid and p.get("primary_alias_id") and p.get("old_work_title") is not None:
        acc.works.writes.update_alias(p["primary_alias_id"], p["old_work_title"])
    for aid in p.get("created_alias_ids") or []:
        acc.works.writes.delete_alias(aid)
    p["applied"] = False
    p["created_alias_ids"] = []


# ── the per-edition pass ──────────────────────────────────────────────────────────
def derive_title_for_edition(db, edition_id: int, *, ladder=None,
                             threshold: float = THRESHOLD, commit: bool = True) -> str:
    """applied | queued | no_text | no_title | unchanged | already | mojibake.

    'applied'  → confident title; replaced now AND queued for review.
    'queued'   → low-confidence title; queued only, stored title untouched.
    'unchanged'→ derived title equals the current one; nothing to do.
    'no_text'  → no cached title-page text. 'no_title' → model gave no title.
    'mojibake' → the page's text layer is corrupt (custom font) → any title read
                 from it is garbage, so we DON'T write one (flag for re-OCR).
    'already'  → a pending proposal for this edition exists (idempotent re-run)."""
    if _proposal_already_queued(db, edition_id):
        return "already"
    fm = _title_page_text(db, edition_id)
    if not fm:
        return "no_text"
    if looks_mojibake(fm):
        return "mojibake"                       # corrupt text layer — don't trust it
    cur = _acc(db).editions.reads.get(edition_id)
    old_title = (cur.title if cur else "") or ""
    wid = _edition_work(db, edition_id)
    prim = _work_primary_alias(db, wid) if wid else None

    sug = suggest_title(fm, ladder=ladder)      # page text ONLY — never the filename
    if not sug or not sug.title:
        return "no_title"
    if looks_mojibake(sug.title):
        return "mojibake"                       # garbled title slipped through
    # No-op ONLY when BOTH targets already hold the derived title — a clean work
    # alias must NOT suppress fixing a still-junky edition.title (the displayed
    # book title). If either differs, proceed so the dirty one gets cleaned.
    edition_ok = sug.title == old_title
    work_ok = prim is None or sug.title == prim[1]
    if edition_ok and work_ok and not sug.native_title:
        return "unchanged"

    payload = {
        "edition_id": edition_id, "work_id": wid,
        "old_title": old_title,
        "old_work_title": prim[1] if prim else None,
        "primary_alias_id": prim[0] if prim else None,
        "new_title": sug.title,
        "native_title": sug.native_title, "native_script": sug.native_script,
        "confidence": sug.confidence, "evidence": sug.evidence,
        "applied": False, "created_alias_ids": [],
    }
    high = sug.confidence >= threshold
    if high:
        _apply_title(db, payload)        # replace now; payload records the change
    _acc(db).review.writes.enqueue("title_proposal", payload)
    if commit:
        db.commit()
    return "applied" if high else "queued"


def derive_all_titles(db, *, ladder=None, threshold: float = THRESHOLD,
                      limit: Optional[int] = None, verbose: bool = False) -> dict:
    """Walk every edition through the title pass. Commits per row (resumable)."""
    ladder = ladder if ladder is not None else default_ladder()
    ids = sorted(_acc(db).editions.reads.all_ids())
    if limit:
        ids = ids[:int(limit)]
    tally = {"applied": 0, "queued": 0, "unchanged": 0,
             "no_text": 0, "no_title": 0, "mojibake": 0, "already": 0}
    if verbose:
        print(f"Title re-derivation over {len(ids)} edition(s) "
              f"(threshold={threshold})…", flush=True)
    marks = {"applied": "✓", "queued": "?", "unchanged": "=",
             "no_text": "·", "no_title": "·", "mojibake": "✗", "already": "»"}
    for i, eid in enumerate(ids, 1):
        status = derive_title_for_edition(db, eid, ladder=ladder,
                                          threshold=threshold, commit=True)
        tally[status] += 1
        if verbose:
            fname = _edition_filename(db, eid)
            # The assigned title: edition.title (now the new one for 'applied'); for
            # a queued-only suggestion show the proposed title (not yet assigned).
            name = _assigned_name(db, eid, status)
            print(f"  [{i}/{len(ids)}] {marks[status]} {status:9} "
                  f"{name[:60]!r}  ⟵  {fname[:60]}", flush=True)
    if verbose:
        print(f"done: {tally}", flush=True)
    return tally


# ── review accept / reject (the /review actions) ──────────────────────────────────
def accept_title_proposal(db, item_id: int, *, commit: bool = True) -> bool:
    """Confirm a queued title. A low-confidence (queue-only) item is APPLIED now;
    an already-applied one just resolves. False if missing/not pending."""
    review = _acc(db).review
    row = review.reads.get_typed(item_id, "title_proposal")
    if not row or row[1] != "pending":
        return False
    p = json.loads(row[0])
    if not p.get("applied"):
        _apply_title(db, p)
        review.writes.set_payload(item_id, p)
    review.writes.resolve(item_id)
    if commit:
        db.commit()
    return True


def reject_title_proposal(db, item_id: int, *, commit: bool = True) -> bool:
    """Reject a queued title. If it was auto-applied, REVERT to the old title."""
    review = _acc(db).review
    row = review.reads.get_typed(item_id, "title_proposal")
    if not row or row[1] != "pending":
        return False
    p = json.loads(row[0])
    if p.get("applied"):
        _revert_title(db, p)
        review.writes.set_payload(item_id, p)
    review.writes.reject(item_id)
    if commit:
        db.commit()
    return True


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Re-derive book/work titles from cached title-page text via "
                    "the LLM ladder (no rescan).")
    ap.add_argument("db")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--threshold", type=float, default=THRESHOLD,
                    help="auto-replace at/above this LLM confidence (default %.2f)"
                         % THRESHOLD)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    db = init_db(args.db)
    db.execute("PRAGMA busy_timeout = 30000")
    tally = derive_all_titles(db, threshold=args.threshold, limit=args.limit,
                              verbose=not args.quiet)
    print("summary:", tally)


if __name__ == "__main__":
    main()
