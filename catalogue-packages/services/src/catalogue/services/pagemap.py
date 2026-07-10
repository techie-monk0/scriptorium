"""Printed-folio → physical-page mapping (§4.7 case 3) — general, not per-book.

A scanned PDF's pages carry PRINTED folio numbers in their running headers that
differ from the 0-based physical page index by an OFFSET (front matter, plates,
inserts). Locating a Contents entry's content means turning its printed folio into a
physical page. This module owns that computation, kept separate from section
location so it can be tested and reused on its own:

  1. `detect_folio(page_text)` — read the printed folio off a page's running-header
     line (a number beside UPPERCASE or Title-Case header text; a verse/prose line
     '307 If you do not…' is a sentence and is rejected).
  2. `PageMap` — build a robust folio→physical model from the detected
     (folio, physical) anchors. It must survive three real-world defects WITHOUT
     assuming the book has one constant offset (that assumption fails on books whose
     page numbering RESETS — multi-part volumes, roman front matter restarting at
     body page 1):
       • undetected folios (a page whose header didn't OCR) — interpolate;
       • isolated OCR misreads (a late page mis-read as 'folio 57' → wild offset) —
         reject as an outlier against the LOCAL consensus;
       • mid-book numbering resets — handled because the estimate uses the NEAREST
         anchor and slope-1 extrapolation (one folio = one page), so a reset only
         perturbs entries near its boundary, never the whole book.
"""
from __future__ import annotations

import re
from typing import Optional


def _mostly_upper(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) >= 0.6


def is_header_text(s: str) -> bool:
    """True if `s` reads as a running-header title — MOSTLY-UPPERCASE ('LIFE OF
    TILOPA') or TITLE-CASE ('Mind Training', 'How Atiśa Relinquished His Kingdom'),
    most words capitalised. A verse/prose line beside a number ('307 If you do not
    see the nature…') is a SENTENCE — words after the first are lowercase — so it
    scores low and is rejected. Short enough to be a header, not a body line."""
    s = s.strip()
    if not s or len(s) > 70:
        return False
    if _mostly_upper(s):
        return True
    words = re.findall(r"[A-Za-zÀ-῿][A-Za-zÀ-῿'’.\-]*", s)
    if not words:
        return False
    cap = sum(1 for w in words if w[0].isupper())
    return cap / len(words) >= 0.6


def _as_folio(s: str) -> Optional[int]:
    """A line that is just a (possibly OCR letter-spaced) page number: '27', '2 8'."""
    t = re.sub(r"\s+", "", s)
    return int(t) if t.isdigit() and 1 <= len(t) <= 4 else None


def detect_folio(page_text: str) -> Optional[int]:
    """The printed folio on a page = the page number printed in its running head OR
    its FOOTER (books put it at either edge), beside header text (`is_header_text`):
      • same line — 'N  HEADER…' / '…HEADER  N';
      • separate lines — a bare number line adjacent to a header line (the common
        footer layout: '… CHAPTER TITLE' then '27').
    Returns None on folio-dropping pages (the caller's `PageMap` interpolates those)
    and on verse/prose lines that merely start with a number. A chapter number on a
    chapter-opening page may slip through here — `PageMap`'s slope-1 chain rejects it,
    since it doesn't advance one page per unit like a real folio."""
    lines = [l.strip() for l in page_text.splitlines() if l.strip()]
    if not lines:
        return None

    def same_line(ln: str) -> Optional[int]:
        m = re.match(r'^(\d{1,4})\s+(.*)$', ln)
        if m and is_header_text(m.group(2)):
            return int(m.group(1))
        m = re.match(r'^(.*?)\s+(\d{1,4})$', ln)
        if m and is_header_text(m.group(1)):
            return int(m.group(2))
        return None

    # Try both edges: top running head, then bottom footer. Same-line first, then a
    # bare-number line validated by an adjacent header line (number can be on either
    # side of the header — recto vs verso).
    for a in (0, -1):
        n = same_line(lines[a])
        if n is not None:
            return n
    if len(lines) >= 2:
        for a, b in ((0, 1), (1, 0), (-1, -2), (-2, -1)):
            n = _as_folio(lines[a])
            if n is not None and is_header_text(lines[b]):
                return n
    return None


def _slope1_consistent(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True if anchors a=(folio,phys) and b (folio_b > folio_a) lie on a real
    folio sequence: physical advances roughly ONE page per folio (allowing blank/
    plate pages, so the physical gap is between the folio gap and ~twice it). This
    is what separates true page folios from CHAPTER numbers read off heading lines
    ('2 Reflections…', '3 From Seed…' — ~13 pages apart per +1) and from isolated
    OCR misreads (a backward or huge jump)."""
    df = b[0] - a[0]
    dp = b[1] - a[1]
    return df > 0 and dp >= df and dp <= df + max(3, df)


class PageMap:
    """Folio→physical model from detected (folio→physical) anchors. Cleans them to
    the longest SLOPE-1 chain (real folios advance ~1 page each) — this rejects
    chapter-number false positives and OCR misreads WITHOUT assuming a single global
    offset, so mid-book numbering resets degrade gracefully (each contiguous run is a
    candidate chain). Truthy only when a chain of ≥ `_MIN` survives."""

    _MIN = 3            # chain length required to trust the map

    def __init__(self, anchors: dict[int, int]):
        # anchors: printed_folio -> physical_index (0-based; first occurrence each).
        items = sorted(anchors.items())                 # by folio
        self._pts = self._longest_chain(items)

    @staticmethod
    def _longest_chain(items: list[tuple[int, int]]) -> list[tuple[int, int]]:
        n = len(items)
        if n == 0:
            return []
        dp = [1] * n
        prev = [-1] * n
        for k in range(n):
            for j in range(k):
                if _slope1_consistent(items[j], items[k]) and dp[j] + 1 > dp[k]:
                    dp[k] = dp[j] + 1
                    prev[k] = j
        end = max(range(n), key=lambda k: dp[k])
        chain = []
        while end != -1:
            chain.append(items[end])
            end = prev[end]
        return chain[::-1]

    def __bool__(self) -> bool:
        return len(self._pts) >= self._MIN

    def estimate(self, folio: Optional[int]) -> Optional[int]:
        """Physical index for a printed `folio` via the NEAREST chain anchor and
        slope-1 extrapolation (one folio = one page). Local by construction, so a
        mid-book numbering reset only shifts entries near its boundary."""
        if folio is None or not self._pts:
            return None
        f0, i0 = min(self._pts, key=lambda p: abs(p[0] - folio))
        return i0 + (folio - f0)

    @property
    def body_floor(self) -> int:
        """First physical page of the numbered body (smallest chain anchor). The
        front matter / Contents index sits before it — so a title-text fallback that
        starts the scan here cannot match the Contents page as a title's first hit."""
        return min((i for _f, i in self._pts), default=0)


def build_page_map(page_texts: list[str]) -> PageMap:
    """Detect a printed folio on every page and build the `PageMap`. `page_texts[i]`
    is the extracted text of physical page i."""
    anchors: dict[int, int] = {}
    for i, txt in enumerate(page_texts):
        f = detect_folio(txt)
        if f is not None:
            anchors.setdefault(f, i)        # first physical page bearing this folio
    return PageMap(anchors)
