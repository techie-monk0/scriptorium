"""Text extraction for the sweep (§4.7, §4.8c).

Returns NFC-normalized raw text (§4.8c step 1). Quality scoring and
validation happen downstream in `quality.py`, before any FTS folding.

PDF support is a soft dependency on PyMuPDF (`fitz`). If absent, PDFs come
back as image-only and are queued for digitization at Step 6 — the sweep
keeps running. EPUB uses only stdlib so tests are hermetic.
"""
from __future__ import annotations

import unicodedata
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ExtractedText:
    text: str                    # NFC-normalized, diacritics intact
    page_count: int | None
    producer: str | None         # PDF /Producer or "epub" — sniffs OCR origin
    is_image_only: bool
    # Per-page NFC text, when the format is paginated (PDF). None for EPUB
    # (reflowable) or when PyMuPDF is unavailable. Used by the Step-6 OCR
    # router (§4.8d) to escalate diacritic-relevant pages to Cloud Vision.
    page_texts: tuple[str, ...] | None = None


def extract(path: Path) -> ExtractedText | None:
    """Dispatch on suffix. Returns None for unsupported file types so the
    sweep can log + skip without choking."""
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return _extract_epub(path)
    if suffix == ".pdf":
        return _extract_pdf(path)
    return None


# ── EPUB (stdlib only) ────────────────────────────────────────────────────
class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._buf: list[str] = []
        self._skip = 0

    # <head> (and its <title>/<meta>) is document metadata, NOT reading-order body
    # text. EPUB publishers frequently leave a stale/templated <title> on the cover
    # page (e.g. a leftover "The Diamond Cutter Sutra" on a Chittamani Tara book);
    # since the cover is first in spine order, that string would otherwise become
    # line 1 of the extraction and poison title recognition. Skip it like script/style.
    _SKIP_TAGS = ("script", "style", "head")

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def text(self) -> str:
        return " ".join(s.strip() for s in self._buf if s.strip())


def _epub_ordered_names(z: "zipfile.ZipFile") -> list[str]:
    """Content-document names in OPF SPINE (true reading) order, so the title and
    copyright pages come first. Any (x)html not listed in the spine is appended; if
    there's no OPF/spine, falls back to ZIP order. (The cache used to use raw ZIP
    order, which scrambles the head onto body text — see title_page_text.)"""
    import posixpath
    import re as _re
    names = set(z.namelist())
    html = [n for n in z.namelist()
            if n.lower().endswith((".html", ".xhtml", ".htm"))]
    try:
        cont = z.read("META-INF/container.xml").decode("utf-8", "replace")
        m = _re.search(r'full-path="([^"]+)"', cont)
        if not m:
            return html
        opf_path = m.group(1)
        opf = z.read(opf_path).decode("utf-8", "replace")
        base = posixpath.dirname(opf_path)
        manifest: dict[str, str] = {}
        for tag in _re.findall(r"<item\b[^>]*>", opf):
            idm = _re.search(r'\bid="([^"]+)"', tag)
            hm = _re.search(r'\bhref="([^"]+)"', tag)
            if idm and hm:
                manifest[idm.group(1)] = hm.group(1)
        ordered: list[str] = []
        for idref in _re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf):
            href = manifest.get(idref)
            if not href:
                continue
            name = posixpath.normpath(posixpath.join(base, href)) if base else href
            if name in names:
                ordered.append(name)
        seen = set(ordered)                      # keep any html the spine omitted
        ordered += [n for n in html if n not in seen]
        return ordered or html
    except Exception:
        return html


def _extract_epub(path: Path) -> ExtractedText:
    parts: list[str] = []
    with zipfile.ZipFile(path) as z:
        for name in _epub_ordered_names(z):      # OPF spine (reading) order
            try:
                raw = z.read(name).decode("utf-8", errors="replace")
            except KeyError:
                continue
            p = _TextHTMLParser()
            p.feed(raw)
            parts.append(p.text())
    joined = "\n".join(p for p in parts if p)
    nfc = unicodedata.normalize("NFC", joined)
    return ExtractedText(
        text=nfc,
        page_count=None,
        producer="epub",
        is_image_only=not nfc.strip(),
    )


