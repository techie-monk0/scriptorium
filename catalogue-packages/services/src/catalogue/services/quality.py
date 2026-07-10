"""OCR quality scoring (§4.8c step 2, §4.8d).

Runs on **raw NFC-normalized text BEFORE any FTS folding** (§6) — folding
would mask systematic OCR errors. Score gates which files get re-OCRed.

Heuristics deliberately simple and configurable; this is a triage filter,
not a final judgment. The review queue is the safety net.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


REPLACEMENT_CHAR = "�"

# A "word" for ratio purposes: a run of ≥2 letters including IAST diacritic
# ranges. Latin-1 supplement, Latin Extended-A/B, Latin Extended Additional.
_WORD_RE = re.compile(
    r"[A-Za-zÀ-ɏḀ-ỿ]{2,}"
)

# Systematic-substitution sentinels (§4.8c step 2 — "ś→'s, ö→o"):
#  - apostrophe wedged inside a word between two lowercase letters (`as'rama`)
#    is a classic Tesseract artifact for `ś`.
#
# Excludes English contractions — `it's`, `don't`, `we're`, `you'll`,
# `she'd`, `I'm`, `they've`. A naive `[a-z]+'[a-z]+` flagged these as OCR
# noise and halved the score on clean publisher PDFs (observed: ~1.0
# contractions per 1000 chars in normal English prose — enough to trip
# the per-1000-char threshold on essentially every native-text book).
# `as'rama` and `bo'di` still match: 2+ letters on each side, tail NOT
# in the contraction set.
_SUBST_APOS = re.compile(
    r"\b[a-z]{2,}'(?!(?:s|t|re|ll|ve|d|m)\b)[a-z]{2,}\b"
)


@dataclass(frozen=True)
class QualityReport:
    score: float                  # 0.0–1.0
    garbage_ratio: float
    alpha_ratio: float
    avg_word_len: float
    suspect_substitutions: int
    char_count: int


def score_text(text: str) -> QualityReport:
    """Return a composite quality score on raw NFC text.

    The caller decides good/poor with a config threshold (default 0.6).
    Empty text → score 0 (image-only / unreadable).
    """
    if not text or not text.strip():
        return QualityReport(0.0, 0.0, 0.0, 0.0, 0, 0)

    total = len(text)
    garbage = sum(
        1 for c in text
        if c == REPLACEMENT_CHAR or (ord(c) < 32 and c not in "\n\r\t")
    )
    alpha = sum(1 for c in text if c.isalpha())
    garbage_ratio = garbage / total
    alpha_ratio = alpha / total

    words = _WORD_RE.findall(text)
    avg_word_len = (sum(map(len, words)) / len(words)) if words else 0.0
    subst_hits = len(_SUBST_APOS.findall(text))

    # Composite — weights chosen so well-OCRed prose lands 0.75+, gibberish <0.4.
    garbage_term = max(0.0, 1.0 - garbage_ratio * 20)
    alpha_term = min(1.0, alpha_ratio * 1.5)
    length_term = 1.0 if 3.0 <= avg_word_len <= 9.0 else 0.4

    score = 0.4 * garbage_term + 0.35 * alpha_term + 0.25 * length_term

    # Systematic-substitution penalty: scaled per 1000 chars so length-neutral.
    per_kchar = subst_hits / max(1.0, total / 1000.0)
    if per_kchar > 1.0:
        score *= 0.5

    return QualityReport(
        score=round(score, 3),
        garbage_ratio=round(garbage_ratio, 4),
        alpha_ratio=round(alpha_ratio, 4),
        avg_word_len=round(avg_word_len, 2),
        suspect_substitutions=subst_hits,
        char_count=total,
    )
