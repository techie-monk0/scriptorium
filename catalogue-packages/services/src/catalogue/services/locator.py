"""Section locator (§4.7) — find WHERE a TOC entry's content actually starts,
using the document's own structure, for the three input cases:

  1. EPUB              → nav/NCX TOC entries are hyperlinks (file#anchor); slice
                         the spine between consecutive entries' start-documents.
  2. PDF with links    → bookmark outline (`get_toc`) resolves to pages; slice by
                         page range.
  3. PDF without links → (scanned) match each entry's heading text to the page
                         carrying its printed folio, via a folio→physical map with
                         before/after neighbour fallback for folio-dropping pages.

Why not the flattened `raw_extract_cache` blob: a title can occur dozens of times
(footnote, header, discussion, index, the reproduced text) and the extraction order
isn't even linear — fuzzy title search picks an essentially random occurrence.

Each located Section carries two deterministic signals for the peek:
  - VERSE FORM — a homage/obeisance opening + CONSECUTIVE stanza numbers (1,2,3,4…)
    → a reproduced canonical text (root/commentary). Brevity alone is NOT verse
    (indexes, source-lists, outlines are short-line too — that fooled an earlier cut).
  - attribution — "Attributed to / composed by / by X" at the opening → the author.
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .pagemap import _mostly_upper, build_page_map, detect_folio
from .toc import is_degenerate_outline


@dataclass
class Section:
    title: str
    text: str                       # section body, verse line-breaks preserved
    n_images: int = 0
    locator: str = ""               # 'file#anchor' (epub) or 'pages a-b' (pdf)
    source: str = ""                # 'epub-nav' | 'pdf-bookmark' | 'pdf-textlayer'
    verse: float = 0.0              # verse-form score [0,1]
    attribution: Optional[str] = None   # author parsed from the opening, if any
    level: int = 0                  # TOC nesting depth (0 = top-level part/chapter,
                                    # >0 = nested child); from nav <ol>/NCX navPoint
                                    # nesting or the PDF bookmark outline level. Only
                                    # consumed under `toc_hierarchy` segmentation.
    toc_author: Optional[str] = None    # author the printed Contents named for this
                                    # entry (parsed by toc.parse_contents_index); the
                                    # FALLBACK author when the section's own first-page
                                    # attribution is absent (see book_analysis).

    def opening(self, n: int = 1600) -> str:
        return self.text[:n]


# ── HTML → text (keep line structure so verse lines survive) ────────────────
def _html_text(html: str) -> tuple[str, int]:
    n_images = len(re.findall(r'<img\b', html, re.I))
    h = re.sub(r'(?is)<(script|style)\b.*?</\1>', ' ', html)
    h = re.sub(r'(?i)</(p|div|h[1-6]|li|br|tr)\s*>|<br\s*/?>', '\n', h)
    h = re.sub(r'<[^>]+>', ' ', h)
    h = (h.replace('&nbsp;', ' ').replace('&amp;', '&')
           .replace('&lt;', '<').replace('&gt;', '>')
           .replace('&#8217;', '’').replace('&#8216;', '‘'))
    lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in h.splitlines()]
    return ('\n'.join(ln for ln in lines if ln), n_images)


# ── VERSE FORM signal ───────────────────────────────────────────────────────
_HOMAGE = re.compile(r'\b(homage|obeisance|i bow|i prostrate|i pay homage|'
                     r'salutation|i take refuge)\b', re.I)


def _longest_consecutive(nums: list[int]) -> int:
    """Longest run of strictly +1-incrementing values in list order — the stanza
    sequence 1,2,3,4… A bibliography/king-list of unrelated numbers stays at 1."""
    best = cur = 0
    prev = None
    for n in nums:
        cur = cur + 1 if (prev is not None and n == prev + 1) else 1
        best = max(best, cur)
        prev = n
    return best


def verse_score(text: str) -> float:
    """Heuristic [0,1] that a passage is reproduced VERSE (not prose/lists).
    Requires real evidence — consecutive stanza numbering and/or a homage opening;
    line brevity only *amplifies* an existing verse signal, never creates one."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 6:
        return 0.0
    sample = lines[:80]
    nums = [int(m.group(1)) for ln in sample
            if (m := re.match(r'^\(?(\d{1,3})\)?[.)\s]', ln))]
    run = _longest_consecutive(nums)
    homage = bool(_HOMAGE.search('\n'.join(sample[:8])))
    short_frac = sum(1 for ln in sample if len(ln) <= 70) / len(sample)
    score = 0.0
    if run >= 4:
        score += 0.6
    elif run >= 2:
        score += 0.25
    if homage:
        score += 0.35
    if run >= 2 or homage:
        score += 0.2 * short_frac
    return min(1.0, score)