# ── PDF (soft dep: PyMuPDF) ───────────────────────────────────────────────
def _extract_pdf(path: Path) -> ExtractedText:
    try:
        import fitz  # type: ignore
    except ImportError:
        # Plan §4.7 step 3: no/unreadable text layer → metadata-only,
        # queued for digitization. Producer sniff is best-effort.
        return ExtractedText(
            text="",
            page_count=None,
            producer=_sniff_pdf_producer(path),
            is_image_only=True,
        )

    doc = fitz.open(str(path))
    try:
        chunks = [doc[i].get_text("text") for i in range(doc.page_count)]
        text = "\n".join(chunks)
        nfc = unicodedata.normalize("NFC", text)
        # Per-page NFC text for the Step-6 router (§4.8d). NFC per page so the
        # router sees the same normalization as the joined text.
        page_texts = tuple(unicodedata.normalize("NFC", c) for c in chunks)
        producer = (doc.metadata or {}).get("producer")
        return ExtractedText(
            text=nfc,
            page_count=doc.page_count,
            producer=producer,
            is_image_only=not nfc.strip(),
            page_texts=page_texts,
        )
    finally:
        doc.close()


def _sniff_pdf_producer(path: Path) -> str | None:
    """Best-effort: scan the first 64 KB for a /Producer entry. Used only
    when PyMuPDF isn't installed; informational, not load-bearing."""
    try:
        head = path.read_bytes()[:65536]
    except OSError:
        return None
    import re as _re
    m = _re.search(rb"/Producer\s*\(([^)]+)\)", head)
    if not m:
        return None
    try:
        return m.group(1).decode("latin-1", errors="replace")
    except Exception:
        return None


# ── Title-page / front matter in READING ORDER (§9 contributor resolver) ────
# The contributor resolver needs the actual title page, NOT the head of the
# sweep's raw_extract_cache: that blob concatenates EPUB documents in ZIP-
# directory order, which is frequently scrambled (e.g. chap07.html physically
# first), so its head lands on body text and an opening epigraph's quoted author
# gets mistaken for the book's author. Here we read in the OPF SPINE order (true
# reading order) for EPUB and the first pages for PDF, so the title/copyright
# page is what the verifier sees. Targeted read; does not touch raw_extract_cache.
def title_page_text(path: Path, *, max_chars: int = 4000) -> str:
    """Reading-order front matter (≈ the first `max_chars`), or '' on any error."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".epub":
            return _epub_front_matter(path, max_chars)
        if suffix == ".pdf":
            return _pdf_front_matter(path, max_chars)
    except Exception:
        pass
    return ""


def _epub_front_matter(path: Path, max_chars: int) -> str:
    import posixpath
    import re as _re
    with zipfile.ZipFile(path) as z:
        cont = z.read("META-INF/container.xml").decode("utf-8", "replace")
        m = _re.search(r'full-path="([^"]+)"', cont)
        if not m:
            return ""
        opf_path = m.group(1)
        opf = z.read(opf_path).decode("utf-8", "replace")
        base = posixpath.dirname(opf_path)
        # manifest id -> href (attribute order varies, so match per <item> tag)
        manifest: dict[str, str] = {}
        for tag in _re.findall(r"<item\b[^>]*>", opf):
            idm = _re.search(r'\bid="([^"]+)"', tag)
            hm = _re.search(r'\bhref="([^"]+)"', tag)
            if idm and hm:
                manifest[idm.group(1)] = hm.group(1)
        spine = _re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf)
        parts: list[str] = []
        total = 0
        for idref in spine:                       # true reading order
            href = manifest.get(idref)
            if not href:
                continue
            name = posixpath.normpath(posixpath.join(base, href)) if base else href
            try:
                raw = z.read(name).decode("utf-8", "replace")
            except KeyError:
                continue
            p = _TextHTMLParser()
            p.feed(raw)
            txt = p.text()
            if txt.strip():
                parts.append(txt)
                total += len(txt)
            if total >= max_chars:
                break
    joined = unicodedata.normalize("NFC", "\n".join(parts))
    return joined[: max_chars * 2]                # headroom; resolver re-slices


def _pdf_front_matter(path: Path, max_chars: int, *, max_pages: int = 20) -> str:
    """Accumulate text from the opening pages, SKIPPING blank/image-only pages,
    until we have ~max_chars of real text (or hit max_pages). A fixed first-N
    window misses the title page in art/travel/plate-heavy books where it sits
    behind a frontispiece (e.g. holding 429: title page on page 9, pages 2–8
    blank)."""
    import fitz  # type: ignore
    doc = fitz.open(str(path))
    try:
        parts: list[str] = []
        total = 0
        for i in range(min(max_pages, doc.page_count)):
            t = doc[i].get_text("text")
            if t.strip():
                parts.append(t)
                total += len(t)
            if total >= max_chars:
                break
    finally:
        doc.close()
    return unicodedata.normalize("NFC", "\n".join(parts))[: max_chars * 2]


# ── Embedded contributor metadata (author/translator/title) ────────────────
# A HINT source for the contributor resolver (§9), NEVER authoritative: many
# re-distributed PDFs carry a blank or wrong /Author, and the title page is the
# real authority (the resolver reconciles these hints against it). Best-effort —
# any read error (e.g. a flaky WebDAV file) returns empty, never raises.
def book_metadata(path: Path) -> dict:
    """`{authors: [str], translators: [str], title: str|None, source: str}`.
    Empty lists when absent or unreadable. PDFs expose no translator field;
    EPUB OPF tags translators explicitly (`opf:role="trl"` / EPUB3 role meta)."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return _pdf_metadata(path)
        if suffix == ".epub":
            return _epub_metadata(path)
    except Exception:
        pass
    return {"authors": [], "translators": [], "title": None, "source": ""}


