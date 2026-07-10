"""TOC extraction cascade (§4.7) + structural validation (§6).

Cascade order (cheapest first):
  1. Structured outline — EPUB nav / PDF bookmarks (PyMuPDF `get_toc()`).
     If present AND valid (§6 structural checks), use it; no LLM needed.
  2. Vision-LLM on TOC images — *deferred for v1*: the interface is here as
     a callable so it drops in by replacement. Until then, the orchestrator
     falls through to (3) for image-only / outline-less files.
  3. No/unreadable text layer → metadata-only; queue for digitization.

The validator catches the failure mode the plan calls out: a structured
outline can be present yet *wrong* (PDF bookmarks pointing at the wrong
pages, an outline that covers only the front matter). Failures → review
queue with `low_confidence_extraction`, with the structural reason in the
payload (§6).
"""
from __future__ import annotations

import json
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from statistics import median
from typing import Callable, Iterable, Optional


@dataclass
class TOCEntry:
    title: str
    page: Optional[int]      # 1-indexed for human display; None for EPUB nav
    level: int = 1
    author: Optional[str] = None    # original author/composer named in a printed
                                    # Contents line ('… by X' / 'X (dates)'); used as
                                    # the fallback author in segmentation. May be
                                    # OCR-mangled — the authority layer canonicalises.


# ── 1. Structured outline — PDF ──────────────────────────────────────────
def extract_pdf_outline(path: Path) -> Optional[list[TOCEntry]]:
    """Return the PDF's bookmark tree, or None if PyMuPDF unavailable
    or the file has no outline. Never raises on read errors."""
    try:
        import fitz   # type: ignore
    except ImportError:
        return None
    try:
        doc = fitz.open(str(path))
    except Exception:
        return None
    try:
        raw = doc.get_toc() or []
    finally:
        doc.close()
    return [TOCEntry(title=t.strip(), page=p, level=lvl)
            for (lvl, t, p) in raw if t and t.strip()]


# ── 1. Structured outline — EPUB ─────────────────────────────────────────
class _NavHTMLParser(HTMLParser):
    """Extract <h1>/<h2>/<h3> text as TOC entries — adequate for EPUBs
    without a real nav.xhtml; for v1 we don't fully parse OPF/NCX."""
    def __init__(self) -> None:
        super().__init__()
        self._stack: list[int] = []
        self._buf: list[str] = []
        self.entries: list[TOCEntry] = []

    def handle_starttag(self, tag, attrs):
        m = re.fullmatch(r"h([1-6])", tag.lower())
        if m:
            self._stack.append(int(m.group(1)))
            self._buf = []

    def handle_endtag(self, tag):
        if self._stack and tag.lower() == f"h{self._stack[-1]}":
            level = self._stack.pop()
            text = " ".join("".join(self._buf).split()).strip()
            if text:
                self.entries.append(TOCEntry(title=text, page=None, level=level))
            self._buf = []

    def handle_data(self, data):
        if self._stack:
            self._buf.append(data)


def _local(tag: str) -> str:
    """Strip an XML namespace: '{ns}navMap' → 'navMap'."""
    return tag.rsplit("}", 1)[-1]


def _zip_read(z: zipfile.ZipFile, name: str) -> Optional[bytes]:
    try:
        return z.read(name)
    except KeyError:
        return None


def _opf_path(z: zipfile.ZipFile) -> Optional[str]:
    """The OPF (package) path, via META-INF/container.xml — the EPUB's own
    pointer to its root document."""
    data = _zip_read(z, "META-INF/container.xml")
    if not data:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    for el in root.iter():
        if _local(el.tag) == "rootfile" and el.get("full-path"):
            return el.get("full-path")
    return None