# ── attribution ("Attributed to X" / "composed by X" / "by X") ──────────────
# The verb phrases are case-insensitive via the scoped (?i:…) group, but the NAME is
# case-SENSITIVE — it must begin with an uppercase letter. A global re.I makes
# [A-ZÀ-῿] also match lowercase, so "composed by the great and" wrongly captured the
# lowercase fragment "the great and" as an author (book 51/69/211 over-segmented).
_NAME = (r"[A-ZÀ-῿][\wÀ-῿.’'-]*(?:[ \t ]+[A-ZÀ-῿][\wÀ-῿.’'-]*){0,3}")
_ATTRIB = re.compile(
    r'(?i:attributed to|ascribed to|composed by|written by|spoken by|'
    r'by the master|a work by|given by|revealed by)[ \t ]+(' + _NAME + ')')
# OCR-tolerant fallback: a standalone-ish opening line "<verb> to <Name>" where the
# OCR mangled the verb "attributed" (book 45 #3 prints 'Ailrib'uted to Atifa'). We
# match a near-by token then fuzzy-check it against "attributed", which is exactly
# "all the things attributed might OCR as" without hand-listing each garble.
_ATTRIB_OCR = re.compile(r"(?:^|\n)[\W\d]*([A-Za-z’'][A-Za-z’']{4,13})[ \t]+to[ \t]+("
                         + _NAME + r")[ \t]*(?:$|\n)")


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def find_attribution(opening: str) -> Optional[str]:
    m = _ATTRIB.search(opening)
    if m:
        return m.group(1).strip()
    # OCR-tolerant "attributed to NAME": a line whose first word reads as a garbled
    # "attributed" (edit distance ≤ 4 — 'Ailrib’uted' is 2) followed by 'to <Name>'.
    for m in _ATTRIB_OCR.finditer(opening):
        verb = re.sub(r"[^a-z]", "", m.group(1).lower())
        if 6 <= len(verb) <= 13 and _levenshtein(verb, "attributed") <= 4:
            return m.group(2).strip()
    return None


