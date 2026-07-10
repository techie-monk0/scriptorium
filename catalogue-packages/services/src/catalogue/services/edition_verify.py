"""EditionVerifier — verify a book's inferred metadata against bibliographic
authorities, keyed by ISBN (preferred) or title + publisher (fallback).

This is the edition/manifestation counterpart to work_authority.py. Where the
work resolver answers "who wrote this *text*", the edition verifier checks the
fields your pipeline inferred about a specific *published book* — title, authors,
translators, publisher, year — against what Open Library / Google Books / … say,
and produces a per-field verdict (confirmed / mismatch / unverified) for review.

It is a *diff*, not an auto-merge: the report is for a human (or a confidence gate)
to act on. Mismatches are signal — e.g. an ISBN record listing the translator as
"author" is the normal shape of a translated classical text, not an error.

Modularity:
  - A *source* implements `EditionSource.by_isbn(...)` and/or
    `by_title_publisher(...)`, each returning `[EditionRecord]`.
  - Register more with `@register_source` or pass `sources=[…]`. The diff engine
    is source-agnostic.
  - Sources MUST NOT raise on network/parse failure — return [].
"""
from __future__ import annotations

import abc
import difflib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from catalogue.db_store import fold_key


def _acc(db):
    """A system Access over this connection — engine-routed edition reads/writes + the review
    queue. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)
from .isbn import normalize_isbn
from .isbn import lookup as ol_isbn_lookup
from .work_canonical_resolver import cached_rows
from catalogue.db_store import default_db_path

OpenerFn = Callable[[str, float], bytes]   # (url, timeout) -> body bytes


def _similar(a: str, b: str) -> float:
    ka, kb = fold_key(a or ""), fold_key(b or "")
    if not ka or not kb:
        return 0.0
    return difflib.SequenceMatcher(None, ka, kb).ratio()


def _year(value) -> Optional[int]:
    """Pull a 4-digit year out of '1997', '1997-03', 'March 1997', 1997, …."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if 800 <= value <= 2100 else None
    import re
    m = re.search(r"(1[0-9]{3}|20[0-9]{2})", str(value))
    return int(m.group(1)) if m else None


# ── Records & report ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EditionRecord:
    source: str
    title: Optional[str] = None
    authors: tuple = ()
    translators: tuple = ()
    publisher: Optional[str] = None
    year: Optional[int] = None
    isbn: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["authors"] = list(self.authors)
        d["translators"] = list(self.translators)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EditionRecord":
        d = dict(d)
        d["authors"] = tuple(d.get("authors") or ())
        d["translators"] = tuple(d.get("translators") or ())
        return cls(**d)


@dataclass
class FieldVerdict:
    field: str
    status: str            # confirmed | mismatch | unverified | authority_only
    inferred: object = None
    authority: list = field(default_factory=list)   # distinct authority values
    sources: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)      # list fields: set breakdown


@dataclass
class EditionReport:
    matched: bool
    by: str                # 'isbn' | 'title_publisher' | 'none'
    overall: str           # confirmed | partial | mismatch | unverified
    fields: list = field(default_factory=list)
    records: list = field(default_factory=list)

    def get_field(self, name) -> Optional[FieldVerdict]:   # convenience for callers/UI
        return next((f for f in self.fields if f.field == name), None)


# ── Source plug-in surface ──────────────────────────────────────────────────────
class EditionSource(abc.ABC):
    name: str = "source"
    version: int = 1

    def by_isbn(self, isbn: str) -> list:
        return []

    def by_lccn(self, lccn: str) -> list:
        return []

    def by_title_publisher(self, title: str, *, publisher: Optional[str] = None,
                           year: Optional[int] = None) -> list:
        return []


_SOURCES: dict = {}


def register_source(cls):
    _SOURCES[cls.name] = cls
    return cls


def build_sources(names=None, **kwargs) -> list:
    names = list(_SOURCES) if names is None else names
    return [_SOURCES[n](**kwargs) for n in names]


