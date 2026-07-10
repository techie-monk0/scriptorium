"""ISBN-13 validation + Open Library lookup (§7.3, §11).

`validate_isbn13` runs the checksum locally (no network). `lookup` queries
Open Library's keyless `/api/books?jscmd=data` endpoint via stdlib urllib
so we keep the dependency footprint small. Both the opener and the
callable are injectable for tests — no real network in CI.

Failure mode is always "return None, never raise" — the capture endpoint
falls back to the manual photo path on any lookup miss.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional


# ── Validation ────────────────────────────────────────────────────────────
def normalize_isbn(raw: str) -> str:
    """Strip everything that isn't a digit — barcode scanners often emit
    hyphens or stray whitespace, and humans add hyphens for readability."""
    return "".join(c for c in (raw or "") if c.isdigit())


def validate_isbn13(raw: str) -> bool:
    """ISBN-13 checksum: weights alternate 1,3,1,3,…; sum % 10 == 0."""
    digits = normalize_isbn(raw)
    if len(digits) != 13:
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    return total % 10 == 0


# ── Open Library client ──────────────────────────────────────────────────
OpenerFn = Callable[[str, float], bytes]  # (url, timeout) → response body


def _default_opener(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _cover_for_isbn(isbn: "Optional[str]") -> "Optional[str]":
    """OpenLibrary's deterministic cover URL for an ISBN (no network call to build it)."""
    return f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg" if isbn else None


# ── Google Books fallback ────────────────────────────────────────────────────
# OpenLibrary is the primary source, but it lacks many newer US titles — notably 979-prefix ISBNs
# (e.g. Wisdom Publications 2024+) and their covers. Google Books carries those, so both the ISBN
# `lookup` and the title `search_by_title` fall through to it on a miss. Same never-raises,
# injectable-opener contract; results are normalized to the SAME shapes as the OpenLibrary paths.
_GOOGLE_VOLUMES = "https://www.googleapis.com/books/v1/volumes"


def _year_of(publish_date: "Optional[str]") -> "Optional[int]":
    """First 4-digit year in a Google `publishedDate` ('2024', '2024-05-01', 'May 2024')."""
    m = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", str(publish_date or ""))
    return int(m.group(1)) if m else None


def _https(url: "Optional[str]") -> "Optional[str]":
    """Upgrade Google's `http://` cover links to https so they aren't blocked as mixed content."""
    return ("https://" + url[len("http://"):]) if url and url.startswith("http://") else url


def _google_volume_info(vi: dict) -> dict:
    """One Google `volumeInfo` → the shared metadata shape (keys align with the OL paths)."""
    ids = vi.get("industryIdentifiers") or []
    isbn13 = next((normalize_isbn(x.get("identifier"))
                   for x in ids if isinstance(x, dict) and x.get("type") == "ISBN_13"
                   and validate_isbn13(x.get("identifier") or "")), None)
    images = vi.get("imageLinks") or {}
    return {
        "title": vi.get("title"),
        "authors": [a for a in (vi.get("authors") or []) if a],
        "publishers": [vi["publisher"]] if vi.get("publisher") else [],
        "publish_date": vi.get("publishedDate"),
        "isbn_13": isbn13,
        "cover_url": _https(images.get("thumbnail") or images.get("smallThumbnail")),
        "source": "googlebooks",
    }


