"""Verify gate: a CIP-derived Wylie title + author → a confirmed BDRC work.

This is step "#2" of the CIP→canonical pipeline and the safety net that makes the
OCR-tolerant ALA-LC→EWTS conversion trustworthy. We never write a Wylie title to the
catalogue on the strength of OCR alone: we convert the printed ALA-LC to an EWTS
candidate (catalogue/translit), search BDRC for it (catalogue/bdrc.BdrcWorkSearch),
and ACCEPT only when a hit's Wylie label sufficiently contains our title tokens AND —
when we have one — the author matches. That author/date anchor is exactly what
disambiguates the many `dgongs pa rab gsal`-style title-formula homonyms.

Composition only — it owns no transport. The BDRC search is injected (default live,
tests pass canned hits), so this module is unit-testable offline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .bdrc import BdrcWorkSearch
from .translit import to_ewts

# search signature: (ewts_title, author_ewts|None) -> list[hit dict]
WorkSearchFn = Callable[[str, Optional[str]], list]


@dataclass
class WorkVerdict:
    matched: bool
    bdrc_id: Optional[str] = None
    confidence: float = 0.0            # 0..1: title containment, downweighted if author unconfirmed
    title_label: Optional[str] = None  # the BDRC Wylie label we matched
    author_label: Optional[str] = None
    ewts_query: Optional[str] = None   # the converted title we searched (for the report)
    reason: str = ""


def _toks(s: str) -> set:
    """EWTS comparison tokens: drop the a-chung apostrophe and punctuation so
    'pa'i'≈'pai', split on whitespace. EWTS is already ASCII-lowercased by to_ewts."""
    s = re.sub(r"['’`.\-/]", " ", (s or "").lower())
    return {w for w in s.split() if w}


def _containment(query: set, label: set) -> float:
    """Fraction of the QUERY tokens present in the label (asymmetric on purpose: a
    long BDRC title that CONTAINS our shorter title should still score 1.0)."""
    return len(query & label) / len(query) if query else 0.0


def verify_work(
    ewts_title: str,
    *,
    author_ewts: Optional[str] = None,
    search: Optional[WorkSearchFn] = None,
    min_title: float = 0.8,
) -> WorkVerdict:
    """Confirm `ewts_title` (already ALA-LC→EWTS converted) against BDRC. With an
    `author_ewts` anchor, a match needs both strong title containment and author
    agreement; without one, title containment alone can match but confidence is
    downweighted (and the caller should route it to human review)."""
    if not ewts_title:
        return WorkVerdict(False, reason="empty title", ewts_query=ewts_title)
    search = search or BdrcWorkSearch().work_search
    try:
        hits = search(ewts_title, author_ewts)
    except Exception as exc:                       # network/transport — not a real miss
        return WorkVerdict(False, reason=f"search error: {exc}", ewts_query=ewts_title)

    q = _toks(ewts_title)
    a = _toks(author_ewts) if author_ewts else set()
    best, best_score, best_author_ok = None, -1.0, False
    for h in hits:
        t_score = max((_containment(q, _toks(t)) for t in h.get("titles", [])),
                      default=0.0)
        author_ok = bool(a) and any(a & _toks(au) for au in h.get("authors", []))
        # Author agreement is the disambiguator. With it: full credit (+a nudge). With
        # NO author anchor: cap below 1.0 — a title-only hit is homonym-risky and should
        # land in review, not auto-apply. With an author that DISAGREES: heavy penalty.
        score = t_score * (1.0 if author_ok else 0.85 if not a else 0.6)
        if author_ok:
            score = min(1.0, score + 0.1)
        if score > best_score:
            best, best_score, best_author_ok = h, score, author_ok

    if best is None:
        return WorkVerdict(False, reason="no BDRC hits", ewts_query=ewts_title)

    author_ok = best_author_ok
    best_title = max(best.get("titles", []),
                     key=lambda t: _containment(q, _toks(t)), default=None)
    t_score = _containment(q, _toks(best_title or ""))
    # Accept rule: strong title containment; and if we HAD an author, it must confirm.
    matched = t_score >= min_title and (author_ok or not a)
    reason = ("title+author confirmed" if (matched and a) else
              "title confirmed (no author anchor)" if matched else
              "author mismatch" if (t_score >= min_title and a and not author_ok) else
              "weak title match")
    return WorkVerdict(
        matched=matched, bdrc_id=best.get("id"), confidence=round(best_score, 3),
        title_label=best_title,
        author_label=(best.get("authors") or [None])[0],
        ewts_query=ewts_title, reason=reason)


def verify_from_cip(uniform_title: str, *, script: str, author_alalc: Optional[str] = None,
                    ocr: bool = True, search: Optional[WorkSearchFn] = None) -> WorkVerdict:
    """Convenience: take a CIP uniform title (ALA-LC, as printed) + optional ALA-LC
    author heading, convert to EWTS (Tibetan only; a Sanskrit/IAST title is not Wylie),
    and verify. Sanskrit titles short-circuit — BDRC work-by-Wylie isn't the right key."""
    if script != "tibetan":
        return WorkVerdict(False, reason=f"script={script}: not a Wylie title",
                           ewts_query=None)
    ewts = to_ewts(uniform_title, ocr=ocr)
    author = to_ewts(author_alalc, ocr=ocr, names=True) if author_alalc else None
    return verify_work(ewts, author_ewts=author, search=search)
