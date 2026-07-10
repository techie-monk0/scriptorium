"""Filename → edition-basics detection.

Some files arrive with rich metadata baked into the filename. Download tools in
particular name files in long, delimited forms — e.g. Anna's Archive:

    Title_ Subtitle -- Author -- Edition, Year -- Publisher -- ISBN -- <md5> -- Anna's Archive.epub

This module turns such a filename into the canonical edition-basics fields WITHOUT
opening the file. It's a small registry: each detector inspects a filename and either
returns a `Detection` or None; `detect()` runs them all and `merge()` folds the hits
(across a book's several files and several detectors) into one best guess.

Add a new pattern by writing a `(filename) -> Detection | None` function and decorating
it with `@register` — the /edition/<id>/detect route and its "Detect" button pick it up
automatically, no wiring. The Anna's-Archive long form is the first implementation."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Detection:
    """Edition-basics fields recovered from a filename. Authors/translators are bare
    NAMES (strings) — the apply step resolves them to person records, never this module."""
    source: str                                   # which detector produced this
    confidence: float = 0.0                       # 0..1; merge() prefers the highest
    title: Optional[str] = None
    subtitle: Optional[str] = None
    authors: list = field(default_factory=list)   # list[str]
    translators: list = field(default_factory=list)
    publisher: Optional[str] = None
    year: Optional[int] = None
    isbn: Optional[str] = None                    # normalized ISBN-13
    edition_statement: Optional[str] = None       # e.g. "First Edition", "PS"

    def is_empty(self) -> bool:
        return not any((self.title, self.subtitle, self.authors, self.translators,
                        self.publisher, self.year, self.isbn))


Detector = Callable[[str], Optional[Detection]]
_DETECTORS: list = []


# ── applying a detection to an edition ─────────────────────────────────────────
def _is_raw_title(title) -> bool:
    """A title that's clearly NOT curated: empty, or a delimited download filename
    pasted in whole (contains ' -- '). Safe to overwrite; anything else we preserve."""
    return (not title) or (" -- " in title)


def predicted_title(old_title, det) -> Optional[str]:
    """The title `apply_to_edition` WOULD end up with for an edition, without applying:
    the detected title when it would overwrite a raw/empty title, else the existing
    (curated) title. Mirrors apply_to_edition's title policy so a bulk caller can check
    for within-batch title collisions before committing."""
    new = det.title if det else None
    if new and _is_raw_title(old_title):
        return new
    return old_title


def _acc(db):
    """A system Access over this connection (engine-routed edition/person reads+writes)."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def resolve_person(db, name: str):
    """A person id for `name` ONLY when exactly one existing person folds to it (reuse).
    Zero or ambiguous (>1) → None, so the apply step never auto-creates or guesses a
    person — it surfaces the name for the operator to link by hand (dedup stays safe)."""
    return _acc(db).persons.reads.resolve_unique_alias(name)


