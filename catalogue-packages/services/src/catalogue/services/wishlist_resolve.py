"""Resolve a wishlist input (ISBN / typed title+author / CIP text) into a metadata snapshot.

A wishlist item is added one of three ways; each must end up with the same shape so the access-API
can persist it identically and the client can show a card. This module is PURE ORCHESTRATION over
the existing intake services — it adds no lookup logic of its own:

  - ISBN  → isbn.normalize/validate → isbn.lookup + isbn.work_key_for_isbn → snapshot
  - CIP   → cip.parse_cip → enrich the first valid ISBN via isbn.lookup → snapshot
  - title → isbn.search_by_title → candidate list (one ⇒ resolved, many ⇒ ambiguous)

Every path also runs the cross-format `intake_match` verdict so a book already in the catalogue is
flagged `owned` (with the matched edition) rather than silently wishlisted twice. Best-effort: a
network miss degrades to a lower-confidence status, never an exception. The fetchers are injectable
(default to the real `isbn` client) so tests stay offline. The returned `Resolution` is consumed by
`acc.wishlist.add(...)`; this module performs no DB writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from catalogue.services import cip, intake_match, isbn as isbn_svc


@dataclass
class Resolution:
    """The outcome of resolving one wishlist input.

    `status`   — unresolved | resolved | ambiguous | owned
    `snapshot` — resolved-metadata dict for `acc.wishlist.add(snapshot=...)` (DTO keys: title,
                 subtitle, authors, publisher, year, isbn, ol_work_key, lccn, cover_url,
                 candidates, matched_edition_id)
    `verdict`  — the `intake_match` cross-format dict (surfaced to the client as the dedupe warning)
    """
    status: str
    snapshot: dict = field(default_factory=dict)
    verdict: dict = field(default_factory=dict)


def _cover_for(isbn: "str | None") -> "str | None":
    """OpenLibrary's deterministic cover URL for an ISBN (no network call to build it)."""
    return f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg" if isbn else None


def _owned_status(verdict: dict, snapshot: dict) -> str:
    """If the cross-format verdict says we already hold this book, mark it `owned` and pin the
    matched edition onto the snapshot; otherwise leave the caller's status unchanged."""
    if verdict.get("in_catalogue") and verdict.get("editions"):
        snapshot["matched_edition_id"] = verdict["editions"][0]["id"]
        return "owned"
    return ""


def resolve_isbn(db, raw_isbn: str, *,
                 isbn_lookup: "Optional[Callable]" = None,
                 work_key_fetch: "Optional[Callable]" = None) -> Resolution:
    """Resolve a typed/scanned ISBN. Invalid checksum ⇒ `unresolved` (the raw value is still kept by
    the caller so the operator can fix it)."""
    norm = isbn_svc.normalize_isbn(raw_isbn or "")
    if not isbn_svc.validate_isbn13(norm):
        return Resolution("unresolved", {}, {"in_catalogue": False, "matched_by": None,
                                             "editions": [], "uncertain": []})
    lookup = isbn_lookup or isbn_svc.lookup
    fetch = work_key_fetch or isbn_svc.make_fetch()
    meta = _safe(lambda: lookup(norm)) or {}
    work_key = _safe(lambda: fetch(norm))
    snapshot = {
        "title": meta.get("title"),
        "authors": meta.get("authors", []),
        "publisher": (meta.get("publishers") or [None])[0],
        "year": _year(meta.get("publish_date")),
        "isbn": norm,
        "ol_work_key": work_key,
        "cover_url": meta.get("cover_url") or _cover_for(norm),
    }
    verdict = intake_match.catalogue_verdict(
        db, norm, ol_work_key_fetch=fetch, isbn_lookup=lookup)
    status = _owned_status(verdict, snapshot) or ("resolved" if snapshot["title"] else "unresolved")
    return Resolution(status, snapshot, verdict)


