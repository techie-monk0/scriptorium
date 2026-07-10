"""Entity DTOs — the read shapes the access-API returns and serializes to clients.

Frozen, no behavior beyond identity + (de)serialization. More aggregates join here as their
entity modules land. See docs/access/entity_api_model.md §3/§4.
"""
from __future__ import annotations

from dataclasses import dataclass

from .refs import Ref


def edition_fingerprint(title: "str | None", isbn: "str | None") -> str:
    """An Edition's identity fingerprint (roster: title-fold + isbn). Stable function of the
    identity columns, so a stale `Ref` to a recycled edition id is caught at re-check rather than
    silently rebinding (the id-reuse guard). One source of truth for reader and writer."""
    return f"{(title or '').strip()}|{(isbn or '').strip()}"


@dataclass(frozen=True)
class Edition:
    """A published manifestation (one book): the parent of holdings, linked to works."""
    id: int
    title: str
    subtitle: "str | None" = None
    isbn: "str | None" = None
    year: "int | None" = None
    publisher: "str | None" = None
    tradition: "str | None" = None            # Buddhist tradition (a `tradition.name`); editable override
    rev: int = 0                              # optimistic-concurrency version (bumped on update)

    def ref(self) -> Ref:
        return Ref("edition", self.id, edition_fingerprint(self.title, self.isbn), rev=self.rev)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "subtitle": self.subtitle,
                "isbn": self.isbn, "year": self.year, "publisher": self.publisher,
                "tradition": self.tradition, "rev": self.rev}

    @classmethod
    def from_dict(cls, d: dict) -> "Edition":
        return cls(d["id"], d["title"], d.get("subtitle"), d.get("isbn"),
                   d.get("year"), d.get("publisher"), d.get("tradition"), d.get("rev", 0))


def work_fingerprint(title: "str | None", author_ids: "tuple[int, ...] | None") -> str:
    """A Work's identity fingerprint (roster: title-fold + author set). A work has no title column
    — its title is its representative alias — and its identity is "this composition by these
    authors", so the fingerprint folds the title (case + whitespace) and pins the sorted author
    person-ids. A recycled work id (a different composition inheriting the id) lands a different
    title and/or author set, so a stale `Ref` is caught at re-check (the id-reuse guard). One source
    of truth for reader and writer; self-consistent by construction."""
    name = " ".join((title or "").split()).casefold()
    authors = ",".join(str(a) for a in sorted(author_ids or ()))
    return f"{name}|{authors}"


@dataclass(frozen=True)
class Work:
    """A composition (FRBR work): shared across editions (one text → many manifestations). Owns its
    aliases; edges to authors, editions (`edition_work`), subjects/traditions/collections, and
    work↔work relationships. Identity = title-fold + author set."""
    id: int
    title: "str | None" = None              # representative alias (display + fingerprint input)
    canonical_system: "str | None" = None
    canonical_number: "str | None" = None
    author_ids: tuple = ()                   # author person-ids (role='author'), identity input
    tradition: "str | None" = None           # Buddhist tradition (a `tradition.name`); editable
    genre: "str | None" = None               # rhetorical genre (contracts.fields GENRE_VALUES); editable
    rev: int = 0                             # optimistic-concurrency version (bumped on update)

    def ref(self) -> Ref:
        return Ref("work", self.id, work_fingerprint(self.title, self.author_ids), rev=self.rev)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "canonical_system": self.canonical_system,
                "canonical_number": self.canonical_number, "author_ids": list(self.author_ids),
                "tradition": self.tradition, "genre": self.genre, "rev": self.rev}

    @classmethod
    def from_dict(cls, d: dict) -> "Work":
        return cls(d["id"], d.get("title"), d.get("canonical_system"),
                   d.get("canonical_number"), tuple(d.get("author_ids", ())),
                   d.get("tradition"), d.get("genre"), d.get("rev", 0))


