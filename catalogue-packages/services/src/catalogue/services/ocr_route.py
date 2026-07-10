"""Per-page OCR routing + valid-IAST filter (§4.8c/§4.8d, Step 6).

Decides which freshly-OCRed pages warrant the high-accuracy Cloud-Vision pass,
from the LOCAL Tesseract output alone (no ground truth). Settled by the
2026-05-30 bake-off (`ocr_considerations.md` §9): on this corpus Tesseract loses
~75% of IAST diacritics on *every* Sanskrit page (not a hard minority), while
Cloud Vision recovers 55–78% — so the gate is diacritic-RELEVANCE, not
hard-page detection.

Signals, validated against the Oxford-Handbook clean-Unicode ground truth:
  - `tdia`  — valid IAST diacritics emitted by the local pass (tracks density)
  - `skt`   — romanized-Sanskrit vocabulary, diacritic-stripped (relevance even
              when marks were dropped)
  - `xgarb` — anusvāra→`X` / lone-internal-capital signature on dense Sanskrit
  - low Tesseract confidence ⇒ priority (dense pages ran ~80–90 vs ~95)
The two-engine *disagreement* signal was a dead end (saturated ~0.8) — not used.

MUST be fed a fresh Tesseract+IAST pass, never a scan's pre-existing text layer
(the old OCR already dropped the diacritics, so routing would under-flag).

`count_foreign_diacritics` is the valid-IAST filter: Cloud Vision recovers more
marks but substitutes non-IAST glyphs (`ž ē á ä õ`) on anusvāra/underdots; a
high count flags its output for review (§4.8c).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence

# Valid IAST diacritic letters (incl. ē ō for Tōhoku/Ōtani sigla + transliteration).
IAST = set("āĀīĪūŪēĒōŌṛṚṝṜḷḶḹṃṂḥḤṅṄñÑṭṬḍḌṇṆśŚṣṢ")

# Recurring romanized Buddhist-Sanskrit/Pali vocabulary — diacritic-independent,
# so it flags relevance even where the local OCR stripped every mark.
_SKT = set(
    "sunyata madhyamaka madhyamika prajna paramita bodhisattva bodhicitta "
    "tathagata dharmakaya sambhogakaya nirmanakaya nagarjuna candrakirti "
    "dharmakirti vasubandhu asanga abhidharma abhidharmakosa skandha samsara "
    "nirvana sutra tantra vajra mandala samadhi vijnana alaya svabhava anatman "
    "pudgala klesa karma vipaka dhatu ayatana pramana anumana pratyaksa "
    "sautrantika vaibhasika yogacara cittamatra mahayana hinayana vinaya pitaka "
    "nikaya sangha stupa mantra dharani mudra anitya duhkha sila dana ksanti "
    "virya dhyana bodhi tathagatagarbha sunya aryadeva santideva bhasya karika "
    "vrtti visuddhimagga anguttara dukkha nibbana bhikkhu sutta dhamma".split()
)
_WORD = re.compile(r"[A-Za-z]+")
# `oX` / `hXt` (anusvāra→X) and lone internal capitals — a Tesseract dense-Sanskrit
# failure signature. Excludes `T.`-style initials (those are not lower-UPPER-lower).
_XGARB = re.compile(r"[a-z][A-Z][a-z]")


def _strip(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    ).lower()


@dataclass(frozen=True)
class RouteDecision:
    route_to_cloud: bool
    priority: str            # 'high' | 'normal'
    tdia: int
    skt: int
    xgarb: int
    conf: Optional[float]


def route_page(
    text: str,
    conf: Optional[float] = None,
    *,
    dia_min: int = 8,
    skt_min: int = 10,
    generous: bool = True,
) -> RouteDecision:
    """Decide whether ONE page (its fresh Tesseract text) warrants Cloud Vision.

    `generous=True` lowers the bar: the GCV $300 credit covers the whole corpus,
    so the only cost of routing is privacy (page images upload to Google) — there
    is no reason to be stingy on accuracy grounds (§9).
    """
    tdia = sum(1 for c in text if c in IAST)
    skt = sum(1 for w in _WORD.findall(_strip(text)) if w in _SKT)
    xgarb = len(_XGARB.findall(text))
    relevant = (
        tdia >= dia_min
        or (skt >= skt_min and tdia >= 3)
        or (tdia >= 4 and xgarb >= 15)
    )
    if generous:
        relevant = relevant or tdia >= 4 or skt >= skt_min
    priority = "high" if ((conf is not None and conf < 90) or tdia >= 25) else "normal"
    return RouteDecision(bool(relevant), priority, tdia, skt, xgarb, conf)


def plan_escalation(
    page_texts: Optional[Sequence[str]],
    confs: Optional[Sequence[Optional[float]]] = None,
    **kw,
) -> list[int]:
    """Return the 0-based page indices that should escalate to Cloud Vision.
    `None`/empty input → no pages (e.g. EPUB or no per-page text available)."""
    if not page_texts:
        return []
    out = []
    for i, t in enumerate(page_texts):
        c = confs[i] if (confs and i < len(confs)) else None
        if route_page(t or "", c, **kw).route_to_cloud:
            out.append(i)
    return out


def _is_diacritic_latin(c: str) -> bool:
    d = unicodedata.normalize("NFD", c)
    return (
        len(d) > 1
        and d[0].isascii()
        and d[0].isalpha()
        and any(unicodedata.combining(x) for x in d[1:])
    )


def count_foreign_diacritics(text: str) -> int:
    """valid-IAST filter (§4.8c): count Latin-with-diacritic chars that are NOT
    valid IAST — the Cloud-Vision substitution signature (`ä ö õ ž ē á à ş`).
    Tesseract scores ~0 here (it omits, never substitutes); a high count on a
    Cloud-Vision result flags it for review."""
    return sum(1 for c in text if _is_diacritic_latin(c) and c not in IAST)
