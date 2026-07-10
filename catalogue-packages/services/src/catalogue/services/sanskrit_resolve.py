"""Sanskrit work verify gate — the Sanskrit twin of `wylie_resolve`.

Given an IAST title (from `sanskrit_title.extract_sanskrit_title`) + optional
author, resolve to a canonical work:

  1. **84000 Toh `by_sanskrit`** (diacritic/digraph fold-key) — a hit gives the Toh#
     AND the cross-lingual {english, tibetan, sanskrit} titles, high confidence.
  2. **BDRC fuzzy work search** over the IAST title — reuses `BdrcWorkSearch` (its
     multi_match covers `prefLabel_iast`) + `wylie_resolve.verify_work`'s
     token-containment + homonym/author-anchor rule (a title-only hit is capped and
     should be routed to review — the dgongs-pa-rab-gsal lesson, mirrored).

Returns a plain dict (consumed by `work_detect.live_classical`). Reuses, doesn't
rebuild: `verify_work`, `WorkVerdict`, `BdrcWorkSearch`, `db.fold_key`.
"""
from __future__ import annotations

from typing import Optional

from catalogue.services.wylie_resolve import verify_work


def verify_sanskrit(iast_title: str, *, author: Optional[str] = None,
                    toh_index=None, bdrc_search=None) -> dict:
    """Resolve an IAST title to a canonical work. `toh_index` =
    EightyFourThousandIndex (or None to skip the Toh step); `bdrc_search` = a
    `BdrcWorkSearch().work_search`-shaped fn (or None to skip BDRC)."""
    title = (iast_title or "").strip()
    if not title:
        return {"matched": False, "confidence": 0.0, "reason": "no IAST title"}

    # 1. 84000 Toh by Sanskrit title (language-independent identity).
    if toh_index is not None:
        hit = toh_index.by_sanskrit(title)
        if hit:
            return {"matched": True, "system": "toh", "number": str(hit.get("toh") or ""),
                    "english": hit.get("english"), "sanskrit": hit.get("sanskrit") or title,
                    "tibetan": hit.get("tibetan"), "confidence": 0.95,
                    "reason": "84000 by_sanskrit"}

    # 2. BDRC fuzzy IAST search (homonym-capped, author-anchored where possible).
    if bdrc_search is not None:
        v = verify_work(title, author_ewts=author or None, search=bdrc_search)
        if v.matched:
            return {"matched": True, "system": "bdrc", "number": v.bdrc_id,
                    "english": None, "sanskrit": title, "tibetan": None,
                    "confidence": v.confidence,
                    "reason": f"bdrc-iast: {v.reason}"}
        return {"matched": False, "confidence": v.confidence,
                "sanskrit": title, "reason": f"bdrc-iast: {v.reason}"}

    return {"matched": False, "confidence": 0.0, "sanskrit": title,
            "reason": "no Toh hit, BDRC not consulted"}