def _nav_and_ncx_hrefs(z: zipfile.ZipFile, opf_path: str) -> tuple[Optional[str], Optional[str]]:
    """From the OPF manifest, the EPUB3 nav doc href and the EPUB2 NCX href
    (both resolved relative to the OPF, fragments/percent-encoding removed)."""
    data = _zip_read(z, opf_path)
    if not data:
        return (None, None)
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return (None, None)
    items: dict[str, str] = {}        # id → href
    nav_href = ncx_href = spine_toc = None
    for el in root.iter():
        lt = _local(el.tag)
        if lt == "item":
            iid, href = el.get("id"), el.get("href")
            mt, props = el.get("media-type") or "", el.get("properties") or ""
            if iid and href:
                items[iid] = href
            if href and "nav" in props.split():
                nav_href = href
            if href and mt == "application/x-dtbncx+xml":
                ncx_href = href
        elif lt == "spine":
            spine_toc = el.get("toc")
    if not ncx_href and spine_toc and spine_toc in items:
        ncx_href = items[spine_toc]

    def resolve(href: Optional[str]) -> Optional[str]:
        if not href:
            return None
        href = urllib.parse.unquote(href.split("#", 1)[0])
        base = posixpath.dirname(opf_path)
        return posixpath.normpath(posixpath.join(base, href)) if base else href

    return (resolve(nav_href), resolve(ncx_href))


def _parse_ncx(z: zipfile.ZipFile, ncx_path: str) -> Optional[list[TOCEntry]]:
    """EPUB2 toc.ncx → entries; navPoint nesting becomes `level`."""
    data = _zip_read(z, ncx_path)
    if not data:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    navmap = next((el for el in root.iter() if _local(el.tag) == "navMap"), None)
    if navmap is None:
        return None
    entries: list[TOCEntry] = []

    def walk(el, level: int) -> None:
        for child in el:
            if _local(child.tag) != "navPoint":
                continue
            text = None
            for label in child:
                if _local(label.tag) == "navLabel":
                    txt = next((t for t in label if _local(t.tag) == "text"), None)
                    if txt is not None and (txt.text or "").strip():
                        text = " ".join((txt.text or "").split())
            if text:
                entries.append(TOCEntry(title=text, page=None, level=level))
            walk(child, level + 1)

    walk(navmap, 1)
    return entries


class _Epub3NavParser(HTMLParser):
    """EPUB3 nav doc: capture the `<nav epub:type="toc">` anchor texts, with
    `<ol>` nesting depth as `level`."""
    def __init__(self) -> None:
        super().__init__()
        self._in_toc = False
        self._ol_depth = 0
        self._in_a = False
        self._buf: list[str] = []
        self.entries: list[TOCEntry] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "nav":
            d = dict(attrs)
            etype = (d.get("epub:type") or d.get("type") or "").lower()
            if "toc" in etype.split():
                self._in_toc = True
            return
        if not self._in_toc:
            return
        if tag == "ol":
            self._ol_depth += 1
        elif tag == "a":
            self._in_a, self._buf = True, []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if not self._in_toc:
            return
        if tag == "a" and self._in_a:
            text = " ".join("".join(self._buf).split()).strip()
            if text:
                self.entries.append(
                    TOCEntry(title=text, page=None, level=max(1, self._ol_depth)))
            self._in_a, self._buf = False, []
        elif tag == "ol":
            self._ol_depth = max(0, self._ol_depth - 1)
        elif tag == "nav":
            self._in_toc = False

    def handle_data(self, data):
        if self._in_a:
            self._buf.append(data)


def _parse_epub3_nav(z: zipfile.ZipFile, nav_path: str) -> Optional[list[TOCEntry]]:
    data = _zip_read(z, nav_path)
    if not data:
        return None
    p = _Epub3NavParser()
    p.feed(data.decode("utf-8", errors="replace"))
    return p.entries or None


def extract_epub_outline(path: Path) -> Optional[list[TOCEntry]]:
    """Prefer the EPUB's own navigation document — EPUB3 `nav` then EPUB2
    `toc.ncx` — which is the canonical TOC. Only if neither exists fall back
    to scraping h1/h2/h3 (which misses chapter titles carried in styled
    markup rather than headings, the failure mode that produced a lone
    "About Wisdom Publications" entry). Returns None on read error;
    [] is a meaningful "found nothing" signal distinct from None."""
    try:
        with zipfile.ZipFile(path) as z:
            opf = _opf_path(z)
            if opf:
                nav_href, ncx_href = _nav_and_ncx_hrefs(z, opf)
                for parsed in (
                    _parse_epub3_nav(z, nav_href) if nav_href else None,
                    _parse_ncx(z, ncx_href) if ncx_href else None,
                ):
                    if parsed:
                        return parsed
            # Fallback: heading scan across every (x)html part.
            entries: list[TOCEntry] = []
            for name in z.namelist():
                if not name.lower().endswith((".html", ".xhtml", ".htm")):
                    continue
                raw = z.read(name).decode("utf-8", errors="replace")
                p = _NavHTMLParser()
                p.feed(raw)
                entries.extend(p.entries)
            return entries
    except (zipfile.BadZipFile, OSError):
        return None