# ── TITLE-PAGE signal (§4.7, A.3) ────────────────────────────────────────────
# A reproduced root text OPENS as the text — its own title page / homage / first
# stanza at the top. A biography that merely QUOTES numbered verses opens with
# narrative prose and only reaches the verses deep in the section. `verse_score`
# can't tell these apart (both contain a consecutive stanza run somewhere in the
# first 80 lines); the onset of the verse is what separates them.
def opens_with_verse(text: str, *, head_lines: int = 15) -> bool:
    """True iff the verse/homage onset is at the TOP of the section (within the
    first `head_lines` non-empty lines) — i.e. the section *is* the reproduced
    text, not prose that later quotes it. Running headers ('123 LIFE OF TILOPA',
    number + MOSTLY-UPPERCASE) are not stanzas and don't count."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    head = lines[:head_lines]
    if _HOMAGE.search("\n".join(head)):
        return True
    for ln in head:
        m = re.match(r'^\(?(\d{1,3})\)?[.)\s]+(\S.*)$', ln)
        if m and not _mostly_upper(m.group(2)):
            return True       # a numbered stanza with lowercase body at the top
    return False


def _finish(title: str, text: str, **kw) -> Section:
    op = text[:1600]
    return Section(title=title, text=text, verse=verse_score(text),
                   attribution=find_attribution(op), **kw)


# ── 1. EPUB ─────────────────────────────────────────────────────────────────
def _epub_sections(path: Path) -> Optional[list[Section]]:
    try:
        z = zipfile.ZipFile(path)
        cont = z.read("META-INF/container.xml").decode("utf-8", "replace")
        opf_path = re.search(r'full-path="([^"]+)"', cont).group(1)
    except Exception:
        return None
    opf_dir = posixpath.dirname(opf_path)
    opf = z.read(opf_path).decode("utf-8", "replace")

    def full(h):
        h = h.split("#")[0]
        return posixpath.normpath(posixpath.join(opf_dir, h)) if opf_dir else h

    man = {}
    for m in re.finditer(r'<item\b[^>]*>', opf):
        i = re.search(r'id="([^"]+)"', m.group(0))
        h = re.search(r'href="([^"]+)"', m.group(0))
        if i and h:
            man[i.group(1)] = h.group(1)
    spine_full = [full(man[i]) for i in re.findall(r'<itemref[^>]+idref="([^"]+)"', opf)
                  if i in man]

    # entries carry TOC NESTING DEPTH (level): nav <ol>/<ul> nesting for EPUB3, or
    # <navPoint> nesting for the NCX — top-level = 0, children > 0. Consumed only by
    # `toc_hierarchy` segmentation downstream; level defaults to 0 otherwise.
    entries: list[tuple[str, str, int]] = []
    nav = re.search(r'<item\b[^>]*properties="[^"]*\bnav\b[^"]*"[^>]*href="([^"]+)"', opf) \
        or re.search(r'<item\b[^>]*href="([^"]+)"[^>]*properties="[^"]*\bnav\b[^"]*"', opf)
    if nav:
        navdoc = z.read(full(nav.group(1))).decode("utf-8", "replace")
        entries = _nav_entries(navdoc)
    else:
        ncx = re.search(r'href="([^"]+\.ncx)"', opf)
        if ncx:
            nd = z.read(full(ncx.group(1))).decode("utf-8", "replace")
            entries = _ncx_entries(nd)
    if not entries:
        return None

    def spine_idx(href):
        f = full(href)
        return next((k for k, sf in enumerate(spine_full) if sf == f), None)

    marked = sorted(
        (m for m in ((t, spine_idx(h), lvl) for t, h, lvl in entries) if m[1] is not None),
        key=lambda m: m[1])
    href_of = {t: h for t, h, _l in entries}
    sections: list[Section] = []
    for n, (title, idx, level) in enumerate(marked):
        end = marked[n + 1][1] if n + 1 < len(marked) else len(spine_full)
        parts, imgs = [], 0
        for sf in spine_full[idx:end]:
            try:
                t, ni = _html_text(z.read(sf).decode("utf-8", "replace"))
            except Exception:
                continue
            parts.append(t)
            imgs += ni
        sections.append(_finish(title, "\n".join(p for p in parts if p),
                                n_images=imgs, locator=href_of.get(title, ""),
                                source="epub-nav", level=level))
    return sections


# ── EPUB TOC parsing with nesting depth ─────────────────────────────────────
def _nav_entries(navdoc: str) -> list[tuple[str, str, int]]:
    """Parse an EPUB3 nav document into (title, href, level) preserving the
    <ol>/<ul> nesting depth of each <a> (outermost list = level 0). Tokenise list
    opens/closes and links in document order, tracking the current list depth."""
    entries: list[tuple[str, str, int]] = []
    depth = 0
    for m in re.finditer(
            r'<(ol|ul)\b[^>]*>|</(ol|ul)\s*>|<a\b[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            navdoc, re.S | re.I):
        if m.group(1):                      # <ol>/<ul> open
            depth += 1
        elif m.group(2):                    # </ol>/</ul>
            depth = max(0, depth - 1)
        else:                               # <a href=…>text</a>
            txt = re.sub(r"<[^>]+>", "", m.group(4)).strip()
            if txt:
                entries.append((txt, m.group(3), max(0, depth - 1)))
    return entries


def _ncx_entries(ncx: str) -> list[tuple[str, str, int]]:
    """Parse an NCX into (title, href, level) preserving <navPoint> nesting depth
    (outermost navPoint = level 0). The docTitle/text before the first navPoint is
    ignored (pending stack is empty)."""
    entries: list[tuple[str, str, int]] = []
    stack: list[dict] = []                  # navPoints open at the current point
    for m in re.finditer(
            r'<navPoint\b[^>]*>|</navPoint\s*>|<text\b[^>]*>(.*?)</text>|'
            r'<content\b[^>]*\bsrc="([^"]+)"',
            ncx, re.S | re.I):
        tok = m.group(0)
        if tok.lower().startswith("<navpoint"):
            stack.append({"level": len(stack), "text": None, "src": None})
        elif tok.lower().startswith("</navpoint"):
            if stack:
                np = stack.pop()
                if np["text"] and np["src"]:
                    entries.append((np["text"], np["src"], np["level"]))
        elif m.group(1) is not None:        # <text> — fill the innermost open navPoint
            if stack and stack[-1]["text"] is None:
                stack[-1]["text"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        elif m.group(2) is not None:        # <content src> — same
            if stack and stack[-1]["src"] is None:
                stack[-1]["src"] = m.group(2)
    return entries


# ── 2. PDF with links (bookmark outline → page range) ───────────────────────
def _pdf_bookmark_sections(doc) -> Optional[list[Section]]:
    # lvl is 1-based in PyMuPDF's outline; store as 0-based depth (top-level = 0).
    marks = [(t, p - 1, max(0, lvl - 1))
             for (lvl, t, p) in doc.get_toc(simple=True) if p and p >= 1]
    if not marks:
        return None
    out: list[Section] = []
    for n, (title, p0, level) in enumerate(marks):
        p1 = max(marks[n + 1][1] if n + 1 < len(marks) else doc.page_count, p0 + 1)
        parts, imgs = [], 0
        for pno in range(p0, min(p1, doc.page_count)):
            parts.append(doc.load_page(pno).get_text("text"))
            imgs += len(doc.load_page(pno).get_images())
        out.append(_finish(title, "\n".join(parts), n_images=imgs,
                           locator=f"pages {p0+1}-{p1}", source="pdf-bookmark", level=level))
    return out


# ── 3. PDF without links (scanned): printed folio + heading text ────────────
def _despace(s: str) -> str:
    return re.sub(r'\s+', '', s).lower()


def _running_header_folio(page) -> Optional[int]:
    """Adapter: read a fitz page's printed folio via `pagemap.detect_folio`."""
    return detect_folio(page.get_text("text"))


