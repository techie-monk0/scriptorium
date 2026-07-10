"""Extract a single-work's Sanskrit (IAST) title from its title page — PRECISELY.

The naive "any IAST word on the title page" picks up the WRONG name constantly: in
"Buddhapālita's Commentary on Nāgārjuna's Middle Way" or "Candrakīrti's Introduction to
the Middle Way", the Sanskrit word is the author OF THE ROOT TEXT, not this book's title.

A published translation prints the Sanskrit original in one of a few STRUCTURAL slots, in
the title-page zone after the English title and before the author — most reliably:

  * a parenthetical right after the English title:  Crushing the Categories (Vaidalyaprakaraṇa)
  * a post-colon subtitle, often "<Author>'s <SanskritTitle>":  …: Nāgārjuna's Vigrahavyāvartanī
  * the lead, when the book is titled in Sanskrit:  Mūlamadhyamakakārikā of Nāgārjuna
  * the MARC 240 uniform title the cataloguer recorded (authoritative): Vigrahavyāvartanī

We extract ONLY from those slots, and we strip a leading "<Name>'s "/"<Name> " author
possessive off a subtitle, so a name that is merely a possessive modifier ("Candrakīrti's
Introduction", "by Nāgārjuna") is never mistaken for the title. That is the whole point:
slot + author-strip = the false positives disappear, by construction.

Diacritics are the strong Sanskrit signal, but the slot itself lets us also accept a
DIACRITIC-LESS transliteration (Vaidalyaprakarana, Mulamadhyamakakarika) when it is a lone
long compound in a parenthetical — without it we'd never need an English dictionary.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

from .translit import _IAST_ONLY_RE

# English / edition / format / role words that disqualify a slot from being a Sanskrit
# title (so "(Revised Edition)", "(A Guide)", ": A Guide" are rejected).
_REJECT = {
    "a", "an", "the", "of", "on", "by", "and", "to", "in", "for", "with", "from",
    "revised", "edition", "ed", "vol", "volume", "second", "third", "fourth", "new",
    "illustrated", "annotated", "abridged", "complete", "selected", "selection",
    "reprint", "paperback", "hardcover", "translation", "translated", "trans",
    "guide", "introduction", "commentary", "part", "book", "series", "anthology",
    "buddhist", "buddhism", "deity", "tibetan", "sanskrit", "english", "chinese",
}
# MARC 240 uniform-title hierarchy prefixes (Tipiṭaka structure) to peel off.
_PITAKA = re.compile(
    r"^(?:tri|tipi|tipiṭaka|tripiṭaka|sūtrapiṭaka|sutrapitaka|vinayapiṭaka|"
    r"abhidharmapiṭaka|tantra)\.\s*", re.I)
_POSSESSIVE = re.compile(r"^(?:[^\s]+(?:['’]s)\s+)+", re.UNICODE)   # "Nāgārjuna's "
_LEAD_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)


def _toks(p: str) -> list:
    return [t.strip(" .,;:’'\"()").lower() for t in p.split() if t.strip(" .,;:’'\"()")]


def _has_reject(p: str) -> bool:
    return any(t in _REJECT for t in _toks(p))


def _all_tokens_iast(p: str) -> bool:
    """Every alphabetic token carries an IAST diacritic (and there is at least one). The
    STRICT test for the noisy lead/subtitle slots, which are full of English words: only a
    phrase that is Sanskrit THROUGHOUT (Daśabhūmika Vibhāṣā, Vigrahavyāvartanī) passes, so a
    lone English word (Readings, Ornament, Meditation) or a mixed phrase (Kālacakra
    Six-session Guru Yoga) is rejected."""
    alpha = [t for t in p.split() if any(c.isalpha() for c in t)]
    return bool(alpha) and all(_IAST_ONLY_RE.search(t) for t in alpha)


def looks_sanskrit_slot(p: str, *, paren: bool = False) -> bool:
    """Is a STRUCTURAL slot's content a Sanskrit title? Rejects anything with an English
    function/edition word. The PARENTHETICAL is the "sure sign", so there a diacritic-less
    lone long compound (Vaidalyaprakarana) is allowed — no English dictionary needed. The
    lead/subtitle slots are English-word-heavy, so they require IAST throughout."""
    p = p.strip()
    if len(p) < 4 or _has_reject(p):
        return False
    if paren:
        if _IAST_ONLY_RE.search(p):
            return True
        toks = p.split()
        return len(toks) == 1 and toks[0].isalpha() and len(toks[0]) >= 8
    return _all_tokens_iast(p)


def _clean_whole_sanskrit(s: str) -> str:
    """A whole uniform-title string IF it reads as a clean Sanskrit phrase (every token
    carries IAST or is length>=6, and at least one carries IAST), after peeling a MARC
    Tipiṭaka hierarchy prefix. Else ''. So 'Vigrahavyāvartanī' and (after peel)
    'Tripiṭaka. Sūtrapiṭaka. Śālistambasūtra' pass; an English wrapper does not."""
    prev = None
    s = s.strip()
    while s != prev:                                   # peel nested "Tripiṭaka. Sūtra…. "
        prev = s
        s = _PITAKA.sub("", s).strip()
    if not s or _has_reject(s):
        return ""
    toks = [t for t in s.split() if any(c.isalpha() for c in t)]
    if not toks:
        return ""
    iast = False
    for t in toks:
        has = bool(_IAST_ONLY_RE.search(t))
        iast = iast or has
        if not has and len(t.strip(".,;:’'\"")) < 6:
            return ""
    return s if iast else ""


def _from_title(title: str) -> List[Tuple[str, str]]:
    out = []
    # (a) parentheticals — the highest-precision "sure sign"
    for m in re.finditer(r"\(([^)]+)\)", title):
        seg = m.group(1).strip()
        if looks_sanskrit_slot(seg, paren=True):
            out.append((seg, "paren"))
    # (b) post-colon subtitle, author-possessive + article stripped
    if ":" in title:
        tail = title.rsplit(":", 1)[1].strip()
        tail = re.sub(r"\([^)]*\)", "", tail).strip()           # paren handled above
        tail = _POSSESSIVE.sub("", tail)
        tail = _LEAD_ARTICLE.sub("", tail).strip()
        if tail and looks_sanskrit_slot(tail):
            out.append((tail, "subtitle"))
    # (c) lead Sanskrit head: "<Sanskrit> of/by <Name>" — but NOT "<Name>'s <English>"
    head = title.split(":", 1)[0]
    m = re.match(r"^(.+?)\s+(?:of|by)\s+[A-ZÀ-ɏ]", head)
    if m and not re.match(r"^[^\s]+['’]s\b", head):
        seg = m.group(1).strip()
        if looks_sanskrit_slot(seg):
            out.append((seg, "lead"))
    return out


def extract_sanskrit_title(title: str, *, uniform_title: str = None) -> List[Tuple[str, str]]:
    """The single-work title-page Sanskrit, as a list of (text, source) in priority order.
    Empty when the title carries no Sanskrit in a structural slot — which is the CORRECT
    answer for an English-titled study whose only Sanskrit is an inline author name."""
    # DB titles are often NFD (decomposed) — the precomposed IAST regex would miss them.
    title = unicodedata.normalize("NFC", title or "")
    uniform_title = unicodedata.normalize("NFC", uniform_title) if uniform_title else None
    out: List[Tuple[str, str]] = []
    seen = set()

    def add(text: str, source: str):
        t = text.strip(" .,;:’'\"")
        k = t.lower()
        if t and k not in seen:
            seen.add(k)
            out.append((t, source))

    if uniform_title:
        whole = _clean_whole_sanskrit(uniform_title)
        if whole:
            add(whole, "cip-uniform")
        for seg, _src in _from_title(uniform_title):       # e.g. paren inside a uniform
            add(seg, "cip-uniform")
    for seg, src in _from_title(title or ""):
        add(seg, src)
    return out