def apply_to_edition(db, eid: int, det: Detection, *, commit: bool = True) -> dict:
    """Write a merged Detection onto edition `eid`. Policy, conservative on curated data:
      • title    — overwritten (the point: a clean title off the filename),
      • subtitle — set when detected and different,
      • publisher / year — FILLED ONLY IF EMPTY (never clobber a curated value),
      • isbn     — set as primary if none yet, else recorded as an `edition_isbn` alias,
      • authors/translators — names that uniquely resolve to an existing person are LINKED
        (reuse); the rest are returned as `unresolved` for the operator to add by hand.
    Returns a summary of exactly what changed."""
    from catalogue.db_store import contributor_store as cs
    ed = _acc(db).editions.reads.get(eid)
    if ed is None:
        raise ValueError(f"no edition {eid}")
    old_title, old_sub, old_pub, old_year, old_isbn = (
        ed.title, ed.subtitle, ed.publisher, ed.year, ed.isbn)
    applied: dict = {}

    changes: dict = {}
    # Title: overwrite ONLY a raw title (empty, or the literal ' -- ' filename used as a
    # title). A curated title is never clobbered — a different detected title is returned
    # as `title_suggestion` for the operator to apply by hand (the filename often
    # TRUNCATES the real title/subtitle, so the stored one is frequently the richer one).
    if det.title and det.title != old_title:
        if _is_raw_title(old_title):
            changes["title"] = det.title
            applied["title"] = {"old": old_title, "new": det.title}
        else:
            from catalogue.db_store import fold_key
            if fold_key(det.title) != fold_key(old_title):
                applied["title_suggestion"] = {"current": old_title, "detected": det.title}
    if det.subtitle and not old_sub:               # fill-if-empty, never clobber curated
        changes["subtitle"] = det.subtitle
        applied["subtitle"] = det.subtitle
    if det.publisher and not old_pub:
        changes["publisher"] = det.publisher
        applied["publisher"] = det.publisher
    if det.year and not old_year:
        changes["year"] = det.year
        applied["year"] = det.year
    if changes:
        _acc(db).editions.writes.set_columns(eid, changes)

    if det.isbn:
        if not old_isbn:
            _acc(db).editions.writes.set_columns(eid, {"isbn": det.isbn})
            applied["isbn"] = det.isbn
        elif det.isbn != old_isbn:
            if not _acc(db).editions.reads.has_isbn_alias(eid, det.isbn):
                _acc(db).editions.writes.add_isbn(eid, det.isbn, "detected from filename")
                applied["isbn_alias"] = det.isbn

    linked = {"authors": [], "translators": []}
    unresolved = {"authors": [], "translators": []}

    def _link(names, role):
        ids = (cs.edition_author_ids(db, eid) if role == "author"
               else cs.edition_translator_ids(db, eid))
        ids = list(ids)
        changed = False
        for name in names:
            pid = resolve_person(db, name)
            if pid is None:
                unresolved[role + "s"].append(name)
            elif pid not in ids:
                ids.append(pid)
                linked[role + "s"].append({"id": pid, "name": name})
                changed = True
            # pid already linked → nothing to do
        if changed:
            (cs.set_edition_authors if role == "author"
             else cs.set_edition_translators)(db, eid, ids)

    if det.authors:
        _link(det.authors, "author")
    if det.translators:
        _link(det.translators, "translator")

    if commit:
        db.commit()
    return {"source": det.source, "confidence": det.confidence,
            "applied": applied, "linked": linked, "unresolved": unresolved}


def register(fn: Detector) -> Detector:
    """Register a `(filename) -> Detection | None` detector. Order = registration order;
    ties in confidence resolve to the earlier-registered detector."""
    _DETECTORS.append(fn)
    return fn


def detectors() -> list:
    """The registered detectors, in registration order (for tests / introspection)."""
    return list(_DETECTORS)


def detect(filename: str) -> list:
    """Run every registered detector on one filename (basename only); return the
    non-empty hits, highest-confidence first."""
    name = os.path.basename(filename or "")
    out = []
    for fn in _DETECTORS:
        try:
            d = fn(name)
        except Exception:
            d = None                              # a flaky detector never breaks the rest
        if d and not d.is_empty():
            out.append(d)
    out.sort(key=lambda d: d.confidence, reverse=True)
    return out


def detect_paths(paths) -> list:
    """detect() over several files (a book's holdings), all hits pooled, best first."""
    out = []
    for p in paths or []:
        out.extend(detect(p))
    out.sort(key=lambda d: d.confidence, reverse=True)
    return out


def merge(detections) -> Optional[Detection]:
    """Fold several detections into one best guess: per scalar field the
    highest-confidence non-empty value wins; authors/translators are taken whole from
    the best hit that has any. Returns None if there's nothing to apply."""
    dets = [d for d in detections if d and not d.is_empty()]
    if not dets:
        return None
    dets.sort(key=lambda d: d.confidence, reverse=True)
    m = Detection(source="+".join(dict.fromkeys(d.source for d in dets)),
                  confidence=dets[0].confidence)
    for fld in ("title", "subtitle", "publisher", "year", "isbn", "edition_statement"):
        for d in dets:
            v = getattr(d, fld)
            if v:
                setattr(m, fld, v)
                break
    for fld in ("authors", "translators"):
        for d in dets:
            v = getattr(d, fld)
            if v:
                setattr(m, fld, list(v))
                break
    return m