# ── 1. Combined entry point ──────────────────────────────────────────────
def extract_structured_outline(path: Path) -> Optional[list[TOCEntry]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_outline(path)
    if suffix == ".epub":
        return extract_epub_outline(path)
    return None


# ── 1b. Text-layer TOC (printed "Contents" page) — [v14] ─────────────────
# Many scanned/OCR'd PDFs have NO bookmark outline (so extract_pdf_outline
# returns []), yet carry a printed Contents page in the text layer. These pure
# helpers locate that region (and detect translator/fragment signals) so an LLM
# can parse it — the §4.7 rung between the structured outline and the vision
# step. The LLM parse itself lives with the LLM client (classify.py); keeping
# these pure makes them testable without a model.
_CONTENTS_KEYS = ("contents", "tableofcontents")
_STRUCT_KW = re.compile(
    r"\b(part|chapter|preface|foreword|introduction|prologue|appendix|"
    r"glossary|bibliography|index|notes?|conclusion|epilogue)\b", re.I)


def _collapse(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _edit_le1(a: str, b: str) -> bool:
    """True if `a` is within Levenshtein distance 1 of `b` — tolerates a single
    OCR glyph slip (e.g. 'contcnts', 'contents1')."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    i = 0
    while i < min(la, lb) and a[i] == b[i]:
        i += 1
    if la == lb:                       # substitution
        return a[i + 1:] == b[i + 1:]
    if la < lb:                        # one char inserted into b
        return a[i:] == b[i + 1:]
    return a[i + 1:] == b[i:]           # one char deleted from b


def _looks_like_contents_heading(line: str) -> bool:
    c = _collapse(line)
    if not c or len(c) > 18:
        return False
    return any(k in c for k in _CONTENTS_KEYS) or _edit_le1(c, "contents")


def locate_toc_region(text: str, *, scan: int = 200_000,
                      window: int = 48) -> Optional[str]:
    """Return the printed Table-of-Contents region of OCR'd `text`, or None.

    Primary: a short line that collapses to ~"contents" — handles OCR
    letter-spacing ("Co n ten ts" → "contents") and a ≤1 glyph slip. Fallback:
    the densest `window` of TOC-shaped lines, scoring same-line "title … N",
    a title line followed by a bare page-number line (numbers in a separate
    column — the 278/299 layout), and structural keywords. None when nothing
    scores → caller falls through to the vision path."""
    lines = text[:scan].split("\n")
    n = len(lines)
    for i, ln in enumerate(lines):                       # primary: heading
        if _looks_like_contents_heading(ln):
            return "\n".join(lines[i:i + window])
    # fallback: score sliding windows
    same = [1 if re.search(r"[A-Za-z].{3,}\s\d{1,3}\s*$", ln.strip()) else 0
            for ln in lines]
    isnum = [1 if re.fullmatch(r"\s*\d{1,3}\s*", ln) else 0 for ln in lines]
    haslet = [1 if re.search(r"[A-Za-z]{3,}", ln) else 0 for ln in lines]
    kw = [1 if _STRUCT_KW.search(ln) else 0 for ln in lines]

    def score(i: int) -> float:
        s = 0.0
        for j in range(i, min(i + window, n)):
            s += same[j]
            if haslet[j] and j + 1 < n and isnum[j + 1]:
                s += 1
            s += 0.4 * kw[j]
        return s

    best_i, best = 0, 0.0
    for i in range(max(1, n - window)):
        sc = score(i)
        if sc > best:
            best, best_i = sc, i
    return "\n".join(lines[best_i:best_i + window]) if best >= 6 else None


# ── Deterministic printed-Contents index parser (§4.7 #2) ────────────────────
# For a numbered Contents — 'NN. Title [wrapped lines] [Author (dates)] folio' —
# parse it WITHOUT an LLM. Used when the bookmark outline is degenerate (page
# labels, book 45) so the real contained-work list comes from the printed index.
# OCR-tolerant: the leading number is often garbled (I→1, IO→10, Jl→31, 2I→21), so
# entry-onset is keyed on 'a short token + . / · / ) + a Capitalised/“quote title',
# not the number's value; the author is the line ending in a (dates) parenthetical;
# the folio is a bare-number line. Names may be OCR-mangled — the authority layer
# canonicalises them (do NOT try to "fix" them here).
_CIDX_ENTRY = re.compile(r'^\s*[\dIilJoOSZ]{1,3}\s*[.·)]\s+(?=["“\'A-ZÀ-῿])(.+\S)\s*$')
_CIDX_ENTRY_NUM_ONLY = re.compile(r'^\s*[\dIilJoOSZ]{1,3}\s*[.·)]\s*$')
_CIDX_AUTHOR = re.compile(
    r'^(?P<name>.+?)\s*\((?:[^)]*\d[^)]*|[^)]*century[^)]*)\)\s*$', re.I)
_CIDX_PAGE = re.compile(r'^\s*\d{1,4}\s*$')
_CIDX_STOP = re.compile(
    r'^\s*(?:table of|notes?|glossary|bibliography|index|preface|introduction|'
    r'technical note|about|appendix|foreword|contents)\b', re.I)


def _cidx_is_anchor(ln: str) -> bool:
    return bool(_CIDX_ENTRY.match(ln) or _CIDX_ENTRY_NUM_ONLY.match(ln))


# A "List of Illustrations / Figures / Plates / Tables / Maps" is ALSO a numbered list
# ('12. Iconometry of the female deity Tara…') and is often LONGER than the real
# Contents, so the longest-run finder would pick it (book 230). Detect such a list's
# heading (OCR letter-spacing tolerated via collapse) and exclude its lines, up to the
# next real-content heading, so the numbered-Contents parser ignores figure captions.
_CIDX_ILLUS_HEAD = re.compile(
    r'^(?:listof)?(?:illustrations?|figures?|colou?rplates?|plates?|tables?|maps?)'
    r'(?:andcredits)?$')
_CIDX_CONTENT_HEAD = re.compile(
    r'^(?:foreword|introduction|preface|prologue|chapter|part(?:one|two|\d)|'
    r'contents|acknowledge?ments?)')


def _cidx_collapse(s: str) -> str:
    return re.sub(r'\s+', '', s).lower()


def _cidx_excluded(lines: list[str]) -> set:
    excl: set = set()
    n, i = len(lines), 0
    while i < n:
        if _CIDX_ILLUS_HEAD.match(_cidx_collapse(lines[i])):
            j = i + 1
            while j < n and not _CIDX_CONTENT_HEAD.match(_cidx_collapse(lines[j])):
                j += 1
            excl.update(range(i, j))
            i = j
        else:
            i += 1
    return excl


def _cidx_span(lines: list[str], excluded: set, *, max_gap: int = 5, min_run: int = 4):
    """The Contents list's line span = the LONGEST run of numbered-entry anchors
    (gaps ≤ max_gap absorb title-wrap/author/folio lines between entries). This is
    self-locating: it picks the 43-entry run over a stray CIP subject number ('1.
    Buddhism—China—…') elsewhere in the front matter — so no fragile region finder
    is needed. Returns (start, end) or None when no run reaches `min_run`."""
    anchors = [i for i, ln in enumerate(lines)
               if i not in excluded and _cidx_is_anchor(ln)]
    if len(anchors) < min_run:
        return None
    runs, cur = [], [anchors[0]]
    for a in anchors[1:]:
        if a - cur[-1] <= max_gap + 1:
            cur.append(a)
        else:
            runs.append(cur); cur = [a]
    runs.append(cur)
    best = max(runs, key=len)
    if len(best) < min_run:
        return None
    end = len(lines)
    for j in range(best[-1] + 1, len(lines)):
        if _CIDX_STOP.match(lines[j]) or j - best[-1] > max_gap:
            end = j; break
    return best[0], end


def parse_contents_index(text: str) -> list[TOCEntry]:
    """Deterministically parse a numbered printed Contents into TOCEntry[] with
    title + author (when named) + folio page. Self-locating (finds the entry run in
    `text`, which may be a whole front-matter slice). Returns [] when no numbered
    Contents is present (caller may fall back to the LLM `parse_toc_region`)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    span = _cidx_span(lines, _cidx_excluded(lines))
    if not span:
        return []
    lines = lines[span[0]:span[1]]
    entries: list[TOCEntry] = []
    cur: Optional[dict] = None

    def flush() -> None:
        nonlocal cur
        if cur and cur["title"]:
            t = re.sub(r"\s+", " ", " ".join(cur["title"])).strip(" .")
            if t:
                entries.append(TOCEntry(title=t, page=cur["page"],
                                        author=cur["author"]))
        cur = None

    for ln in lines:
        if _CIDX_STOP.match(ln):
            flush(); continue
        m = _CIDX_ENTRY.match(ln)
        if m:
            flush(); cur = {"title": [m.group(1)], "author": None, "page": None}; continue
        if _CIDX_ENTRY_NUM_ONLY.match(ln):       # number on its own line, title next
            flush(); cur = {"title": [], "author": None, "page": None}; continue
        if cur is None:
            continue
        a = _CIDX_AUTHOR.match(ln)
        if a and cur["author"] is None and cur["title"]:
            cur["author"] = a.group("name").strip(" .,")
        elif _CIDX_PAGE.match(ln):
            cur["page"] = int(ln.strip()); flush()
        else:
            cur["title"].append(ln)
    flush()
    return entries


def is_toc_fragment(text: str, title: str = "") -> bool:
    """A split-out back/front-matter file or a tiny note isn't a book and has no
    TOC (e.g. a holding titled '… Back Matter', a 2 KB note) — don't force a
    text-layer TOC parse or book-level classification on it."""
    if len(text) < 5_000:
        return True
    return bool(re.search(r"\b(back|front)\s*matter\b", title, re.I))


_TRANSLATOR_RE = re.compile(
    r"\btranslat(ed|ion)\b[^\n]{0,30}\b(by|from)\b"     # "translated by", "translated, and edited by", "translation … by/from"
    r"|\btranslations?\s+by\b"                          # "Translations by …"
    r"|\btrans\.\s+by\b"
    r"|\btranslation\s+(group|committee|team)\b"        # "… Translation Committee/Group"
    r"|\banalyzed,?\s+(and\s+)?translated\b"            # title-page credit form (155)
    r"|\brendered\s+(by|into\s+english)\b", re.I)


def has_translator(text: str, *, head: int = 4_000) -> bool:
    """Title-page translator signal — [v14]: a named translator means the book
    reproduces a translated text (root/commentary), not a modern study.

    Uses *credit-phrase* patterns (not the bare word "translation") on the
    title-page region — a false positive would misclassify a modern study as
    root_plus_commentary (the bug we're fixing), so precision beats recall here;
    missed translations fall through to the book-level LLM call. (EPUB body text
    is filename-ordered, so the title page may be past `head` — another reason
    this is one signal among several, not the sole decider.)"""
    return bool(_TRANSLATOR_RE.search(text[:head]))


# ── 6. Structural validation (§6) ────────────────────────────────────────
@dataclass
class ValidationReport:
    ok: bool
    issues: list[str]
    entry_count: int


def validate_toc(entries: list[TOCEntry], *,
                 doc_page_count: Optional[int] = None,
                 min_entries: int = 3,
                 max_entries: int = 500) -> ValidationReport:
    """Plan §6 checks: entry-count plausibility, monotonic page numbers,
    title-length distribution, TOC-span vs document length."""
    issues: list[str] = []
    n = len(entries)

    if n < min_entries:
        issues.append(f"entry_count_too_low ({n} < {min_entries})")
    if n > max_entries:
        issues.append(f"entry_count_too_high ({n} > {max_entries})")

    # Monotonic pages (only checked when present — EPUB nav has none).
    # Don't demand a perfect ascending sort: nearly every real book has ONE
    # backward step where the front matter (roman prelims parsed to arabic, e.g.
    # 7, 9, 17) gives way to the body restarting at page 1, and multi-part books
    # add a few more resets. Only PERVASIVE backwardness (a large fraction of
    # transitions going backward) signals genuinely scrambled OCR. [M8]
    pages = [e.page for e in entries if e.page is not None]
    if pages:
        descents = sum(1 for i in range(len(pages) - 1) if pages[i + 1] < pages[i])
        if descents > 3 and descents > 0.15 * (len(pages) - 1):
            issues.append("non_monotonic_pages")

    if pages and doc_page_count:
        # TOC span shouldn't fall outside the document.
        if max(pages) > doc_page_count + 5:   # tolerate small front-matter offset
            issues.append("toc_page_beyond_document_end")
        # And it shouldn't cover only the front matter.
        if max(pages) < doc_page_count * 0.2 and doc_page_count > 50:
            issues.append("toc_covers_only_front_matter")

    # Title-length distribution: very short titles often = OCR noise.
    if n:
        lengths = [len(e.title) for e in entries]
        m = median(lengths)
        if m < 3:
            issues.append("title_length_median_too_low")
        if m > 200:
            issues.append("title_length_median_too_high")

    return ValidationReport(ok=not issues, issues=issues, entry_count=n)


# A bare page/folio label used as a bookmark title: 'page0037', 'p. 12', 'folio 3',
# 'Sheet 5', or just '5' — what auto-page-bookmarked PDFs emit (one per page).
_PAGE_LABEL_TITLE = re.compile(
    r'^\s*(?:page|pg|pp|p|folio|fol|sheet|leaf|img|image|scan)?\s*[.\-_]?\s*\d{1,5}\s*$',
    re.I)


def is_degenerate_outline(titles: Iterable[str], *, threshold: float = 0.9,
                          page_count: Optional[int] = None) -> bool:
    """True when a bookmark outline carries NO work signal, so it must be treated as
    ABSENT (the printed-Contents parser runs instead of trusting the outline). Two
    independent signals, either suffices:
      • page-label titles — (nearly) every title is a bare page/folio label
        ('page0001'…'page0719', 'p. 12'), a one-bookmark-per-page auto-generator
        (book 45); ≥`threshold` of the titles match.
      • per-page density — given `page_count`, an outline with an entry for ~every
        page (≥ 0.5 × pages) is a page-level dump, not a list of works (book 22:
        188 bookmarks / 188 pages — 'cover', 'empty page', 'Intro'×8, …).
    Needs ≥5 titles (too few to judge below that)."""
    ts = [t for t in titles if t and t.strip()]
    if len(ts) < 5:
        return False
    if page_count and len(ts) >= 0.5 * page_count:
        return True
    n_label = sum(1 for t in ts if _PAGE_LABEL_TITLE.match(t))
    return n_label / len(ts) >= threshold


# ── 2. Vision-LLM TOC — deferred but interface-ready (§4.7, §12.1) ───────
VisionTOCFn = Callable[[Path], "Optional[list[TOCEntry]]"]


def vision_toc_unavailable(_path: Path) -> Optional[list[TOCEntry]]:
    """Default v1 implementation: return None so the orchestrator queues
    the file for digitization instead. A real implementation drops in by
    replacing this callable on the app config."""
    return None


# ── Cache helpers ────────────────────────────────────────────────────────
def cache_parsed_toc(conn, *, file_hash: str, parse_version: int,
                     entries: list[TOCEntry]) -> None:
    from catalogue.access_api import system_conn
    payload = json.dumps([asdict(e) for e in entries])
    system_conn(conn).parsed_toc_cache.store(file_hash, parse_version, payload)


def load_cached_toc(conn, *, file_hash: str,
                    parse_version: int) -> Optional[list[TOCEntry]]:
    from catalogue.access_api import system_conn
    raw = system_conn(conn).parsed_toc_cache.load(file_hash, parse_version)
    if not raw:
        return None
    return [TOCEntry(**d) for d in json.loads(raw)]