def _pdf_textlayer_sections(doc, toc_entries) -> Optional[list[Section]]:
    N = doc.page_count
    page_texts = [doc.load_page(i).get_text("text") for i in range(N)]
    # General folio→physical model (catalogue/pagemap): the longest slope-1 chain of
    # detected folios — tolerant of undetected folios, OCR misreads, and chapter-
    # number false positives, none of which form a chain. When a real chain exists
    # (book 45) it SEEDS each entry's page and gives a `body_floor` (start of the
    # numbered body) so the title fallback can't match a title's first occurrence on
    # the Contents index page. When no chain exists (folio-less scans, or only
    # chapter numbers) the locator still works by title text alone, as before.
    pm = build_page_map(page_texts)
    body_floor = max(0, pm.body_floor - 3) if pm else 0

    def page_text(i):
        return page_texts[i]

    _dp_cache: dict[int, str] = {}

    def despaced(i):
        if i not in _dp_cache:
            _dp_cache[i] = _despace(page_text(i))
        return _dp_cache[i]

    def _key(title: str) -> str:
        return _despace(re.sub(r'^\s*\d+[\s.:–-]*', '', title))[:18]

    # INDEX pages: a printed Contents / index page carries many of the entry titles
    # at once. Skip them in the title fallback so a title's FIRST occurrence there
    # can't win over its real body heading — the general form of `body_floor` for
    # books with no folio chain to pin the body start (book 78).
    all_keys = [k for e in toc_entries if len(k := _key(e.title)) >= 6]
    index_pages = {i for i in range(N)
                   if sum(1 for k in all_keys if k in despaced(i)) >= 4}

    def locate(title: str, P: Optional[int]) -> Optional[int]:
        key = _key(title)
        seed = pm.estimate(P) if pm else None
        if seed is not None and 0 <= seed < N:
            for d in (0, 1, -1, 2, -2, 3, -3):
                i = seed + d
                if 0 <= i < N and len(key) >= 5 and key in despaced(i):
                    return i
            return max(0, min(seed, N - 1))
        # No usable folio seed → title scan from `body_floor`, skipping index pages.
        if len(key) >= 6:
            for i in range(body_floor, N):
                if i not in index_pages and key in despaced(i):
                    return i
        return None

    located = []
    for e in toc_entries:
        i = locate(e.title, getattr(e, "page", None))
        if i is not None:
            located.append((e.title, i, getattr(e, "author", None)))
    if not located:
        return None
    located.sort(key=lambda x: x[1])

    out: list[Section] = []
    for n, (title, p0, toc_author) in enumerate(located):
        p1 = max(located[n + 1][1] if n + 1 < len(located) else N, p0 + 1)
        parts, imgs = [], 0
        for pno in range(p0, min(p1, N)):
            parts.append(page_text(pno))
            imgs += len(doc.load_page(pno).get_images())
        out.append(_finish(title, "\n".join(parts), n_images=imgs,
                           locator=f"pages {p0+1}-{p1}", source="pdf-textlayer",
                           toc_author=toc_author))
    return out


def extract_sections(path: Path, *, toc_entries=None) -> Optional[list[Section]]:
    """Locate real section content. EPUB → nav anchors; PDF → bookmarks, else
    (link-less scan) printed-folio + heading if `toc_entries` is supplied. Returns
    None when no path applies (caller falls back to vision, §15)."""
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return _epub_sections(path)
    if suffix == ".pdf":
        try:
            import fitz
            doc = fitz.open(path)
        except Exception:
            return None
        # Prefer the bookmark outline — UNLESS it's degenerate (page-label-only auto
        # bookmarks, book 45): those carry no work title, so fall through to the
        # printed-Contents locator with the real parsed entries. Checked on the raw
        # outline titles up front so a 700-page junk outline isn't extracted in full
        # only to be discarded.
        outline_titles = [t for (_lvl, t, _p) in doc.get_toc(simple=True) if t]
        if outline_titles and not is_degenerate_outline(
                outline_titles, page_count=doc.page_count):
            bm = _pdf_bookmark_sections(doc)
            if bm:
                return bm
        return _pdf_textlayer_sections(doc, toc_entries) if toc_entries else None
    return None
