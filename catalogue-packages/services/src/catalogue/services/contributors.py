"""Book contributor resolution (§9) — author(s) + translator(s) per book.

A pluggable module the resolver COMPOSES (like `BDRCClient` / the 84000 index in
`work_canonical_resolver.py`). The model (per the user): *a book is a container of one or more
works; every book has author(s) and optionally translator(s), and in the
degenerate single-work case those ARE the contained work's author/translator.*

The hard rule here: **don't bank on embedded metadata or the filename** — they
are frequently absent and sometimes wrong (re-distributed PDFs carry blank or
bogus `/Author`). They are only HINTS. The authority is the book's own **title
page**, read locally on Ollama (the resolver's LLM ladder, §4.9): the model is
given the hints and the front-matter text and returns ONLY what the title page
supports — confirming, correcting, or dropping each hint, and splitting author
vs translator. No title-page evidence (no front matter / no local LLM) → the
hints survive but the result is marked unverified + low-confidence for review.

This module is pure (no DB, no network of its own); `ContributorResolver.resolve`
takes the front-matter text and an optional LLM `ladder` and returns a
`ContributorResult`. The resolver wraps it with the `resolver_cache` discipline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .classify import Rung, _lenient_json, _run_ladder


@dataclass
class ContributorResult:
    authors: list                     # [str], original/primary authors of the book
    translators: list                 # [str]
    source: str                       # 'title-page' | 'metadata' | 'title-string' | 'none'
    confidence: float
    verified: bool                    # reconciled against the title-page text?
    evidence: Optional[str] = None    # short title-page quote backing the call

    def to_dict(self) -> dict:
        return {"authors": list(self.authors), "translators": list(self.translators),
                "source": self.source, "confidence": round(self.confidence, 2),
                "verified": self.verified, "evidence": self.evidence}


def contributor_result_from_dict(d: dict) -> ContributorResult:
    return ContributorResult(
        authors=list(d.get("authors") or []),
        translators=list(d.get("translators") or []),
        source=d.get("source", "none"),
        confidence=float(d.get("confidence", 0.0) or 0.0),
        verified=bool(d.get("verified", False)),
        evidence=d.get("evidence"),
    )


# ── Filename / title-string hint parsing ────────────────────────────────────
# The corpus uses several conventions (profiled on the real DB):
#   "Title -- Author -- Publisher, Year -- ISBN -- hash -- Anna's Archive"  (≈145)
#   "Author - Title"                                                        (≈123)
#   "Title — Author"   (em-dash; author trails)
# We don't try to assign author-vs-translator or trust the position — we just
# surface candidate NAMES for the title-page verifier to adjudicate.
_NOISE = re.compile(
    r"^\s*(?:"
    r"\d{9,13}[\dxX]?"                      # ISBN-10/13
    r"|[0-9a-f]{16,}"                       # content hash
    r"|.*\b(?:19|20)\d{2}\b.*"              # any field carrying a year (pub info)
    r"|(?:anna[’'`]?s archive)"
    r"|.*\b(?:press|publications?|publishing|publishers?|archive|editions?|"
    r"books|library|institute|foundation|wisdom|shambhala|snow lion)\b.*"
    r")\s*$", re.I)
_ROMAN_OR_NUM = re.compile(r"^[\divxlcdm.\s]+$", re.I)


def _looks_like_person(s: str) -> bool:
    s = s.strip()
    if not (2 <= len(s) <= 60) or _ROMAN_OR_NUM.match(s) or _NOISE.match(s):
        return False
    # at least one alphabetic, capitalized-ish token; not a bare common noun run
    return bool(re.search(r"[A-Za-zÀ-ÿ]", s)) and not s.isdigit()


def _split_people(field_value: str) -> list[str]:
    parts = re.split(r"\s*;\s*|\s*&\s*|\s+\band\b\s+", field_value.strip())
    return [p.strip() for p in parts if p.strip()]


def parse_title_contributors(edition_title: Optional[str]) -> tuple[list, Optional[str]]:
    """Return `(candidate_names, clean_title)` parsed from the filename-derived
    edition title. Roles are NOT assigned (the title page decides)."""
    raw = (edition_title or "").strip()
    if not raw:
        return [], None
    # underscores are filename-safe spaces ('The_Connected_Discourses…') — normalize
    # so word counts and the clean title come out right.
    t = re.sub(r"\s+", " ", raw.replace("_", " ")).strip()

    if " -- " in t:                                  # Anna's Archive layout
        fields = [f.strip() for f in t.split(" -- ")]
        clean_title = fields[0] or None
        cands: list[str] = []
        # field 1 is the author column; later fields are pub/isbn/hash noise.
        for f in fields[1:2]:
            cands += [p for p in _split_people(f) if _looks_like_person(p)]
        return list(dict.fromkeys(cands)), clean_title

    for dash in ("—", "–"):                          # em/en-dash: "Title — Author"
        if dash in t:
            left, _, right = t.partition(dash)
            cands = [p for p in _split_people(right) if _looks_like_person(p)]
            return list(dict.fromkeys(cands)), (left.strip() or None)

    if " - " in t:                                   # "Author - Title" OR "Title - Author"
        left, _, right = t.partition(" - ")
        lw, rw = len(left.split()), len(right.split())
        # The author is the shorter, name-shaped side; the other side is the title.
        if _looks_like_person(right) and rw <= 4 and rw < lw:
            cands = [p for p in _split_people(right) if _looks_like_person(p)]
            return list(dict.fromkeys(cands)), (left.strip() or None)
        if _looks_like_person(left) and lw <= 4 and lw <= rw:
            cands = [p for p in _split_people(left) if _looks_like_person(p)]
            return list(dict.fromkeys(cands)), (right.strip() or None)

    return [], t


# ── Title-page reconciliation (local Ollama) ────────────────────────────────
_SYS = (
    "You are shown the FRONT MATTER / TITLE PAGE text of a book, plus CANDIDATE "
    "names parsed from its filename and embedded metadata. Output the book's "
    "author(s) and translator(s) as named for THIS book on its title page or "
    "cover ('by …', 'translated by …', 'trans.', 'rendered by'). NEVER invent a "
    "name.\n"
    "CRITICAL: a name that appears only in an opening EPIGRAPH, dedication, "
    "homage, refuge or lineage prayer, or a QUOTED VERSE is NOT the book's author "
    "— ignore it (e.g. a modern lamrim book may open by quoting Tokme Zangpo or "
    "paying homage to a deity; those are not the author).\n"
    "The CANDIDATE names — especially any marked CONFIRMED (filename and embedded "
    "metadata agree) — are usually the book's real author/translator: keep them "
    "unless the title page clearly assigns the book to someone else. authors = "
    "who wrote/composed the book (a reproduced classical text → its original "
    "composer; a modern work → its modern author). Output ONLY JSON:\n"
    '{"authors": ["..."], "translators": ["..."], "confidence": 0.0, '
    '"evidence": "short quote from the page"}\n'
    "If the page names no one, return the CONFIRMED candidates with low confidence."
)


def _agreed_authors(cand: list, meta_authors: list) -> list:
    """Authors that BOTH the filename and the embedded metadata point to — a
    strong prior (case-insensitive containment handles 'McDonald' vs 'Kathleen
    McDonald'). The verifier must not drop these for a quoted/epigraph name."""
    out = []
    for c in cand:
        cl = c.lower().strip()
        if not cl:
            continue
        for m in meta_authors:
            ml = m.lower().strip()
            if cl == ml or cl in ml or ml in cl:
                out.append(m if len(m) >= len(c) else c)
                break
    return list(dict.fromkeys(out))


def _clean_names(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        if isinstance(v, str):
            s = v.strip(" .,;:’'\"\n\t")
            if s and _looks_like_person(s):
                out.append(s)
    return list(dict.fromkeys(out))


@dataclass
class ContributorResolver:
    """Pluggable engine. `resolve` reconciles hints against the title page via
    the LLM `ladder` (local Ollama by default). DB-free; the resolver caches it."""

    max_front_matter: int = 4000      # title page lives in the first pages

    def resolve(self, *, edition_title: Optional[str], front_matter: str = "",
                meta: Optional[dict] = None,
                ladder: Optional[list[Rung]] = None) -> ContributorResult:
        meta = meta or {}
        cand, _clean_title = parse_title_contributors(edition_title)
        meta_authors = list(meta.get("authors") or [])
        meta_translators = list(meta.get("translators") or [])
        agreed = _agreed_authors(cand, meta_authors)

        fm = (front_matter or "")[: self.max_front_matter].strip()
        if ladder is not None and fm:
            verdict = self._verify(fm, cand, meta_authors, meta_translators,
                                   agreed, ladder)
            if verdict is not None:
                # an author the filename AND metadata agree on must not be dropped
                # for a quoted/epigraph name the LLM latched onto.
                have = {a.lower() for a in verdict.authors}
                for a in agreed:
                    if a.lower() not in have:
                        verdict.authors.insert(0, a)
                return verdict
        return self._hints_only(cand, meta_authors, meta_translators)

    def _hints_only(self, cand, meta_authors, meta_translators) -> ContributorResult:
        authors = list(dict.fromkeys(meta_authors or cand))
        translators = list(dict.fromkeys(meta_translators))
        if meta_authors or meta_translators:
            source = "metadata"
        elif cand:
            source = "title-string"
        else:
            source = "none"
        return ContributorResult(
            authors=authors, translators=translators, source=source,
            confidence=0.3 if (authors or translators) else 0.0, verified=False)

    def _verify(self, front_matter, cand, meta_authors, meta_translators,
                agreed, ladder) -> Optional[ContributorResult]:
        hint_lines = []
        if agreed:
            hint_lines.append("CONFIRMED (filename+metadata agree): " + "; ".join(agreed))
        if cand:
            hint_lines.append("from filename: " + "; ".join(cand))
        if meta_authors:
            hint_lines.append("embedded author(s): " + "; ".join(meta_authors))
        if meta_translators:
            hint_lines.append("embedded translator(s): " + "; ".join(meta_translators))
        hints = "\n".join(hint_lines) or "(none)"
        user = (f"CANDIDATE NAMES (hints, may be wrong):\n{hints}\n\n"
                f"TITLE-PAGE / FRONT-MATTER TEXT:\n{front_matter}")
        out = _lenient_json(_run_ladder(
            [{"role": "system", "content": _SYS},
             {"role": "user", "content": user}], ladder, max_tokens=300))
        if isinstance(out, list):
            out = next((o for o in out if isinstance(o, dict)), None)
        if not isinstance(out, dict):
            return None
        authors = _clean_names(out.get("authors"))
        translators = _clean_names(out.get("translators"))
        if not authors and not translators:
            return None
        conf = float(out.get("confidence", 0.0) or 0.0)
        ev = out.get("evidence")
        ev = ev.strip()[:300] if isinstance(ev, str) and ev.strip() else None
        return ContributorResult(
            authors=authors, translators=translators, source="title-page",
            confidence=conf, verified=True, evidence=ev)