def enrich_with_isbn(det, isbn, *, lookup):
    """Prefer AUTHORITATIVE ISBN metadata over the filename parse. The filename gives us
    the ISBN (and rough fields); a real catalogue record gives a clean title + authors —
    so when we have an ISBN we look it up and let it win.

    `lookup(isbn) -> dict | None` in OpenLibrary shape ({title, authors[], publishers[],
    publish_date}); inject a fake in tests / a cached wrapper in the route. Returns a
    merged Detection (ISBN fields win by confidence; the filename still fills gaps the
    lookup lacks, e.g. subtitle). On a miss (or no isbn / no lookup) returns `det`
    unchanged, so detection degrades gracefully to filename-only."""
    if not isbn or lookup is None:
        return det
    try:
        meta = lookup(isbn)
    except Exception:
        meta = None
    if not meta:
        return det
    auth_title = (meta.get("title") or "").strip() or None
    fn_title = det.title if det else None
    # GUARD against a WRONG / shared ISBN — e.g. one ISBN (mistakenly) stamped on every
    # volume of a multi-volume set, which would otherwise copy one volume's title onto all
    # its siblings (the bulk "detect from filename" clobber bug). The filename is PER FILE
    # and can't be cross-contaminated, so when the ISBN-resolved title is unrelated to the
    # filename's title (neither is a fold-substring of the other), the ISBN clearly doesn't
    # describe THIS file: ignore the lookup entirely and fall back to filename-only
    # detection. A compatible title (the ISBN expands a truncated filename) still wins.
    if auth_title and fn_title and not _title_compatible(auth_title, fn_title):
        return det
    auth = Detection(
        source="isbn:" + (meta.get("source") or "lookup"),
        confidence=0.98,                          # > filename's 0.95, so it wins in merge()
        title=auth_title,
        authors=[a.strip() for a in (meta.get("authors") or []) if a and a.strip()],
        translators=[t.strip() for t in (meta.get("translators") or []) if t and t.strip()],
        publisher=next((p for p in (meta.get("publishers") or []) if p), None),
        year=_year_from(meta.get("publish_date") or meta.get("year")),
        isbn=meta.get("isbn_13") or isbn)
    return merge([auth, det] if det else [auth])


def _title_compatible(a: str, b: str) -> bool:
    """Do two titles plausibly describe the SAME book? True when their fold-keys match
    or one contains the other (an ISBN record expanding a truncated filename title, or
    vice-versa). False for unrelated titles — the signal that an ISBN lookup landed on a
    different book than the file (a wrong / shared-across-volumes ISBN)."""
    from catalogue.db_store import fold_key
    ka, kb = fold_key(a or ""), fold_key(b or "")
    if not ka or not kb:
        return True                       # can't judge → don't block enrichment
    return ka in kb or kb in ka


def _year_from(value) -> Optional[int]:
    """A 4-digit year out of an OpenLibrary publish_date ('2009', 'September 2009')."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    m = _YEAR_RE.search(str(value))
    return int(m.group(1)) if m else None


# ── shared field parsers ──────────────────────────────────────────────────────
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$", re.I)


_ISBN_LABEL_RE = re.compile(r"(?i)^\s*isbn(?:[-\s]?1[03])?\s*[:#]?\s*")


def _norm_isbn(seg: str) -> Optional[str]:
    """Normalize a candidate ISBN segment to ISBN-13 via the project's validator
    (rejects non-ISBNs), else None. Strips an 'isbn13 '/'isbn10 '/'ISBN: ' label first —
    Anna's Archive labels the field that way, and the label's trailing '13'/'10' would
    otherwise glue onto the digit run and fail validation."""
    seg = _ISBN_LABEL_RE.sub("", (seg or "").strip())
    try:
        from .book_identifier import IsbnScheme
        return IsbnScheme().normalize(seg)
    except Exception:
        return None


def _looks_md5(seg: str) -> bool:
    return bool(_MD5_RE.match(seg.strip()))


def _split_year(seg: str):
    """('First Edition', 2009) from 'First Edition, 2009'; (stmt|None, year|None)."""
    m = _YEAR_RE.search(seg)
    year = int(m.group(1)) if m else None
    stmt = seg
    if m:
        stmt = (seg[:m.start()] + seg[m.end():])
    stmt = stmt.strip(" ,-–—\t")
    return (stmt or None), year


def _split_subtitle(title: str):
    """Anna's Archive encodes the ':' subtitle separator as '_ ' (a colon is illegal in
    filenames). 'Retreat_ Land of Medicine' → ('Retreat', 'Land of Medicine')."""
    if "_ " in title:
        head, sub = title.split("_ ", 1)
        head, sub = head.strip(), sub.strip()
        if head and sub:
            return head, sub
    return title.strip(), None


_DATE_TOKEN_RE = re.compile(r"^\(?(?:b\.?|d\.?|fl\.?|c\.?|ca\.?)?\s*\d{3,4}\s*[-–]?\s*\d{0,4}\)?\.?$", re.I)


def _is_date_token(p: str) -> bool:
    """A life-dates / year fragment that download filenames append to author lists
    ('1951-', '1570–1662', 'b. 1935') — never a person's name."""
    return bool(_DATE_TOKEN_RE.match(p.strip()))


