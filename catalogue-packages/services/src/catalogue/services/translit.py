"""ALA-LC (Tibetan) → EWTS/Wylie transliteration — small, deterministic, OCR-tolerant.

Reusable by design: this module depends on NOTHING else in the package, so the
converter can be lifted into any pass that needs to turn a library catalogue's
romanized Tibetan (ALA-LC, as printed in LoC CIP / MARC 240 uniform titles and 100
name headings) into the EWTS/Wylie key the rest of the system matches on.

ALA-LC romanization of Tibetan parallels Wylie (Turrell Wylie, 1959). The ONLY
systematic differences are five diacritic-marked letters plus the a-chung:

    ALA-LC   →  EWTS     (Tibetan letter)
      ṅ          ng        ང  nga
      ñ          ny        ཉ  nya
      ś          sh        ཤ  sha
      ź          zh        ཞ  zha
      ʼ/'/`      '         འ  'a  (a-chung / achung)

Every other consonant (ka kha ga, ca cha ja, ta tha da na, pa pha ba ma, tsa tsha
dza, wa za ya ra la sa ha) and every Tibetan vowel is ALREADY identical between the
two schemes. So conversion is a per-character substitution — the EASY, lossless
direction — NOT the ambiguous many-to-one phonetic→Wylie direction.
Refs: loc.gov/catdir/cpso/romanization/tibetan-rev.pdf; Wikisource "Tibetan
romanization table"; en.wikipedia.org/wiki/Wylie_transliteration.

OCR caveat (this is the whole reason for `ocr=True`): scanned CIP pages mangle
exactly these diacritics — ś → s / S / 3 / ⁄, ṅ → h / n, ź → z. `to_ewts(..,
ocr=True)` folds the common diacritic LOOK-ALIKES back to the canonical diacritic
(š→ś, ž→ź, ń→ṅ, …) BEFORE converting. That repair is deliberately conservative: it
never promotes a bare ASCII letter (a plain 's' stays 's', never 'sh'), so it cannot
corrupt already-correct text. Residual damage that destroyed the diacritic entirely
('3' for 'ś') is left for the downstream BDRC verify step to absorb via fuzzy match
+ author/date anchor — guessing there would do more harm than good.

This module does ALA-LC→EWTS only. The reverse (EWTS→ALA-LC) is NOT a plain
substitution ('ng' could be nga OR n+g across a stack boundary) and needs a real
Wylie syllable parser; it's intentionally omitted rather than shipped half-right.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Tuple

# ── a-chung (achung) ─────────────────────────────────────────────────────────────────
# ALA-LC writes the a-chung འ as an alif (ʼ, U+02BC). EWTS uses an ASCII apostrophe.
# Typography + OCR scatter it across many glyphs; fold them all to ASCII "'".
_ACHUNG = "ʼʾ‘’ʻ´`′‛"   # ʼʾ''ʻ´`′‛
_ACHUNG_RE = re.compile("[" + re.escape(_ACHUNG) + "]")

# ── OCR confusables → canonical ALA-LC diacritic (only applied when ocr=True) ──────────
# Each maps a glyph an OCR engine substitutes for a *diacritic letter* back to that
# letter. We map only marked look-alikes (caron-for-acute, acute/grave-for-overdot),
# NEVER a bare ASCII letter, so correct text is untouched.
_CONFUSABLE = {
    "š": "ś", "ŝ": "ś", "ṥ": "ś", "ş": "ś",            # s-caron/circumflex/cedilla → s-acute (sha)
    "Š": "Ś", "Ŝ": "Ś", "Ş": "Ś",
    "ž": "ź", "ż": "ź", "ẑ": "ź",                        # z-caron/dot → z-acute (zha)
    "Ž": "Ź", "Ż": "Ź",
    "ń": "ṅ", "ǹ": "ṅ", "ṇ": "ṅ", "ñ̇": "ṅ",            # n-acute/grave/underdot → n-overdot (nga)
    "Ń": "Ṅ", "Ǹ": "Ṅ", "Ṇ": "Ṅ",
}
_CONFUSABLE_RE = re.compile("|".join(re.escape(k) for k in _CONFUSABLE))

# ── the actual ALA-LC → EWTS letter substitutions (case-insensitive on the diacritic) ──
_LETTER = [
    ("ṅ", "ng"), ("Ṅ", "ng"),
    ("ñ", "ny"), ("Ñ", "ny"),
    ("ś", "sh"), ("Ś", "sh"),
    ("ź", "zh"), ("Ź", "zh"),
]

# ── script-detection fingerprints ─────────────────────────────────────────────────────
# Tibetan grammatical particles (high precision when several co-occur).
_TIB_PARTICLES = {
    "kyi", "gyi", "gi", "yi", "kyis", "gyis", "gis", "yis", "pa", "ba", "po", "mo",
    "la", "las", "na", "nas", "du", "tu", "ru", "su", "dang", "te", "ste", "de",
    "pa'i", "ba'i", "pa’i", "ba’i",
}
# IAST diacritics that Tibetan ALA-LC does NOT use (long vowels, retroflexes,
# anusvara/visarga). ṅ/ñ/ś are shared with Tibetan, so they are NOT discriminators.
_IAST_ONLY_RE = re.compile(r"[āīūṝṛḷḹṭḍṣṃḥ]", re.I)

# English contraction / possessive apostrophe ("Āryadeva's", "Shantideva's", "don't") —
# must NOT be read as a Tibetan a-chung. Stripped before the a-chung test so a Latin
# title with a possessive isn't misclassified Tibetan.
_CONTRACTION_RE = re.compile(r"[A-Za-z][ʼʾ‘’`'](?:s|t|re|ll|ve|d|m)\b", re.I)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def fold_confusables(s: str) -> str:
    """Map common OCR diacritic look-alikes back to canonical ALA-LC diacritics
    (š→ś, ż→ź, ń→ṅ, …). Conservative: touches only already-marked glyphs."""
    return _CONFUSABLE_RE.sub(lambda m: _CONFUSABLE[m.group(0)], _nfc(s))


def strip_language_subfield(s: str) -> Tuple[str, "str | None", bool]:
    """Split a MARC uniform title's trailing language/`Selections` subfields off the
    romanized core: `Dbu ma … dgoṅs pa rab gsal. English. Selections` →
    (`Dbu ma … dgoṅs pa rab gsal`, 'English', True). Returns (core, language|None,
    is_selection). OCR-tolerant on the separators/spacing."""
    if not s:
        return "", None, False
    core = _nfc(s).strip()
    selections = bool(re.search(r"\bselections?\b\.?\s*$", core, re.I))
    core = re.sub(r"\.?\s*selections?\b\.?\s*$", "", core, flags=re.I).strip()
    lang = None
    # ". English" — tolerate an OCR ellipsis ("…English") or run-together text in place
    # of the period before the language word.
    m = re.search(r"(?:\.|…|\.{2,})\s*([A-Z][a-z]{2,})\s*\.?\s*$", core)
    if m and m.group(1).lower() in (
            "english", "tibetan", "sanskrit", "chinese", "french", "german",
            "italian", "spanish", "japanese"):
        lang = m.group(1)
        core = core[:m.start()].strip()
    return core.strip(" .,"), lang, selections


def classify_script(s: str) -> str:
    """'tibetan' | 'sanskrit' | 'unknown' for a romanized title. Tibetan is signalled
    by the a-chung apostrophe or several Tibetan particles; Sanskrit (IAST) by long
    vowels / retroflex / anusvara dots WITHOUT a-chung. Used to decide whether a CIP
    uniform title is a Wylie original (→ convert) or already IAST (→ keep as-is)."""
    if not s:
        return "unknown"
    t = _nfc(s)
    t_noctr = _CONTRACTION_RE.sub(" ", t)            # drop English 's/'t/'re/... first
    has_achung = bool(_ACHUNG_RE.search(t_noctr)) or "'" in t_noctr
    toks = re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)?", t.lower())
    particle_hits = sum(1 for w in toks if w in _TIB_PARTICLES)
    # A Tibetan Wylie title is MULTI-SYLLABLE and space-separated. Require the a-chung to
    # be corroborated by a space or a particle, so a single concatenated word whose only
    # apostrophe is a Sanskrit visarga ("…catuh'sata…") is NOT read as Tibetan.
    if (has_achung and (re.search(r"\s", t) or particle_hits >= 1)) or particle_hits >= 2:
        return "tibetan"
    if _IAST_ONLY_RE.search(t):
        return "sanskrit"
    return "unknown"


def to_ewts(s: str, *, ocr: bool = False, names: bool = False) -> str:
    """Convert ALA-LC-romanized Tibetan `s` to an EWTS/Wylie key.

    ocr=True   first folds OCR diacritic look-alikes (scanned CIP) — see module docs.
    names=True treats input as an ALA-LC name heading, where syllables of one name are
               hyphen-joined ('Blo-bzang-grags-pa'); hyphens become spaces.

    Output is lowercased, whitespace-collapsed, edge-punctuation-trimmed — i.e. ready
    to compare against BDRC `prefLabel_bo_x_ewts` / your `fold_key` alias keys. It is
    NOT round-trippable and must never be stored as display text; it's a match key.
    """
    if not s or not s.strip():
        return ""
    t = _nfc(s)
    if ocr:
        t = fold_confusables(t)
    t = _ACHUNG_RE.sub("'", t)                       # a-chung → ASCII apostrophe
    for src, dst in _LETTER:                          # ṅ→ng, ñ→ny, ś→sh, ź→zh
        t = t.replace(src, dst)
    if names:
        t = t.replace("-", " ")
    t = t.lower()
    t = re.sub(r"\s+", " ", t).strip(" .,;:·•/-")
    return t