def person_fingerprint(primary_name: "str | None", dates: "str | None") -> str:
    """A Person's identity fingerprint (roster: name-fold + dates) — the `person_identity_ok`
    id-reuse guard generalized to a `Ref`. Folds the name (case + whitespace) so trivial
    reformatting is not a false `StaleWrite`, and pins `dates` so a recycled id (a different
    person inheriting this id) is caught at re-check. Self-consistent by construction (one source
    for reader and writer) — it need not equal `db_store.fold_key`, which folds harder to serve the
    review-queue bind guard; this only has to detect identity drift on the same id."""
    name = " ".join((primary_name or "").split()).casefold()
    return f"{name}|{(dates or '').strip()}"


@dataclass(frozen=True)
class Person:
    """A contributor (author / translator / …): the authority root for works and editions.
    Identity = name-fold + dates (the [[sqlite-id-reuse-hazard]] guard)."""
    id: int
    primary_name: str
    role_hint: "str | None" = None
    dates: "str | None" = None
    external_id: "str | None" = None
    verification_status: "str | None" = None
    notes: "str | None" = None
    tradition: "str | None" = None            # author's lineage (a `tradition.name`); editable
    tenet_system: "str | None" = None         # doctrinal/siddhānta home (contracts.fields); editable
    rev: int = 0                              # optimistic-concurrency version (bumped on update)

    def ref(self) -> Ref:
        return Ref("person", self.id, person_fingerprint(self.primary_name, self.dates), rev=self.rev)

    def to_dict(self) -> dict:
        return {"id": self.id, "primary_name": self.primary_name, "role_hint": self.role_hint,
                "dates": self.dates, "external_id": self.external_id,
                "verification_status": self.verification_status, "notes": self.notes,
                "tradition": self.tradition, "tenet_system": self.tenet_system, "rev": self.rev}

    @classmethod
    def from_dict(cls, d: dict) -> "Person":
        return cls(d["id"], d["primary_name"], d.get("role_hint"), d.get("dates"),
                   d.get("external_id"), d.get("verification_status"), d.get("notes"),
                   d.get("tradition"), d.get("tenet_system"), d.get("rev", 0))


@dataclass(frozen=True)
class Subject:
    """A topical (or series) heading attached to works/editions. Identity = its unique name."""
    id: int
    name: str
    kind: str = "topic"
    rev: int = 0                              # optimistic-concurrency version (bumped on update)

    def ref(self) -> Ref:
        return Ref("subject", self.id, self.name, rev=self.rev)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "rev": self.rev}

    @classmethod
    def from_dict(cls, d: dict) -> "Subject":
        return cls(d["id"], d["name"], d.get("kind", "topic"), d.get("rev", 0))


@dataclass(frozen=True)
class Collection:
    """A named grouping of works. Identity = its unique name."""
    id: int
    name: str
    rev: int = 0                              # optimistic-concurrency version (bumped on update)

    def ref(self) -> Ref:
        return Ref("collection", self.id, self.name, rev=self.rev)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "rev": self.rev}

    @classmethod
    def from_dict(cls, d: dict) -> "Collection":
        return cls(d["id"], d["name"], d.get("rev", 0))


@dataclass(frozen=True)
class Tradition:
    """A lineage/tradition tag on works. Identity = its unique name."""
    id: int
    name: str
    rev: int = 0                              # optimistic-concurrency version (bumped on update)

    def ref(self) -> Ref:
        return Ref("tradition", self.id, self.name, rev=self.rev)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "rev": self.rev}

    @classmethod
    def from_dict(cls, d: dict) -> "Tradition":
        return cls(d["id"], d["name"], d.get("rev", 0))


def wishlist_fingerprint(source: "str | None", raw_isbn: "str | None",
                         raw_title: "str | None", raw_author: "str | None") -> str:
    """A WishlistItem's identity fingerprint (roster: source + the raw inputs it was created from).
    The resolved snapshot mutates as the item is resolved/edited, but what the operator originally
    entered does not — so a stale `Ref` to a recycled wishlist id is caught at re-check (the
    [[sqlite-id-reuse-hazard]] guard) without a benign re-resolve counting as drift."""
    title = " ".join((raw_title or "").split()).casefold()
    author = " ".join((raw_author or "").split()).casefold()
    return f"{(source or '').strip()}|{(raw_isbn or '').strip()}|{title}|{author}"


