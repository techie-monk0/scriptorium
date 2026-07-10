"""BookIdentifier — the one API for book identifiers (ISBN, LCCN, …).

The rest of the system never branches on "is this an ISBN or an LCCN". It hands a
book's title string + front-matter text to `BookIdentifier`, gets back a list of
`Identifier(scheme, value)` (most-trusted first), and asks it to `resolve` them to
an authoritative bibliographic record — scheme-agnostic throughout.

Each identifier KIND is a self-contained `IdentifierScheme` plug-in that owns its
own four concerns in one place:
  • find(text)        — pull raw candidates out of OCR/title text
  • normalize(value)  — canonical form (e.g. ISBN-10 → ISBN-13)
  • validate(value)   — cheap local check (e.g. ISBN-13 checksum)
  • lookup(value, …)  — resolve to [EditionRecord] via the authority sources
Add a new identifier (OCLC, British Library system number, DOI, …) by writing one
`@register_scheme` class — extraction/lookup live together and NOTHING downstream
(the resolver, the edition-metadata pass, the UI) changes.

The actual web lookup is delegated to injected authority `sources`
(catalogue/edition_verify.EditionSource — OpenLibrary, Google Books, …) so this
module stays independent of any particular backend and is trivially testable with
fakes. A scheme advertises which source method it needs; the facade does not care.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass, replace
from typing import Optional


@dataclass(frozen=True)
class Identifier:
    """One resolved book identifier. `scheme` is the kind ('isbn'/'lccn'/…),
    `value` the normalized canonical form, `found_in` its provenance."""
    scheme: str
    value: str
    found_in: str = ""          # 'title' | 'front_matter' | …

    def __str__(self) -> str:
        return f"{self.scheme}:{self.value}"


@dataclass
class Resolution:
    """A successful resolve: the authority record + which identifier produced it."""
    record: object              # edition_verify.EditionRecord
    identifier: Identifier


# ── scheme plug-in surface ────────────────────────────────────────────────────────
class IdentifierScheme(abc.ABC):
    name: str = "scheme"
    priority: int = 100          # lower = more trusted → tried first

    @abc.abstractmethod
    def find(self, text: str) -> list[str]:
        """Raw candidate strings of THIS scheme found in `text`."""
        raise NotImplementedError

    def spans(self, text: str) -> list:
        """`[(position, normalized_value)]` for every valid occurrence of this
        scheme in `text` — positions let a caller prefer the one nearest a copyright
        marker. Default: locate each `find()` result via its raw substring."""
        out = []
        for raw in self.find(text or ""):
            val = self.normalize(raw)
            if val and self.validate(val):
                out.append((max((text or "").find(raw), 0), val))
        return out

    def normalize(self, value: str) -> Optional[str]:
        v = (value or "").strip()
        return v or None

    def validate(self, value: str) -> bool:
        return bool(value)

    @abc.abstractmethod
    def lookup(self, value: str, sources) -> list:
        """Resolve a normalized value to [EditionRecord] via `sources`. The scheme
        knows which source method it needs; MUST NOT raise (return [] on failure)."""
        raise NotImplementedError


_SCHEMES: dict = {}


def register_scheme(cls):
    """Class decorator: make a scheme part of the default set."""
    _SCHEMES[cls.name] = cls
    return cls


def default_schemes() -> list:
    return [cls() for cls in sorted(_SCHEMES.values(), key=lambda c: c.priority)]


# ── ISBN ────────────────────────────────────────────────────────────────────────
def _isbn10_to_13(raw10: str) -> Optional[str]:
    """Convert a 10-char ISBN (9 digits + check, which may be 'X') to ISBN-13.
    The ISBN-10 check digit is discarded; the ISBN-13 check is recomputed."""
    body = raw10[:9]
    if not body.isdigit():
        return None
    core = "978" + body
    chk = (10 - sum(int(c) * (1 if i % 2 == 0 else 3)
                    for i, c in enumerate(core)) % 10) % 10
    return core + str(chk)


def _valid_isbn10(s: str) -> bool:
    """ISBN-10 mod-11 checksum (final digit may be 'X' = 10). This validates the
    ORIGINAL 10-char ISBN — essential, because converting any 10 digits to ISBN-13
    yields a checksum-valid 13 by construction (so the 13-check can't gate it)."""
    if len(s) != 10:
        return False
    total = 0
    for i, c in enumerate(s):
        if c in "Xx" and i == 9:
            v = 10
        elif c.isdigit():
            v = int(c)
        else:
            return False
        total += v * (10 - i)
    return total % 11 == 0


@register_scheme
class IsbnScheme(IdentifierScheme):
    """Checksum-validated, OCR-tolerant ISBN extraction. Rather than a rigid regex
    (which breaks on stray spaces, 'ISBN Number:' labels, letter-spacing, and O↔0
    confusion), it anchors on a '978/979' prefix or an 'ISBN' label, takes a short
    window, strips separators (trying a pass of common OCR digit/letter fixes), and
    accepts a candidate ONLY if the ISBN checksum passes — which makes the loose
    matching safe from false positives."""
    name = "isbn"
    priority = 0

    _ANCHOR = re.compile(r"(?i)97[89]|ISBN")
    # OCR digit/letter confusions seen in scanned copyright pages.
    _OCR = str.maketrans("OoIlSBZG", "00115826")

    def _emit(self, run: str, allow_isbn10: bool) -> Optional[str]:
        """A normalized ISBN-13 from a digit/X run, or None. Tries a 978/979-prefixed
        13-digit window (ISBN-13 checksum), then — only after an 'ISBN' label — an
        ISBN-10 validated by its OWN mod-11 checksum (NOT the converted-13 checksum,
        which is always valid by construction and would false-match any 10 digits)."""
        from .isbn import validate_isbn13
        for i in range(max(1, len(run) - 12)):
            d = run[i:i + 13]
            if len(d) == 13 and d[:3] in ("978", "979") and d.isdigit() \
                    and validate_isbn13(d):
                return d
        if allow_isbn10 and len(run) >= 10 and _valid_isbn10(run[:10]):
            return _isbn10_to_13(run[:10])
        return None

    def spans(self, text: str) -> list:
        t = text or ""
        out, seen = [], set()
        for m in self._ANCHOR.finditer(t):
            labelled = m.group(0).lower() == "isbn"
            # window after an 'ISBN' label (skip the label so its letters don't get
            # OCR-translated), else from the 978/979 prefix itself.
            window = t[m.end(): m.end() + 34] if labelled else t[m.start(): m.start() + 30]
            for w in (window, window.translate(self._OCR)):   # plain, then OCR-fixed
                run = re.sub(r"[^0-9Xx]", "", w)
                v = self._emit(run, allow_isbn10=labelled)
                if v:
                    if v not in seen:               # one (position, value) per ISBN
                        seen.add(v)
                        out.append((m.start(), v))
                    break
        return out

    def find(self, text: str) -> list[str]:
        return [v for _, v in self.spans(text)]

    def normalize(self, value: str) -> Optional[str]:
        raw = re.sub(r"[^0-9Xx]", "", value or "")
        if len(raw) == 13 and raw.isdigit():
            return raw
        if len(raw) == 10:
            return _isbn10_to_13(raw)
        return None

    def validate(self, value: str) -> bool:
        from .isbn import validate_isbn13
        return validate_isbn13(value or "")

    def lookup(self, value: str, sources) -> list:
        out = []
        for src in sources:
            try:
                out.extend(src.by_isbn(value) or [])
            except Exception:
                pass
        return out


# ── LCCN (Library of Congress control / catalog-card number) ──────────────────────
@register_scheme
class LccnScheme(IdentifierScheme):
    name = "lccn"
    priority = 1
    _RE = re.compile(
        r"(?i)(?:library of congress (?:control|catalog(?:ue)?(?: card)?) number|lccn)"
        r"[:\s.#]*([0-9][0-9\-\s/]{5,16}[0-9])")

    def find(self, text: str) -> list[str]:
        return [m.group(1) for m in self._RE.finditer(text or "")]

    def normalize(self, value: str) -> Optional[str]:
        # Canonical LCCN drops separators: '75-189390' → '75189390'.
        v = re.sub(r"[\s/\-]", "", value or "")
        return v or None

    def validate(self, value: str) -> bool:
        return (value or "").isdigit() and 8 <= len(value) <= 10

    def lookup(self, value: str, sources) -> list:
        out = []
        for src in sources:
            try:
                out.extend(src.by_lccn(value) or [])
            except Exception:
                pass
        return out


def _title_quality(title: str) -> tuple:
    """Rank a candidate title: prefer one carrying a subtitle, then better-cased
    (more capitalized words), then longer. Used to pick the best record when several
    sources answer for one identifier."""
    t = title or ""
    if not t:
        return (0, 0, 0)
    words = t.split()
    caps = sum(1 for w in words if w[:1].isupper())
    has_sub = 1 if any(sep in t for sep in (":", "—", " - ")) else 0
    return (has_sub, caps, len(t))


def _best_record(records: list):
    """Pick the record with the fullest/best-cased title and back-fill missing
    fields (publisher/year/authors/translators/isbn) from the other records — so a
    source with a better title and one with richer metadata combine."""
    best = max(records, key=lambda r: _title_quality(getattr(r, "title", "")))

    def first(attr):
        return next((getattr(r, attr) for r in records if getattr(r, attr, None)), None)
    return replace(
        best,
        publisher=best.publisher or first("publisher"),
        year=best.year or first("year"),
        authors=best.authors or (first("authors") or ()),
        translators=best.translators or (first("translators") or ()),
        isbn=best.isbn or first("isbn"))


# ── the facade ────────────────────────────────────────────────────────────────────
class BookIdentifier:
    """Scheme-agnostic API over the registered identifier schemes."""

    def __init__(self, schemes=None):
        self.schemes = schemes if schemes is not None else default_schemes()
        self._by_name = {s.name: s for s in self.schemes}

    def extract(self, title: str = "", front_matter: str = "") -> list[Identifier]:
        """All valid identifiers in the title string + front matter, deduped and
        ordered most-trusted-scheme first (title before front matter within a
        scheme). A book with no identifier → []."""
        found: list[Identifier] = []
        seen: set = set()
        for label, text in (("title", title or ""), ("front_matter", front_matter or "")):
            for scheme in self.schemes:
                for raw in scheme.find(text):
                    val = scheme.normalize(raw)
                    if not val or not scheme.validate(val):
                        continue
                    key = (scheme.name, val)
                    if key not in seen:
                        seen.add(key)
                        found.append(Identifier(scheme.name, val, label))
        found.sort(key=lambda i: self._by_name[i.scheme].priority)
        return found

    def resolve(self, identifiers, *, sources=None) -> Optional[Resolution]:
        """Try each identifier (in the given order) against the authority sources;
        return the first that yields a record, with the identifier that produced it.
        `sources` defaults to edition_verify.default_sources() (injectable for tests
        / offline). None when nothing resolves."""
        if sources is None:
            from .edition_verify import default_sources
            sources = default_sources()
        for ident in identifiers:
            scheme = self._by_name.get(ident.scheme)
            if not scheme:
                continue
            recs = scheme.lookup(ident.value, sources)
            if recs:
                return Resolution(_best_record(recs), ident)
        return None

    def resolve_text(self, title: str = "", front_matter: str = "", *,
                     sources=None) -> Optional[Resolution]:
        """Convenience: extract identifiers from the text, then resolve them."""
        return self.resolve(self.extract(title, front_matter), sources=sources)

    def find_in_text(self, text: str, *, markers=None) -> Optional[Identifier]:
        """Find the book's OWN identifier anywhere in `text` (the whole OCR'd body,
        since EPUB extraction can land the copyright page mid-text). When several
        identifiers occur, prefer the one NEAREST a copyright/CIP `markers` match
        (its own, not an "other volumes in this series" list); else the most-trusted
        scheme, earliest position. `markers` is a compiled regex or None."""
        cands = []  # (position, scheme_name, value)
        for scheme in self.schemes:
            for pos, val in scheme.spans(text or ""):
                cands.append((pos, scheme.name, val))
        if not cands:
            return None
        # Rank by SCHEME priority first (ISBN beats LCCN — better lookup coverage),
        # then proximity to a copyright/CIP marker (the book's own vs a series list),
        # then position. So within a scheme the copyright-block one wins, but a nearby
        # LCCN never out-ranks the ISBN.
        marks = [m.start() for m in markers.finditer(text or "")] if markers else []

        def dist(pos):
            return min((abs(pos - mk) for mk in marks), default=0)
        cands.sort(key=lambda c: (self._by_name[c[1]].priority, dist(c[0]), c[0]))
        _, scheme, val = cands[0]
        return Identifier(scheme, val, "page")