def resolve_cip(db, cip_text: str, *,
                isbn_lookup: "Optional[Callable]" = None,
                work_key_fetch: "Optional[Callable]" = None) -> Resolution:
    """Resolve a copyright-page OCR block: parse the CIP, enrich the first checksum-valid ISBN it
    carries, and run the CIP dedupe verdict. No parsable CIP ⇒ `unresolved`."""
    record = cip.parse_cip(cip_text or "")
    if record is None:
        return Resolution("unresolved", {}, {"in_catalogue": False, "matched_by": None,
                                             "editions": [], "uncertain": []})
    lookup = isbn_lookup or isbn_svc.lookup
    fetch = work_key_fetch or isbn_svc.make_fetch()
    primary_isbn = next((i for i in (record.isbns or []) if isbn_svc.validate_isbn13(i)), None)
    meta = _safe(lambda: lookup(primary_isbn)) if primary_isbn else None
    meta = meta or {}
    work_key = _safe(lambda: fetch(primary_isbn)) if primary_isbn else None
    snapshot = {
        "title": meta.get("title") or record.title,
        "authors": meta.get("authors") or list(record.authors or []),
        "publisher": (meta.get("publishers") or [record.publisher])[0],
        "year": _year(meta.get("publish_date")) or record.year,
        "isbn": primary_isbn,
        "ol_work_key": work_key,
        "lccn": record.lccn,
        "cover_url": meta.get("cover_url") or _cover_for(primary_isbn),
    }
    verdict = intake_match.cip_verdict(
        db, record, ol_work_key_fetch=fetch, isbn_lookup=lookup)
    status = _owned_status(verdict, snapshot) or ("resolved" if snapshot["title"] else "unresolved")
    return Resolution(status, snapshot, verdict)


def resolve_title(db, title: str, author: "Optional[str]" = None, *,
                  title_search: "Optional[Callable]" = None) -> Resolution:
    """Resolve a typed title (+ optional author). First a LOCAL "do I already own this?" check, then
    an OpenLibrary title search: one candidate ⇒ `resolved`, several ⇒ `ambiguous` (the candidates
    are stored for the operator to pick), none ⇒ `unresolved`."""
    title = (title or "").strip()
    if not title:
        return Resolution("unresolved", {}, {"in_catalogue": False, "editions": []})

    # Local dedupe (no network): title-containment + shared author against the catalogue.
    meta = {"title": title, "authors": [author] if author else []}
    held = intake_match.editions_now_holding(db, meta=meta)
    if held:
        snapshot = {"title": title, "authors": meta["authors"], "matched_edition_id": held[0]["id"]}
        return Resolution("owned", snapshot,
                          {"in_catalogue": True, "matched_by": "title", "editions": held})

    search = title_search or isbn_svc.search_by_title
    candidates = _safe(lambda: search(title, author)) or []
    if not candidates:
        # Nothing found — keep what the user typed so the card still shows something.
        return Resolution("unresolved", {"title": title, "authors": meta["authors"]},
                          {"in_catalogue": False, "editions": []})
    if len(candidates) == 1:
        return Resolution("resolved", _snapshot_from_candidate(candidates[0]),
                          {"in_catalogue": False, "editions": []})
    # Several hits — store the typed title + the candidate list for the operator to disambiguate.
    snap = {"title": title, "authors": meta["authors"], "candidates": candidates}
    return Resolution("ambiguous", snap, {"in_catalogue": False, "editions": []})


def snapshot_from_candidate(candidate: dict) -> dict:
    """Public helper: turn one `search_by_title` candidate into a resolved snapshot — used when the
    operator PICKs a candidate of an `ambiguous` item (PATCH /api/v1/wishlist/<id>)."""
    return _snapshot_from_candidate(candidate)


# ── internals ────────────────────────────────────────────────────────────────
def _snapshot_from_candidate(c: dict) -> dict:
    isbn = c.get("isbn_13")
    return {
        "title": c.get("title"),
        "authors": c.get("authors", []),
        "publisher": c.get("publisher"),
        "year": c.get("year"),
        "isbn": isbn,
        "ol_work_key": c.get("ol_work_key"),
        "cover_url": c.get("cover_url") or _cover_for(isbn),
    }


def _year(publish_date: "str | None") -> "int | None":
    """Pull a 4-digit year out of an OpenLibrary free-text publish_date ('March 2001', '2001', …)."""
    import re
    if not publish_date:
        return None
    m = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", str(publish_date))
    return int(m.group(1)) if m else None


def _safe(fn):
    """Run a best-effort fetch, swallowing any error (network/timeout/parse) to None."""
    try:
        return fn()
    except Exception:
        return None