# ── The engine ──────────────────────────────────────────────────────────────────
class EditionVerifier:
    """Gather authority records (by ISBN, else by title+publisher) and diff them
    against the inferred fields. `db` is used only for the per-source cache."""

    def __init__(self, sources=None, *, db=None):
        self.sources = list(sources) if sources is not None else default_sources()
        self.db = db

    def _isbn(self, src, isbn):
        rows = cached_rows(self.db, namespace="edition_verify:isbn",
                           source=src.name, query=isbn, version=src.version,
                           compute=lambda: [r.to_dict() for r in src.by_isbn(isbn)])
        return [EditionRecord.from_dict(r) for r in rows]

    def _title_pub(self, src, title, publisher, year):
        key = f"{title}|{publisher or ''}|{year or ''}"
        rows = cached_rows(
            self.db, namespace="edition_verify:tp", source=src.name,
            query=key, version=src.version,
            compute=lambda: [r.to_dict() for r in src.by_title_publisher(
                title, publisher=publisher, year=year)])
        return [EditionRecord.from_dict(r) for r in rows]

    def verify(self, inferred: dict, *, isbn: Optional[str] = None) -> EditionReport:
        """`inferred` keys (all optional): title, authors[list], translators[list],
        publisher, year. `isbn` overrides inferred['isbn'] if given."""
        isbn = normalize_isbn(isbn or inferred.get("isbn") or "")
        title = (inferred.get("title") or "").strip()
        publisher = (inferred.get("publisher") or "").strip() or None
        year = _year(inferred.get("year"))

        records: list = []
        by = "none"
        if isbn:
            for src in self.sources:
                records.extend(self._isbn(src, isbn))
            if records:
                by = "isbn"
        if not records and title:
            for src in self.sources:
                records.extend(self._title_pub(src, title, publisher, year))
            if records:
                by = "title_publisher"

        if not records:
            return EditionReport(matched=False, by="none", overall="unverified",
                                 fields=[], records=[])

        fields = [
            _scalar_verdict("title", title, [r.title for r in records],
                            [r.source for r in records], fuzzy=True),
            _scalar_verdict("publisher", publisher, [r.publisher for r in records],
                            [r.source for r in records], fuzzy=True),
            _year_verdict(year, records),
            _list_verdict("authors", inferred.get("authors") or [], records, "authors"),
            _list_verdict("translators", inferred.get("translators") or [],
                          records, "translators"),
        ]
        return EditionReport(matched=True, by=by, overall=_overall(fields),
                             fields=fields, records=records)


# ── Field comparison ────────────────────────────────────────────────────────────
def _scalar_verdict(name, inferred, values, sources, *, fuzzy) -> FieldVerdict:
    authority = _dedup_str([v for v in values if v])
    src = sorted({s for s, v in zip(sources, values) if v})
    if not authority:
        return FieldVerdict(name, "unverified", inferred, [], src)
    if not inferred:
        return FieldVerdict(name, "authority_only", inferred, authority, src)
    if fuzzy:
        ok = any(_similar(inferred, v) >= 0.90 for v in authority)
    else:
        ok = any(fold_key(inferred) == fold_key(v) for v in authority)
    return FieldVerdict(name, "confirmed" if ok else "mismatch",
                        inferred, authority, src)


def _year_verdict(inferred, records) -> FieldVerdict:
    authority = _dedup([r.year for r in records if r.year])
    src = sorted({r.source for r in records if r.year})
    if not authority:
        return FieldVerdict("year", "unverified", inferred, [], src)
    if not inferred:
        return FieldVerdict("year", "authority_only", inferred, authority, src)
    ok = inferred in authority
    return FieldVerdict("year", "confirmed" if ok else "mismatch",
                        inferred, authority, src)


def _list_verdict(name, inferred_list, records, attr) -> FieldVerdict:
    """Compare two name sets by fold-key. confirmed = inferred ⊆ authority;
    mismatch = an inferred name absent from a non-empty authority set. Records the
    set breakdown (confirmed/inferred_only/authority_only) so a reviewer sees
    exactly what to fix or add."""
    inferred_keys = {fold_key(x): x for x in inferred_list if x and fold_key(x)}
    auth_map: dict = {}
    src = set()
    for r in records:
        for nm in getattr(r, attr):
            k = fold_key(nm)
            if k:
                auth_map.setdefault(k, nm)
                src.add(r.source)
    confirmed = [inferred_keys[k] for k in inferred_keys if k in auth_map]
    inferred_only = [inferred_keys[k] for k in inferred_keys if k not in auth_map]
    authority_only = [auth_map[k] for k in auth_map if k not in inferred_keys]
    detail = {"confirmed": confirmed, "inferred_only": inferred_only,
              "authority_only": authority_only}

    if not auth_map:
        status = "unverified"
    elif not inferred_keys:
        status = "authority_only"
    elif inferred_only:
        status = "mismatch"
    else:
        status = "confirmed"
    return FieldVerdict(name, status, list(inferred_list),
                        list(auth_map.values()), sorted(src), detail)