def _split_names(value: str | None) -> list[str]:
    """Split a metadata name field into individual people. Split only on
    unambiguous separators (';', '&', ' and ') — NOT bare commas, which appear
    inside 'Last, First' forms. Role disambiguation is the resolver's job."""
    if not value:
        return []
    import re as _re
    parts = _re.split(r"\s*;\s*|\s*&\s*|\s+\band\b\s+", value.strip())
    return [p.strip() for p in parts if p.strip()]


def _pdf_metadata(path: Path) -> dict:
    import fitz  # type: ignore
    doc = fitz.open(str(path))
    try:
        md = doc.metadata or {}
    finally:
        doc.close()
    authors = _split_names(md.get("author"))
    title = (md.get("title") or "").strip() or None
    return {"authors": authors, "translators": [], "title": title,
            "source": "pdf-meta" if (authors or title) else ""}


_DC = "{http://purl.org/dc/elements/1.1/}"
_OPF = "{http://www.idpf.org/2007/opf}"


def _epub_metadata(path: Path) -> dict:
    import re as _re
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        cont = z.read("META-INF/container.xml").decode("utf-8", "replace")
        m = _re.search(r'full-path="([^"]+)"', cont)
        if not m:
            return {"authors": [], "translators": [], "title": None, "source": ""}
        opf = z.read(m.group(1)).decode("utf-8", "replace")
    try:
        root = ET.fromstring(opf)
    except ET.ParseError:
        return {"authors": [], "translators": [], "title": None, "source": ""}

    # EPUB3 carries roles in <meta property="role" refines="#id">trl</meta>.
    id_roles: dict[str, str] = {}
    for meta in root.iter(f"{_OPF}meta"):
        if (meta.get("property") or "").lower() == "role":
            ref = (meta.get("refines") or "").lstrip("#")
            if ref:
                id_roles[ref] = (meta.text or "").strip().lower()

    authors: list[str] = []
    translators: list[str] = []
    title: str | None = None
    for el in root.iter():
        if el.tag == f"{_DC}title" and title is None:
            title = (el.text or "").strip() or None
        elif el.tag in (f"{_DC}creator", f"{_DC}contributor"):
            name = (el.text or "").strip()
            if not name:
                continue
            role = (el.get(f"{_OPF}role") or id_roles.get(el.get("id") or "") or "").lower()
            if role == "trl":
                translators.append(name)
            elif role == "aut" or (el.tag == f"{_DC}creator" and not role):
                authors.append(name)
            elif el.tag == f"{_DC}creator":
                authors.append(name)        # creator w/ some other role → still an author hint
    src = "epub-opf" if (authors or translators or title) else ""
    return {"authors": authors, "translators": translators, "title": title, "source": src}


# Type alias for tests / config (§12.1 — interfaces over tools).
ExtractorFn = Callable[[Path], "ExtractedText | None"]