@dataclass(frozen=True)
class WishlistItem:
    """A book wanted but not yet owned. Lives OUTSIDE the edition graph (so catalogue reads keep
    meaning "books I own") until acquired, when it converts to a real edition+holding. Carries the
    raw input it was created from, a resolved metadata snapshot, and a resolution `status`
    (unresolved|resolved|ambiguous|owned|acquired). See docs/access/entity_api_model.md."""
    id: int
    source: str                                  # 'manual' | 'isbn' | 'cip' | 'scan'
    status: str = "unresolved"
    # raw inputs (kept so a failed/ambiguous resolve can be retried or edited)
    raw_isbn: "str | None" = None
    raw_title: "str | None" = None
    raw_author: "str | None" = None
    raw_cip_text: "str | None" = None
    # resolved snapshot
    title: "str | None" = None
    subtitle: "str | None" = None
    authors: tuple = ()                          # contributor names (resolved_authors JSON in DB)
    publisher: "str | None" = None
    year: "int | None" = None
    isbn: "str | None" = None
    ol_work_key: "str | None" = None
    lccn: "str | None" = None
    cover_url: "str | None" = None
    candidates: tuple = ()                        # ambiguous-candidate dicts for the user to pick
    matched_edition_id: "int | None" = None       # dedupe / fulfilled-by edition
    priority: "int | None" = None
    notes: "str | None" = None
    added_at: "str | None" = None
    updated_at: "str | None" = None
    acquired_at: "str | None" = None
    rev: int = 0                                  # optimistic-concurrency version (bumped on update)

    def ref(self) -> Ref:
        return Ref("wishlist_item", self.id,
                   wishlist_fingerprint(self.source, self.raw_isbn, self.raw_title, self.raw_author),
                   rev=self.rev)

    def to_dict(self) -> dict:
        return {"id": self.id, "source": self.source, "status": self.status,
                "raw_isbn": self.raw_isbn, "raw_title": self.raw_title,
                "raw_author": self.raw_author, "raw_cip_text": self.raw_cip_text,
                "title": self.title, "subtitle": self.subtitle, "authors": list(self.authors),
                "publisher": self.publisher, "year": self.year, "isbn": self.isbn,
                "ol_work_key": self.ol_work_key, "lccn": self.lccn, "cover_url": self.cover_url,
                "candidates": list(self.candidates), "matched_edition_id": self.matched_edition_id,
                "priority": self.priority, "notes": self.notes, "added_at": self.added_at,
                "updated_at": self.updated_at, "acquired_at": self.acquired_at, "rev": self.rev}

    @classmethod
    def from_dict(cls, d: dict) -> "WishlistItem":
        return cls(d["id"], d["source"], d.get("status", "unresolved"),
                   d.get("raw_isbn"), d.get("raw_title"), d.get("raw_author"), d.get("raw_cip_text"),
                   d.get("title"), d.get("subtitle"), tuple(d.get("authors", ())),
                   d.get("publisher"), d.get("year"), d.get("isbn"), d.get("ol_work_key"),
                   d.get("lccn"), d.get("cover_url"), tuple(d.get("candidates", ())),
                   d.get("matched_edition_id"), d.get("priority"), d.get("notes"),
                   d.get("added_at"), d.get("updated_at"), d.get("acquired_at"), d.get("rev", 0))


@dataclass(frozen=True)
class Holding:
    """A copy/file of an edition (the file-bearing leaf; owns provenance)."""
    id: int
    edition_id: int
    file_path: "str | None"
    content_hash: "str | None"
    text_status: "str | None"

    def ref(self) -> Ref:
        # fingerprint = content_hash: a stable content identity, so a stale Ref to a
        # recycled holding id is caught rather than silently rebinding (id-reuse guard).
        return Ref("holding", self.id, self.content_hash)

    def to_dict(self) -> dict:
        return {"id": self.id, "edition_id": self.edition_id, "file_path": self.file_path,
                "content_hash": self.content_hash, "text_status": self.text_status}

    @classmethod
    def from_dict(cls, d: dict) -> "Holding":
        return cls(d["id"], d["edition_id"], d.get("file_path"),
                   d.get("content_hash"), d.get("text_status"))