def _overall(fields) -> str:
    statuses = [f.status for f in fields]
    if any(s == "mismatch" for s in statuses):
        return "mismatch"
    if statuses and all(s in ("confirmed",) for s in statuses):
        return "confirmed"
    if any(s == "confirmed" for s in statuses):
        return "partial"
    return "unverified"


def _dedup_str(items) -> list:
    out, seen = [], set()
    for it in items:
        k = fold_key(it)
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _dedup(items) -> list:
    out, seen = [], set()
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ── Built-in sources ────────────────────────────────────────────────────────────
def _default_opener(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:   # noqa: S310
        return resp.read()


@register_source
class OpenLibrarySource(EditionSource):
    """Open Library — reuses catalogue/isbn.lookup for the ISBN path (already the
    capture lookup) and the keyless search.json endpoint for title+publisher.
    Open Library lumps translators into `authors`/`author_name`; the diff handles
    that (a translator shows as an author-set difference, not a hard error)."""
    name = "openlibrary"
    version = 1

    def __init__(self, opener: Optional[OpenerFn] = None, timeout: float = 5.0):
        self.opener = opener or _default_opener
        self.timeout = timeout

    def by_isbn(self, isbn):
        rec = ol_isbn_lookup(isbn, timeout=self.timeout, opener=self.opener)
        if not rec:
            return []
        return [EditionRecord(
            source=self.name, title=rec.get("title"),
            authors=tuple(rec.get("authors") or ()),
            publisher=(rec.get("publishers") or [None])[0],
            year=_year(rec.get("publish_date")),
            isbn=rec.get("isbn_13"))]

    def by_lccn(self, lccn):
        # Same Open Library api/books endpoint as the ISBN path, keyed by LCCN.
        return _ol_bibkey(f"LCCN:{lccn}", self.name,
                          timeout=self.timeout, opener=self.opener)

    def by_title_publisher(self, title, *, publisher=None, year=None):
        params = {"title": title, "limit": "5"}
        if publisher:
            params["publisher"] = publisher
        url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
        try:
            data = json.loads(self.opener(url, self.timeout).decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return []
        return _parse_ol_search(data, self.name)


def _ol_bibkey(bibkey: str, source: str, *, timeout, opener) -> list:
    """Open Library api/books lookup by an arbitrary bibkey ('LCCN:…', 'OCLC:…').
    Returns [EditionRecord] or [] on any failure (never raises)."""
    url = "https://openlibrary.org/api/books?" + urllib.parse.urlencode(
        {"bibkeys": bibkey, "format": "json", "jscmd": "data"})
    try:
        data = json.loads(opener(url, timeout).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []
    rec = (data or {}).get(bibkey) or {}
    if not rec:
        return []
    ids = rec.get("identifiers") or {}
    return [EditionRecord(
        source=source, title=rec.get("title"),
        authors=tuple(a.get("name") for a in rec.get("authors", []) if a.get("name")),
        publisher=([p.get("name") for p in rec.get("publishers", [])] or [None])[0],
        year=_year(rec.get("publish_date")),
        isbn=(ids.get("isbn_13") or [None])[0])]


def _parse_ol_search(data: dict, source: str) -> list:
    out = []
    for doc in (data or {}).get("docs", [])[:5]:
        out.append(EditionRecord(
            source=source, title=doc.get("title"),
            authors=tuple(doc.get("author_name") or ()),
            publisher=(doc.get("publisher") or [None])[0],
            year=_year(doc.get("first_publish_year")),
            isbn=(doc.get("isbn") or [None])[0]))
    return out


@register_source
class GoogleBooksSource(EditionSource):
    """Google Books — free JSON `volumes` API. Strong modern coverage; like OL it
    lumps translators into `authors`. Keyless calls are rate-limited but fine for
    interactive verification; pass an API key via `key` to raise the ceiling."""
    name = "googlebooks"
    version = 1

    def __init__(self, opener: Optional[OpenerFn] = None, timeout: float = 5.0,
                 key: Optional[str] = None):
        self.opener = opener or _default_opener
        self.timeout = timeout
        self.key = key

    def _query(self, q: str) -> list:
        params = {"q": q, "maxResults": "5"}
        if self.key:
            params["key"] = self.key
        url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)
        try:
            data = json.loads(self.opener(url, self.timeout).decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return []
        return _parse_gbooks(data, self.name)

    def by_isbn(self, isbn):
        return self._query(f"isbn:{normalize_isbn(isbn)}")

    def by_title_publisher(self, title, *, publisher=None, year=None):
        q = f'intitle:"{title}"'
        if publisher:
            q += f' inpublisher:"{publisher}"'
        return self._query(q)


def _parse_gbooks(data: dict, source: str) -> list:
    out = []
    for item in (data or {}).get("items", [])[:5]:
        vi = item.get("volumeInfo") or {}
        ids = vi.get("industryIdentifiers") or []
        isbn13 = next((i.get("identifier") for i in ids
                       if i.get("type") == "ISBN_13"), None)
        out.append(EditionRecord(
            source=source, title=vi.get("title"),
            authors=tuple(vi.get("authors") or ()),
            publisher=vi.get("publisher"),
            year=_year(vi.get("publishedDate")),
            isbn=isbn13))
    return out


def default_sources() -> list:
    """Open Library first (already wired + keyless), then Google Books."""
    return [OpenLibrarySource(), GoogleBooksSource()]


def _batch_sources() -> list:
    """Bulk-run sources whose HTTP opener throttles + retries 429/5xx, so a corpus
    sweep's rate-limits aren't mistaken for misses (mirrors edition_resolve)."""
    from .http_util import ThrottledOpener
    op = ThrottledOpener()
    return [OpenLibrarySource(opener=op), GoogleBooksSource(opener=op)]


# ── Wiring: per-edition pass · review queue · accept/reject · CLI ─────────────────
# The engine above produces a *diff*; this turns it into reviewable work. We walk
# editions, build each one's inferred metadata from the catalogue (its own
# title/publisher/year/isbn + the author/translator persons of the works it
# contains), diff it against the authorities, and queue an `edition_verify` review
# item ONLY when the diff is actionable — a field MISMATCH a human should adjudicate
# or an `authority_only` value we could safely backfill. Pure confirmations and
# "authority silent" reports are dropped (nothing to do). Accept performs the one
# safe, additive, reversible write — backfilling a genuinely-empty publisher/year
# from an unambiguous authority value — and records `applied_fills` for audit. It
# NEVER overwrites a populated field or touches title/authors/translators (a diff
# there is a human call, per this module's "diff, not auto-merge" contract).

def _edition_contributors(db, eid: int) -> tuple:
    """(authors, translators) display names inferred for an edition. Authors come
    from the `work_contributor` rows of every work the edition contains;
    translators from those rows PLUS each `edition_work.translator_person_id` (the
    book-level translator). Deduped by fold-key, order-stable."""
    authors, translators = _acc(db).editions.reads.contributor_names(eid)
    return _dedup_str(authors), _dedup_str(translators)


def _inferred_for_edition(db, eid: int) -> dict:
    ed = _acc(db).editions.reads.get(eid)
    if not ed:
        return {}
    authors, translators = _edition_contributors(db, eid)
    return {"title": ed.title or "", "publisher": ed.publisher, "year": ed.year,
            "isbn": ed.isbn, "authors": authors, "translators": translators}


def _is_actionable(report: EditionReport) -> bool:
    """A diff is worth a human's time when an authority CONTRADICTS us (mismatch) or
    SUPPLIES a value we're missing (authority_only). All-confirmed / authority-silent
    reports are dropped."""
    return report.matched and any(
        f.status in ("mismatch", "authority_only") for f in report.fields)


def _report_payload(eid: int, report: EditionReport) -> dict:
    return {
        "edition_id": eid,
        "by": report.by,
        "overall": report.overall,
        "fields": [{"field": f.field, "status": f.status, "inferred": f.inferred,
                    "authority": f.authority, "sources": f.sources, "detail": f.detail}
                   for f in report.fields],
        "records": [r.to_dict() for r in report.records],
    }


def _ev_already_queued(db, eid: int) -> bool:
    return _acc(db).review.reads.exists_pending(
        "edition_verify", f'%"edition_id": {eid}%')


def verify_edition(db, verifier: "EditionVerifier", eid: int, *,
                   commit: bool = True) -> str:
    """already | actionable | clean | no_authority | missing. Queues an
    `edition_verify` review item once per edition only when the diff is actionable."""
    if _ev_already_queued(db, eid):
        return "already"
    inferred = _inferred_for_edition(db, eid)
    if not inferred:
        return "missing"
    report = verifier.verify(inferred, isbn=inferred.get("isbn"))
    if not report.matched:
        return "no_authority"
    if not _is_actionable(report):
        return "clean"
    _acc(db).review.writes.enqueue("edition_verify", _report_payload(eid, report))
    if commit:
        db.commit()
    return "actionable"


def verify_all_editions(db, verifier: "Optional[EditionVerifier]" = None, *,
                        limit: Optional[int] = None, verbose: bool = False) -> dict:
    """Walk every edition through the verifier, queuing actionable diffs. Commits
    per row (resumable). Returns a status tally."""
    verifier = verifier or EditionVerifier(sources=_batch_sources(), db=db)
    ids = sorted(_acc(db).editions.reads.all_ids())
    if limit:
        ids = ids[:int(limit)]
    tally = {"actionable": 0, "clean": 0, "no_authority": 0, "already": 0,
             "missing": 0}
    if verbose:
        print(f"Edition metadata verify over {len(ids)} edition(s) "
              f"via [{', '.join(s.name for s in verifier.sources)}]…", flush=True)
    for i, eid in enumerate(ids, 1):
        status = verify_edition(db, verifier, eid, commit=True)
        tally[status] += 1
        if verbose:
            mark = {"actionable": "?", "clean": "✓", "no_authority": "·",
                    "already": "»", "missing": "✗"}[status]
            ed = _acc(db).editions.reads.get(eid)
            print(f"  [{i}/{len(ids)}] {mark} {status:12} {((ed.title if ed else '') or '')[:60]}",
                  flush=True)
    if verbose:
        print(f"done: {tally}", flush=True)
    return tally


# ── Acting on a queued diff (the /review accept/reject UI) ────────────────────────
_BACKFILL_FIELDS = ("publisher", "year")


def _safe_fills(db, eid: int, payload: dict) -> dict:
    """The only writes accept is allowed: fill an EMPTY edition publisher/year from
    an `authority_only` verdict carrying exactly one distinct authority value. Never
    overwrites a populated column; never touches title/authors/translators."""
    ed = _acc(db).editions.reads.get(eid)
    if not ed:
        return {}
    current = {"publisher": ed.publisher, "year": ed.year}
    fills: dict = {}
    for f in payload.get("fields", []):
        name = f.get("field")
        if name not in _BACKFILL_FIELDS or f.get("status") != "authority_only":
            continue
        if current.get(name):                       # already populated → never clobber
            continue
        distinct = list(dict.fromkeys(
            v for v in (f.get("authority") or []) if v not in (None, "")))
        if len(distinct) == 1:                       # only an unambiguous value
            fills[name] = distinct[0]
    return fills


def accept_edition_verify(db, item_id: int, *, commit: bool = True) -> bool:
    """Acknowledge a diff and apply the one safe, additive write (backfill an empty
    publisher/year from an unambiguous authority value). Records `applied_fills` for
    audit and marks the item resolved. False if missing/not pending."""
    review = _acc(db).review
    row = review.reads.get_typed(item_id, "edition_verify")
    if not row or row[1] != "pending":
        return False
    p = json.loads(row[0])
    eid = p.get("edition_id")
    fills = _safe_fills(db, eid, p) if eid else {}
    if fills:
        _acc(db).editions.writes.set_columns(eid, fills)
        p["applied_fills"] = fills
        review.writes.set_payload(item_id, p)
    review.writes.resolve(item_id)
    if commit:
        db.commit()
    return True


def reject_edition_verify(db, item_id: int, *, commit: bool = True) -> bool:
    """Dismiss a diff without writing anything (queue-time wrote nothing). False if
    missing/not pending."""
    if _acc(db).review.reads.status_of(item_id, "edition_verify") != "pending":
        return False
    _acc(db).review.writes.reject(item_id)
    if commit:
        db.commit()
    return True


def main(argv=None) -> None:
    import argparse
    from catalogue.db_store import init_db
    ap = argparse.ArgumentParser(
        description="Diff each edition's inferred metadata against Open Library / "
                    "Google Books and queue an 'edition_verify' review item per "
                    "actionable diff (mismatch or backfillable value).")
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-row progress (just the final tally)")
    args = ap.parse_args(argv)
    db = init_db(args.db)
    db.execute("PRAGMA busy_timeout = 30000")
    tally = verify_all_editions(db, limit=args.limit, verbose=not args.quiet)
    print("summary:", tally)


if __name__ == "__main__":
    main()