def _names_from_chunk(chunk: str) -> list:
    """One ';'/'&'-delimited chunk → individual person names. Handles the comma forms
    download tools emit: 'Last, First', 'Last, First, Last2, First2' (paired), and a
    trailing life-date token; leaves comma-separated FULL names alone."""
    parts = [p.strip() for p in chunk.split(",") if p.strip()]
    parts = [p for p in parts if not _is_date_token(p)]        # drop '1951-' &c.
    if len(parts) <= 1:
        return parts
    if all(" " not in p for p in parts):                       # 'Last, First[, Last, First]'
        names, i = [], 0
        while i + 1 < len(parts):
            names.append(f"{parts[i + 1]} {parts[i]}")         # → 'First Last'
            i += 2
        if i < len(parts):
            names.append(parts[i])                             # odd leftover, as-is
        return names
    if len(parts) == 2 and " " in parts[0] and " " not in parts[1]:
        return [f"{parts[1]} {parts[0]}"]                      # 'García Márquez, Gabriel'
    return parts                                               # comma-separated full names


def _split_people(seg: str) -> list:
    """Split a contributor segment into individual names. First splits on ';'/'&' (the
    multi-person delimiters), then resolves each chunk's comma form via
    `_names_from_chunk`. Dupes dropped (fold-insensitive), order kept."""
    out, seen = [], set()
    for chunk in re.split(r"\s*[;&]\s*", seg or ""):
        for n in _names_from_chunk(chunk):
            n = n.strip().strip(",").strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
    return out


# ── Detector 1: Anna's Archive long form ───────────────────────────────────────
_ANNAS_RE = re.compile(r"ann?a.?s?\s*archive", re.I)   # Anna's / Annas / Anna’s Archive


def _is_annas_sig(seg: str) -> bool:
    return bool(_ANNAS_RE.search(seg.strip()))


@register
def annas_archive(filename: str) -> Optional[Detection]:
    """Parse the Anna's Archive long filename:
        Title_ Subtitle -- Author[; Author] -- [Edition, ]Year -- Publisher -- ISBN -- <md5> -- Anna's Archive

    The trailing 'Anna's Archive' tag is the signature; the 32-hex md5 and the ISBN are
    pattern-matched so missing/re-ordered middle fields are tolerated. Fields not present
    are simply left None."""
    stem = os.path.splitext(filename)[0]
    parts = [p.strip() for p in stem.split(" -- ")]
    if len(parts) < 2 or not any(_is_annas_sig(p) for p in parts):
        return None

    title, subtitle = _split_subtitle(parts[0])
    if not title:
        return None
    d = Detection(source="annas_archive", confidence=0.95, title=title, subtitle=subtitle)
    d.authors = _split_people(parts[1])

    # Classify the middle segments (between author and the trailing md5/signature) by
    # shape: pull out the ISBN and the year/edition; what's left over is publisher-ish.
    others = []
    for seg in parts[2:]:
        if _is_annas_sig(seg) or _looks_md5(seg):
            continue
        if d.isbn is None:
            isbn = _norm_isbn(seg)
            if isbn:
                d.isbn = isbn
                continue
        if d.year is None and _YEAR_RE.search(seg):
            d.edition_statement, d.year = _split_year(seg)
            continue
        others.append(seg)
    # Anna's column order is `… EditionYear -- Publisher -- ISBN -- md5`, so the leftover
    # CLOSEST to the ISBN end is the publisher; earlier leftovers tend to be series/place
    # junk. Take the last one.
    if others:
        d.publisher = others[-1]
    return d
