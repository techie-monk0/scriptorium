"""Edition read surface — queries only, storage-agnostic.

Declares a READ `Action`, authorizes it, then delegates to the `EditionStore` (RO connection). No
SQL here — the store is the implementation; tombstoned editions read as absent. See entity_api_model.md §8.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

from .. import _crud


class EditionReader:
    RESOURCE = "edition"

    def __init__(self, access, store):
        self._a = access
        self._s = store

    def _read(self, verb: str) -> None:
        self._a.authorize(Action(self.RESOURCE, verb, AccessMode.READ))

    def get(self, edition_id: int):
        """One **live** edition by id, or None (a soft-deleted tombstone reads as absent)."""
        self._read("get")
        return self._s.get(edition_id)

    def by_work(self, work_id: int):
        """Every **live** edition that contains `work_id` (via edition_work), in id order."""
        self._read("by_work")
        return self._s.list_by_work(work_id)

    def list(self, query=None):
        """One page of **live** editions (id-ordered), filtered by `query.contains` (title substring)
        and paginated by `query.limit`/`offset`. Defaults to the first 50."""
        return _crud.list_page(self._a, self.RESOURCE, self._s, query)

    def count(self, query=None) -> int:
        """Total **live** editions matching `query.contains` — the pagination total for `list`."""
        return _crud.count(self._a, self.RESOURCE, self._s, query)

    def all(self):
        """Every **live** edition as a DTO, id-ordered — a whole-catalogue pass (covers, export)."""
        self._read("all")
        return self._s.all()

    def all_ids(self):
        """Every **live** edition id (a set) — the cheap whole-catalogue id pass (title-derivation walk)."""
        self._read("all_ids")
        return self._s.all_ids()

    def first_isbn(self, edition_id: int):
        """The edition's ISBN (own column, else first `edition_isbn` alias), or None."""
        self._read("first_isbn")
        return self._s.first_isbn(edition_id)

    # ── intake / cross-format matching (LIVE editions) ───────────────────────────
    def by_holding_isbn(self, isbn: str):
        """(id, title) of LIVE editions a holding carries `isbn` for (per-manifestation ISBN)."""
        self._read("by_holding_isbn")
        return self._s.by_holding_isbn(isbn)

    def by_edition_isbn(self, isbn: str):
        """(id, title) of LIVE editions carrying `isbn` as an `edition_isbn` variant-printing link."""
        self._read("by_edition_isbn")
        return self._s.by_edition_isbn(isbn)

    def by_ol_work_key(self, key: str):
        """(id, title) of LIVE editions sharing OpenLibrary work `key` (the cross-format cluster)."""
        self._read("by_ol_work_key")
        return self._s.by_ol_work_key(key)

    def titled(self):
        """(id, title, publisher) for every LIVE titled edition — the title-containment scan input."""
        self._read("titled")
        return self._s.titled()

    def isbn_duplicate_groups(self):
        """(isbn, [edition_ids]) for every ISBN shared by >1 LIVE edition — the auto-merge worklist."""
        self._read("isbn_duplicate_groups")
        return self._s.isbn_duplicate_groups()

    def needs_work_tiers(self, skip_token: str):
        """Per-edition holding-tier counts (phys/clean/dirty/hold/skip) — the needs-work dashboard."""
        self._read("needs_work_tiers")
        return self._s.needs_work_tiers(skip_token)

    # ── full-text + facet search ─────────────────────────────────────────────────
    def fts_search(self, match_query: str, limit: int = 400):
        """(edition_id, page, snippet, bm25) for an FTS5 MATCH, bm25-ordered."""
        self._read("fts_search")
        return self._s.fts_search(match_query, limit)

    def title_fields_all(self):
        """(id, title, subtitle, sanskrit_title, tibetan_title) for every edition — title facet scan."""
        self._read("title_fields_all")
        return self._s.title_fields_all()

    def ids_by_work_alias_key(self, needle: str):
        """Edition ids linked to a work whose any alias normalized_key contains `needle`."""
        self._read("ids_by_work_alias_key")
        return self._s.ids_by_work_alias_key(needle)

    def ids_by_author_person(self, person_id: int):
        """Edition ids whose contained works have `person_id` as a role='author' contributor."""
        self._read("ids_by_author_person")
        return self._s.ids_by_author_person(person_id)

    def ids_by_person(self, person_id: int):
        """Edition ids where `person_id` is ANY contributor (work author / translator)."""
        self._read("ids_by_person")
        return self._s.ids_by_person(person_id)

    def edition_byline(self, edition_id: int):
        """(name, is_translator) for an edition's OWN by-line, in display order."""
        self._read("edition_byline")
        return self._s.edition_byline(edition_id)

    def ol_work_key_state(self, edition_id: int):
        """(isbn, ol_work_key) for the edition, or None — the ensure-key precondition."""
        self._read("ol_work_key_state")
        return self._s.ol_work_key_state(edition_id)

    def missing_work_key(self, limit: "int | None" = None):
        """(id, isbn) of LIVE editions with an ISBN but no ol_work_key yet — the backfill worklist."""
        self._read("missing_work_key")
        return self._s.missing_work_key(limit)

    def subject_names_kinds(self, edition_id: int):
        """(name, kind) of every subject on an edition + its contained works, name-sorted."""
        self._read("subject_names_kinds")
        return self._s.subject_names_kinds(edition_id)

    def volume_set_id(self, edition_id: int):
        """The edition's volume_set_id, or None."""
        self._read("volume_set_id")
        return self._s.volume_set_id(edition_id)

    def holding_paths_for_subject(self, subject_id: int, exclude_eid):
        """Holding file_paths whose edition carries `subject_id` (canonical join), excl. `exclude_eid`."""
        self._read("holding_paths_for_subject")
        return self._s.holding_paths_for_subject(subject_id, exclude_eid)

    def volume_set_holding_paths(self, set_id: int, exclude_eid: int):
        """Holding file_paths of the OTHER editions in a volume set."""
        self._read("volume_set_holding_paths")
        return self._s.volume_set_holding_paths(set_id, exclude_eid)

    def all_isbns(self, edition_id: int):
        """Every ISBN reachable from an edition (own + edition_isbn + holdings), deduped."""
        self._read("all_isbns")
        return self._s.all_isbns(edition_id)

    def topic_subject_names(self, edition_id: int):
        """TOPICAL subject names of the edition + its contained works (series excluded), sorted."""
        self._read("topic_subject_names")
        return self._s.topic_subject_names(edition_id)

    def contained_work_author_names(self, edition_id: int):
        """Distinct author names of the edition's contained works (export search text)."""
        self._read("contained_work_author_names")
        return self._s.contained_work_author_names(edition_id)

    def contributor_persons(self, edition_id: int):
        """(authors, translators) as [(person_id, name)] for an edition — the recorded-contributor
        read WITH ids."""
        self._read("contributor_persons")
        return self._s.contributor_persons(edition_id)

    def detection(self, edition_id: int):
        """(kind, payload_json) of an edition's cached work-detection, or None."""
        self._read("detection")
        return self._s.detection(edition_id)

    def contributor_names(self, edition_id: int):
        """(authors, translators) display-name lists for an edition (raw, book-level first) — the
        shared by-line read; the caller fold-key-dedups."""
        self._read("contributor_names")
        return self._s.contributor_names(edition_id)

    def cached_extract_text(self, edition_id: int):
        """The newest cached front-matter/extract text for an edition (no re-OCR), or None — the
        title/work re-derivation source."""
        self._read("cached_extract_text")
        return self._s.cached_extract_text(edition_id)

    def first_file_path(self, edition_id: int):
        """The edition's representative holding file_path, or None."""
        self._read("first_file_path")
        return self._s.first_file_path(edition_id)

    def raw_text_for_hash(self, file_hash):
        """The newest cached raw_extract_cache text for a file_hash, or None."""
        self._read("raw_text_for_hash")
        return self._s.raw_text_for_hash(file_hash)

    def review_verdict(self, edition_id: int):
        """(review_status, review_flags, review_note, reviewed_at) for an edition, or None."""
        self._read("review_verdict")
        return self._s.review_verdict(edition_id)

    def text_row_count(self, edition_id: int) -> int:
        """How many edition_text rows an edition holds — a cheap content-volume proxy."""
        self._read("text_row_count")
        return self._s.text_row_count(edition_id)

    def without_holding(self):
        """(id, title) for editions with NO holding (file-orphaned), id-ordered."""
        self._read("without_holding")
        return self._s.without_holding()

    def text_content(self, edition_id: int, limit: int = 40):
        """Up to `limit` edition_text content strings for an edition (the shingle-match sample)."""
        self._read("text_content")
        return self._s.text_content(edition_id, limit)

    # ── library display / FRBR-graph reads ───────────────────────────────────────
    def browser_card(self, edition_id: int):
        """(title, review_status, volume) for an edition, or None — the browser-row source."""
        self._read("browser_card")
        return self._s.browser_card(edition_id)

    def recent_ids(self, limit: int = 500):
        """LIVE edition ids, newest first (id DESC) — the no-query master list."""
        self._read("recent_ids")
        return self._s.recent_ids(limit)

    def recently_opened(self, limit: int = 24):
        """Edition ids that have been GENUINELY opened (a real holding.last_opened),
        by MAX(holding.last_opened) DESC — the home 'recently read' rail. Never-opened
        books are excluded (recently-ADDED books are surfaced separately, from
        date_added, by homeVM)."""
        self._read("recently_opened")
        return self._s.recently_opened(limit)

    def recently_added(self, limit: int = 24):
        """Edition ids by MAX(holding.date_added) DESC — the home rail."""
        self._read("recently_added")
        return self._s.recently_added(limit)

    def contained_work_author_ids(self, edition_id: int, author_roles):
        """Distinct person ids authoring (role in `author_roles`) any contained work."""
        self._read("contained_work_author_ids")
        return self._s.contained_work_author_ids(edition_id, author_roles)

    def realizations(self, work_id: int):
        """(id, title, volume, language, volume_set_id, volume_seq) for editions realizing a work."""
        self._read("realizations")
        return self._s.realizations(work_id)

    def realizations_titled(self, work_id: int):
        """(id, title, volume, isbn) for every edition realizing a work — the dedup member evidence."""
        self._read("realizations_titled")
        return self._s.realizations_titled(work_id)

    def volume_title(self, edition_id: int):
        """(volume, title) for an edition, or None — the volume-number derivation."""
        self._read("volume_title")
        return self._s.volume_title(edition_id)

    def translator_names(self, edition_id: int):
        """An edition's translator primary_names, seq/name-ordered."""
        self._read("translator_names")
        return self._s.translator_names(edition_id)

    def person_book_rows(self, person_id: int):
        """(edition_id, title, role) for every edition naming a person as author/translator."""
        self._read("person_book_rows")
        return self._s.person_book_rows(person_id)

    def other_editions(self, work_id: int):
        """(edition_id, title) for editions realizing a work, title/id-ordered."""
        self._read("other_editions")
        return self._s.other_editions(work_id)

    def commentary_target_work_ids(self, edition_id: int):
        """to_work_ids of an edition's modern-commentary edges."""
        self._read("commentary_target_work_ids")
        return self._s.commentary_target_work_ids(edition_id)

    def edition_work_notes(self, edition_id: int):
        """(work_id, note) per contained work, sequence-ordered."""
        self._read("edition_work_notes")
        return self._s.edition_work_notes(edition_id)

    def detection_payloads(self):
        """The payload_json of every work_detection row — the Books-backlog scan."""
        self._read("detection_payloads")
        return self._s.detection_payloads()

    def detections(self, kind=None):
        """(edition_id, kind, payload_json) of work_detection rows (all, or one kind), id-ordered."""
        self._read("detections")
        return self._s.detections(kind)

    def detect_meta(self):
        """(id, structure, title, volume) for every LIVE edition — the detect-view metadata."""
        self._read("detect_meta")
        return self._s.detect_meta()

    def single_work_ids(self):
        """LIVE edition ids that are NOT multi-work, id-ordered — the single-work detect worklist."""
        self._read("single_work_ids")
        return self._s.single_work_ids()

    def multi_work_ids(self):
        """LIVE edition ids whose structure = 'multi_work', id-ordered — the segment-detect worklist."""
        self._read("multi_work_ids")
        return self._s.multi_work_ids()

    def container_work_ids(self, edition_id: int):
        """Whole-book work(s) that duplicate this multi-work edition — a work whose title equals
        the edition's when the edition isn't a single work. General principle: a multi-text
        edition is represented by its constituent works, not one whole-book work. The add-edition
        flow surfaces a non-empty result as an OVERRIDABLE warning; also reported by
        `db_store.integrity`. Empty = OK."""
        self._read("container_work_ids")
        return self._s.container_work_ids(edition_id)

    def has_authority_work(self, edition_id: int) -> bool:
        """Whether the edition contains a MAIN (non root/commentary) work with a canonical_number."""
        self._read("has_authority_work")
        return self._s.has_authority_work(edition_id)

    def linked_work_of_type(self, edition_id: int, work_type: str):
        """The first (by sequence) contained work of `work_type`, or None."""
        self._read("linked_work_of_type")
        return self._s.linked_work_of_type(edition_id, work_type)

    def detect_card_fields(self, edition_id: int):
        """(id, title, structure, notes, isbn) for the detect edit-card, or None."""
        self._read("detect_card_fields")
        return self._s.detect_card_fields(edition_id)

    def summary_card(self, edition_id: int):
        """(id, title, isbn, notes) for the read-only Browse three-layer summary, or None."""
        self._read("summary_card")
        return self._s.summary_card(edition_id)

    def record_card(self, edition_id: int):
        """(id, title, publisher, year, isbn, language, notes) for the record editor, or None."""
        self._read("record_card")
        return self._s.record_card(edition_id)

    def full_record(self, edition_id: int):
        """(title, subtitle, volume, publisher, year, isbn, language, structure) for export, or None."""
        self._read("full_record")
        return self._s.full_record(edition_id)

    def contained_works(self, edition_id: int):
        """(work_id, sequence, translator_person_id, translator_name, section_locator, title,
        locator_type, note) per contained work, sequence-ordered."""
        self._read("contained_works")
        return self._s.contained_works(edition_id)

    def next_work_sequence(self, edition_id: int) -> int:
        """MAX(sequence)+1 over the edition's contained works (append position)."""
        self._read("next_work_sequence")
        return self._s.next_work_sequence(edition_id)

    def ids_by_isbn_like(self, digits: str):
        """LIVE edition ids whose isbn contains the digit fragment `digits`."""
        self._read("ids_by_isbn_like")
        return self._s.ids_by_isbn_like(digits)

    def titled_by_ids(self, ids, pin_id=None):
        """(id, title, isbn, year) for the given LIVE edition ids, `pin_id` first, capped at 20."""
        self._read("titled_by_ids")
        return self._s.titled_by_ids(ids, pin_id)

    def titled_isbn_first(self, isbn, limit: int = 50):
        """(id, title, isbn) for LIVE editions, `isbn` matches first then id-DESC — staging-resolve."""
        self._read("titled_isbn_first")
        return self._s.titled_isbn_first(isbn, limit)

    def text_passages(self):
        """Every in-book text passage as (id, edition_id, page, content) — the content-index source."""
        self._read("text_passages")
        return self._s.text_passages()

    def edition_ids_with_text(self):
        """Distinct edition ids that carry indexed in-book text."""
        self._read("edition_ids_with_text")
        return self._s.edition_ids_with_text()

    def text_signature(self):
        """(row_count, max_id, total_content_length) over the in-book text — the index ETag input."""
        self._read("text_signature")
        return self._s.text_signature()

    def volumes(self, edition_ids):
        """{edition_id: volume} for the given ids (series volume-ordering)."""
        self._read("volumes")
        return self._s.volumes(edition_ids)

    def structure_of(self, edition_id: int):
        """The edition's `structure` ('single_work' | 'multi_work' | None)."""
        self._read("structure_of")
        return self._s.structure_of(edition_id)

    def list_with_structure(self):
        """(id, title, structure, n_works, first_holding_id) per edition, title-sorted."""
        self._read("list_with_structure")
        return self._s.list_with_structure()

    def has_isbn_alias(self, edition_id: int, isbn: str) -> bool:
        """Whether the edition already carries `isbn` as an edition_isbn alias."""
        self._read("has_isbn_alias")
        return self._s.has_isbn_alias(edition_id, isbn)

    def find_ids(self, *, subject=None, author=None, since_date=None, since_edition=None):
        """The reusable cross-cut filter: LIVE edition ids matching the INTERSECTION of the given
        filters (all optional, but pass at least one). `subject` is prefix-inclusive (the node + every
        subject nested beneath it), an edition covered if tagged directly OR via a contained work;
        `author` matches a person by primary_name/alias substring acting as book author, translator,
        or author of a contained work; `since_date` keeps editions whose earliest holding landed
        on/after an ISO date; `since_edition` keeps id > N. Returns a sorted id list. Tombstones are
        excluded throughout. The store does the SQL; this layer intersects.

        Returns None for `subject`/`author` that match nothing (so a caller can distinguish "no such
        subject" from "subject with no editions"); an empty result set is `[]`."""
        self._read("find")
        ids = None  # the running intersection; None = unconstrained so far
        if subject is not None:
            sids = self._s.subject_descendant_ids(subject)
            if not sids:
                return None
            ids = self._s.ids_for_subjects(sids)
        if author is not None:
            pids = self._s.person_ids_by_name(author)
            if not pids:
                return None
            hits = self._s.ids_with_persons(pids)
            ids = hits if ids is None else (ids & hits)
        if since_date is not None:
            hits = self._s.ids_added_since(since_date)
            ids = hits if ids is None else (ids & hits)
        if ids is None:                       # no subject/author/date filter → start from all live
            ids = self._s.all_ids()
        if since_edition is not None:
            ids = {e for e in ids if e > since_edition}
        return sorted(ids)
