"""Work read surface — queries only, storage-agnostic.

Declares a READ `Action`, authorizes it, then delegates to the `WorkStore` (RO connection). No SQL
here — the store assembles the `Work` DTO (title = representative alias, plus the author set the
identity fingerprint pins); tombstoned works read as absent. See entity_api_model.md §8.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

from .. import _crud


class WorkReader:
    RESOURCE = "work"

    def __init__(self, access, store):
        self._a = access
        self._s = store

    def _read(self, verb: str) -> None:
        self._a.authorize(Action(self.RESOURCE, verb, AccessMode.READ))

    def get(self, work_id: int):
        """One **live** work by id, or None (a soft-deleted/merged-away tombstone reads as absent)."""
        self._read("get")
        return self._s.get(work_id)

    def by_edition(self, edition_id: int):
        """Every **live** work this edition contains (via edition_work), in id order."""
        self._read("by_edition")
        return self._s.list_by_edition(edition_id)

    def list(self, query=None):
        """One page of **live** works (id-ordered), filtered by `query.contains` (representative-alias
        substring) and paginated by `query.limit`/`offset`. Defaults to the first 50."""
        return _crud.list_page(self._a, self.RESOURCE, self._s, query)

    def count(self, query=None) -> int:
        """Total **live** works matching `query.contains` — the pagination total for `list`."""
        return _crud.count(self._a, self.RESOURCE, self._s, query)

    # ── work-authority reads (identity at creation; LIVE works only) ─────────────
    def find_by_canonical(self, system, number):
        """The LIVE work id carrying this canonical (system, number) pair, or None."""
        self._read("find_by_canonical")
        return self._s.find_by_canonical(system, number)

    def ids_by_alias_key(self, key: str):
        """LIVE work ids carrying any alias whose `normalized_key` == `key` (title-key lookup)."""
        self._read("ids_by_alias_key")
        return self._s.work_ids_by_alias_key(key)

    def author_ids(self, work_id: int):
        """person_ids contributing to this work (every role) — the English-title-guard author set."""
        self._read("author_ids")
        return self._s.author_ids(work_id)

    def has_alias_key(self, work_id: int, key: str) -> bool:
        """Whether the work already carries an alias folding to `key`."""
        self._read("has_alias_key")
        return self._s.has_alias_key(work_id, key)

    def commentary_root_id(self, work_id: int):
        """The root work this commentary points at (relationship 'commentary_on'), or None."""
        self._read("commentary_root_id")
        return self._s.commentary_root_id(work_id)

    def first_alias_text(self, work_id: int, scheme: str):
        """The earliest alias text of `scheme` for this work — the native-title resync source."""
        self._read("first_alias_text")
        return self._s.first_alias_text(work_id, scheme)

    # ── work-review reads (the work-completion review pane / badge) ──────────────
    def representative_title(self, work_id: int):
        """The work's display title (first alias, any scheme), or None."""
        self._read("representative_title")
        return self._s.representative_title(work_id)

    def search_hits(self, contains: str, limit: int = 20):
        """(id, canonical_system, canonical_number, title) for LIVE works matching `contains`."""
        self._read("search_hits")
        return self._s.search_hits(contains, limit)

    def hit_by_id(self, work_id: int):
        """(id, canonical_system, canonical_number, title) for one LIVE work, or None."""
        self._read("hit_by_id")
        return self._s.hit_by_id(work_id)

    def aliases_with_id(self, work_id: int):
        """(id, text, scheme) for every alias of the work, id-ordered — the Work Details source."""
        self._read("aliases_with_id")
        return self._s.aliases_with_id(work_id)

    # ── works-route display / edit reads ─────────────────────────────────────────
    def list_rows(self, contains=None):
        """(id, original_language, label) for works matching `contains` (or all), id-DESC."""
        self._read("list_rows")
        return self._s.list_rows(contains)

    def recent_labels(self, limit: int = 100):
        """(id, label) for the most recent `limit` works (id-DESC) — the edition-card work dropdown."""
        self._read("recent_labels")
        return self._s.recent_labels(limit)

    def all_ids(self):
        """Every work id (incl. tombstoned — the dedup pass walks all), id-ordered."""
        self._read("all_ids")
        return self._s.all_ids()

    def canonical_duplicate_groups(self):
        """(canonical_system, canonical_number, [work_ids]) for each canonical id shared by >1 work."""
        self._read("canonical_duplicate_groups")
        return self._s.canonical_duplicate_groups()

    def card_fields(self, work_id: int):
        """(id, work_type, original_language, era, canonical_system, canonical_number, notes), or None."""
        self._read("card_fields")
        return self._s.card_fields(work_id)

    def aliases_full(self, work_id: int):
        """(id, text, scheme, normalized_key) for every alias, id-ordered."""
        self._read("aliases_full")
        return self._s.aliases_full(work_id)

    def has_alias_scheme_key(self, work_id: int, scheme: str, key: str) -> bool:
        """Whether the work carries an alias of `scheme` folding to `key`."""
        self._read("has_alias_scheme_key")
        return self._s.has_alias_scheme_key(work_id, scheme, key)

    def author_rows_named(self, work_id: int):
        """(person_id, role, primary_name) of the work's authors, role/name-ordered."""
        self._read("author_rows_named")
        return self._s.author_rows_named(work_id)

    def editions_of(self, work_id: int):
        """(edition_id, title, sequence, section_locator) for LIVE editions containing the work."""
        self._read("editions_of")
        return self._s.editions_of(work_id)

    def commentaries_of(self, work_id: int):
        """from_work_ids of works that are a commentary_on this work, id-ordered."""
        self._read("commentaries_of")
        return self._s.commentaries_of(work_id)

    def linked_with_type(self, edition_id: int):
        """(work_id, work_type) of the works an edition contains, sequence-ordered."""
        self._read("linked_with_type")
        return self._s.linked_with_type(edition_id)

    def alias_keys(self, work_id: int):
        """Distinct normalized_keys of the work's aliases — the merge-candidate signal."""
        self._read("alias_keys")
        return self._s.alias_keys(work_id)

    def ids_by_alias_keys(self, keys, exclude_work_id: int):
        """Distinct work ids sharing any normalized_key in `keys`, excluding `exclude_work_id`."""
        self._read("ids_by_alias_keys")
        return self._s.ids_by_alias_keys(keys, exclude_work_id)

    def alias_title(self, work_id: int):
        """The work's English-first display title (english → other → filename), or None."""
        self._read("alias_title")
        return self._s.alias_title(work_id)

    def summary_fields(self, work_id: int):
        """(work_type, original_language, era, canonical_system, canonical_number, sanskrit_title,
        tibetan_title, notes) for a work, or None — the Work Basics/Details source."""
        self._read("summary_fields")
        return self._s.summary_fields(work_id)

    def aliases(self, work_id: int):
        """Every (text, scheme) alias of the work, id-ordered."""
        self._read("aliases")
        return self._s.aliases(work_id)

    def primary_alias(self, work_id: int):
        """(alias_id, text) of the work's primary (first) alias, or None — the rename target."""
        self._read("primary_alias")
        return self._s.primary_alias(work_id)

    def ids_in_edition(self, edition_id: int):
        """work_ids this edition contains, in (sequence, rowid) order."""
        self._read("ids_in_edition")
        return self._s.ids_in_edition(edition_id)

    def edition_work_rows(self, edition_id: int):
        """(work_id, sequence, section_locator) per contained work, in (sequence, work_id) order."""
        self._read("edition_work_rows")
        return self._s.edition_work_rows(edition_id)

    def notes(self, work_id: int):
        """The work's `notes` column."""
        self._read("notes")
        return self._s.notes(work_id)

    def books_of_work(self, work_id: int):
        """(edition_id, title, holding_id, file_path) for every book a work appears in."""
        self._read("books_of_work")
        return self._s.books_of_work(work_id)

    def has_author_link(self, work_id: int, person_id: int) -> bool:
        """Whether a work_author edge links this (work, person) — the merge re-point post-check."""
        self._read("has_author_link")
        return self._s.has_author_link(work_id, person_id)

    def has_edition_link(self, work_id: int) -> bool:
        """Whether the work is still linked to any edition (the orphan-GC guard)."""
        self._read("has_edition_link")
        return self._s.has_edition_link(work_id)

    def edition_link_count(self, work_id: int) -> int:
        """How many edition_work rows reference this work (the degenerate-placeholder gate)."""
        self._read("edition_link_count")
        return self._s.edition_link_count(work_id)

    def subject_ids(self, work_id: int):
        """subject_ids tagged on this work (work_subject)."""
        self._read("subject_ids")
        return self._s.subject_ids(work_id)

    def review_fields(self, work_id: int):
        """The work's review/identity scalar columns as a dict, or None if the work is absent."""
        self._read("review_fields")
        return self._s.review_fields(work_id)

    def has_subject(self, work_id: int) -> bool:
        """Whether the work carries any subject link (a completeness signal)."""
        self._read("has_subject")
        return self._s.has_subject(work_id)

    def has_author(self, work_id: int) -> bool:
        """Whether the work carries any author link (a completeness signal)."""
        self._read("has_author")
        return self._s.has_author(work_id)

    # ── work-authorship walk reads (the "who wrote it" resolver) ─────────────────
    def has_author_role(self, work_id: int) -> bool:
        """Whether the work has a role='author' contributor — the authorship-walk gate (narrower
        than `has_author`, which counts every contributor role)."""
        self._read("has_author_role")
        return self._s.has_author_role(work_id)

    def author_less_ids(self, limit: "int | None" = None):
        """LIVE work ids with no role='author' contributor, id-ordered — the authorship walk worklist."""
        self._read("author_less_ids")
        return self._s.author_less_ids(limit)

    def canonical_unresolved_ids(self, limit=None, ids=None):
        """LIVE work ids with no canonical_number (or exactly `ids`) — the canonical picker worklist."""
        self._read("canonical_unresolved_ids")
        return self._s.canonical_unresolved_ids(limit, ids)

    def canonical_unresolved_count(self) -> int:
        """Count of LIVE works with no canonical_number that carry ≥1 alias (the picker 'N of M')."""
        self._read("canonical_unresolved_count")
        return self._s.canonical_unresolved_count()

    def backing_filename(self, work_id: int) -> str:
        """Basename of a file backing this work (via its edition's holding), or '' — CLI provenance."""
        self._read("backing_filename")
        return self._s.backing_filename(work_id)

    def incomplete_rows(self):
        """(id, review_status) for every work — the review-queue scan input."""
        self._read("incomplete_rows")
        return self._s.incomplete_rows()

    def count_incomplete(self) -> int:
        """How many works need review (the dashboard badge count)."""
        self._read("count_incomplete")
        return self._s.count_incomplete()