def _google_items(query: str, limit: int, timeout: float, opener: OpenerFn) -> list[dict]:
    params = {"q": query, "maxResults": str(min(40, max(1, limit)))}
    # The unauthenticated Books API is heavily rate-limited (HTTP 429). If a GOOGLE_BOOKS_API_KEY is
    # configured (env or api_key.txt), send it for the higher quota; without one this is best-effort.
    try:
        from catalogue.services import apikeys
        key = apikeys.get("GOOGLE_BOOKS_API_KEY")
        if key:
            params["key"] = key
    except Exception:
        pass
    url = _GOOGLE_VOLUMES + "?" + urllib.parse.urlencode(params)
    try:
        data = json.loads(opener(url, timeout).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [it.get("volumeInfo") or {} for it in (items or []) if isinstance(it, dict)]


def _google_lookup(isbn: str, timeout: float, opener: OpenerFn) -> "Optional[dict]":
    """ISBN → metadata via Google Books, or None. `lookup`'s OL-miss fallback."""
    infos = _google_items(f"isbn:{isbn}", 1, timeout, opener)
    if not infos:
        return None
    meta = _google_volume_info(infos[0])
    meta["isbn_13"] = meta["isbn_13"] or isbn
    return meta if meta.get("title") else None


def _google_search(title: str, author: "Optional[str]", limit: int,
                   timeout: float, opener: OpenerFn) -> list[dict]:
    """Title (+ author) → candidate list via Google Books. `search_by_title`'s OL-miss fallback."""
    q = f"intitle:{title}" + (f" inauthor:{author.strip()}" if author and author.strip() else "")
    out: list[dict] = []
    for vi in _google_items(q, limit, timeout, opener):
        m = _google_volume_info(vi)
        if not m.get("title"):
            continue
        out.append({
            "title": m["title"], "authors": m["authors"],
            "publisher": (m["publishers"] or [None])[0], "year": _year_of(m["publish_date"]),
            "isbn_13": m["isbn_13"], "ol_work_key": None,
            "cover_url": m["cover_url"] or _cover_for_isbn(m["isbn_13"]), "source": "googlebooks",
        })
    return out[:limit]


def lookup(isbn: str, *, timeout: float = 5.0,
           opener: Optional[OpenerFn] = None) -> Optional[dict]:
    """Return a small dict of metadata, or None on any failure (404,
    timeout, malformed JSON, empty record).

    Shape (all keys optional, all strings):
      {title, authors[list[str]], publishers[list[str]],
       publish_date, isbn_13, source='openlibrary'}
    """
    isbn = normalize_isbn(isbn)
    if not validate_isbn13(isbn):
        return None
    opener = opener or _default_opener
    url = (
        "https://openlibrary.org/api/books?"
        + urllib.parse.urlencode({
            "bibkeys": f"ISBN:{isbn}",
            "format": "json",
            "jscmd": "data",
        })
    )
    try:
        body = opener(url, timeout)
        data = json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        data = {}

    rec = (data.get(f"ISBN:{isbn}") if isinstance(data, dict) else None) or {}
    if rec:
        return {
            "title": rec.get("title"),
            "authors": [a.get("name") for a in rec.get("authors", []) if a.get("name")],
            "publishers": [p.get("name") for p in rec.get("publishers", []) if p.get("name")],
            "publish_date": rec.get("publish_date"),
            "isbn_13": isbn,
            "cover_url": None,          # OL covers are deterministic (covers.openlibrary.org)
            "source": "openlibrary",
        }
    # OpenLibrary miss — fall back to Google Books, which carries many 979-prefix ISBNs (newer
    # US titles) that OL lacks, plus a cover thumbnail. Same never-raises, injectable-opener contract.
    return _google_lookup(isbn, timeout, opener)


def work_key_for_isbn(isbn: str, *, timeout: float = 5.0,
                      opener: Optional[OpenerFn] = None) -> Optional[str]:
    """Resolve an ISBN to its OpenLibrary *work* key ('/works/OL…W'), or None.

    The `jscmd=data` endpoint used by `lookup` does NOT carry the work key; the
    edition record at `/isbn/{isbn}.json` does (`{"works":[{"key":"/works/OL…W"}]}`).
    The work key clusters editions of one work across formats (print/epub/pdf carry
    DIFFERENT ISBNs), so a phone scan of one format can detect we already hold another.

    Same contract as `lookup`: injectable opener, never raises — any failure (404,
    timeout, malformed JSON, missing key) returns None so the capture path is unaffected.
    """
    isbn = normalize_isbn(isbn)
    if not validate_isbn13(isbn):
        return None
    url = f"https://openlibrary.org/isbn/{isbn}.json"
    opener = opener or _default_opener
    try:
        body = opener(url, timeout)
        data = json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    works = data.get("works") if isinstance(data, dict) else None
    if not isinstance(works, list) or not works:
        return None
    key = works[0].get("key") if isinstance(works[0], dict) else None
    return key if isinstance(key, str) and key.startswith("/works/") else None


def search_by_title(title: str, author: "Optional[str]" = None, *, limit: int = 5,
                    timeout: float = 5.0, opener: Optional[OpenerFn] = None) -> list[dict]:
    """Search OpenLibrary by title (+ optional author) and return up to `limit` candidate
    editions — the resolution path for a wishlist item added by typed title/author (no ISBN).

    Queries the keyless `/search.json` endpoint via stdlib urllib, same dependency-free,
    never-raises contract as `lookup`: any failure (network, timeout, malformed JSON) returns
    `[]`. Each candidate is a small dict (keys optional):
      {title, authors[list[str]], publisher, year, isbn_13, ol_work_key, source='openlibrary'}
    `isbn_13`/`ol_work_key` let a picked candidate flow straight into the cross-format verdict.
    """
    title = (title or "").strip()
    if not title:
        return []
    params = {"title": title, "limit": str(max(1, limit)),
              "fields": "title,author_name,first_publish_year,publisher,isbn,key"}
    if author and author.strip():
        params["author"] = author.strip()
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
    opener = opener or _default_opener
    try:
        body = opener(url, timeout)
        data = json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        data = {}
    docs = data.get("docs") if isinstance(data, dict) else None
    out: list[dict] = []
    for doc in (docs or [])[:limit]:
        if not isinstance(doc, dict):
            continue
        # First checksum-valid ISBN-13 the doc carries (search returns a mixed isbn list).
        isbn13 = next((normalize_isbn(i) for i in (doc.get("isbn") or [])
                       if validate_isbn13(i)), None)
        key = doc.get("key")
        out.append({
            "title": doc.get("title"),
            "authors": [a for a in (doc.get("author_name") or []) if a],
            "publisher": next((p for p in (doc.get("publisher") or []) if p), None),
            "year": doc.get("first_publish_year"),
            "isbn_13": isbn13,
            "ol_work_key": key if isinstance(key, str) and key.startswith("/works/") else None,
            "cover_url": _cover_for_isbn(isbn13),   # deterministic OL cover from the candidate's ISBN
            "source": "openlibrary",
        })
    if out:
        return out
    # OpenLibrary found nothing — fall back to Google Books title search (covers + 979 ISBNs).
    return _google_search(title, author, limit, timeout, opener)


def make_fetch(opener: "Optional[OpenerFn]" = None) -> "Callable[[str], Optional[str]]":
    """An isbn → work-key callable backed by the throttled OpenLibrary client.

    The default opener is the polite ThrottledOpener (spaces requests, retries 429/5xx)
    so a bulk backfill is not mistaken for a string of 'no record' misses. ThrottledOpener
    is imported lazily so this module's import graph stays free of the HTTP client. Lives
    here (not in the CLI) so callers in `domain` — e.g. `sweep` — don't reach up into `cli`.
    """
    if opener is None:
        from .http_util import ThrottledOpener
        opener = ThrottledOpener()
    return lambda isbn: work_key_for_isbn(isbn, opener=opener)
