"""Edition persistence — the storage PORT + its SQLite adapter.

The Edition access layer (reads/writes) programs against `EditionStore` and holds no SQL or
filesystem code: policy, the plan→apply orchestration, the cross-entity holding delegation, the
orphan policy and the registry whitelist all live there. `SqliteEditionStore` is the implementation
(SQL over the gateway's RO/RW connections + cover-art file enumeration). See entity_api_model.md §5/§6.
"""
from __future__ import annotations

import abc

from catalogue.contracts import Edition, StaleWrite

from ..registry import edition_art_files

_COLS = "id, title, subtitle, isbn, year, publisher, tradition, rev"


def _edition(row) -> Edition:
    return Edition(id=row[0], title=row[1], subtitle=row[2], isbn=row[3],
                   year=row[4], publisher=row[5], tradition=row[6], rev=row[7])


class EditionStore(abc.ABC):
    """Port: the data operations the Edition access layer needs (no policy/transaction logic)."""

    # ── reads (plan) ────────────────────────────────────────────────────────────
    @abc.abstractmethod
    def get(self, edition_id: int) -> "Edition | None": ...
    @abc.abstractmethod
    def list_by_work(self, work_id: int) -> "list[Edition]": ...
    @abc.abstractmethod
    def list_page(self, contains, limit: int, offset: int) -> "list[Edition]":
        """Live editions matching the optional title substring, id-ordered, one page."""
    @abc.abstractmethod
    def count(self, contains) -> int:
        """Total live editions matching the optional title substring (for pagination)."""
    @abc.abstractmethod
    def holding_ids(self, edition_id: int) -> "list[int]": ...

    # ── query primitives (live editions; the reusable filter set) ───────────────
    @abc.abstractmethod
    def all(self) -> "list[Edition]":
        """Every LIVE edition as a DTO, id-ordered — for whole-catalogue passes (covers, export)."""
    @abc.abstractmethod
    def first_isbn(self, edition_id: int) -> "str | None":
        """The edition's ISBN: its own `isbn` column, else the first `edition_isbn` alias, else None."""
    @abc.abstractmethod
    def subject_names_kinds(self, edition_id: int) -> "list[tuple]":
        """(name, kind) of every subject on an edition + its contained works (the canonical join),
        name-sorted — the filing-context subject set."""
    @abc.abstractmethod
    def volume_set_id(self, edition_id: int) -> "int | None":
        """The edition's volume_set_id, or None."""
    @abc.abstractmethod
    def holding_paths_for_subject(self, subject_id: int, exclude_eid) -> "list[str]":
        """Non-empty holding file_paths whose edition carries `subject_id` (edition_subject ∪
        contained-work work_subject), excluding `exclude_eid` — the filing 'where do siblings live'."""
    @abc.abstractmethod
    def volume_set_holding_paths(self, set_id: int, exclude_eid: int) -> "list[str]":
        """Non-empty holding file_paths of the OTHER editions in a volume set."""
    @abc.abstractmethod
    def all_isbns(self, edition_id: int) -> "list[str]":
        """Every ISBN reachable from an edition (own column + edition_isbn aliases + holdings),
        deduped, order-stable — the replica-export ISBN set."""
    @abc.abstractmethod
    def topic_subject_names(self, edition_id: int) -> "list[str]":
        """TOPICAL subject names of the edition + its contained works (series excluded), sorted."""
    @abc.abstractmethod
    def contained_work_author_names(self, edition_id: int) -> "list[str]":
        """Distinct author primary_names of the edition's contained works (for export search text)."""
    @abc.abstractmethod
    def contributor_persons(self, edition_id: int) -> "tuple":
        """(authors, translators) as [(person_id, name)] for an edition — the recorded-contributor
        read WITH ids (work_author + edition_author for authors; edition_translator + per-work
        override for translators). Distinct from `contributor_names` (names only)."""
    @abc.abstractmethod
    def detection(self, edition_id: int) -> "tuple | None":
        """(kind, payload_json) of an edition's cached work-detection, or None."""
    @abc.abstractmethod
    def store_detection(self, edition_id: int, kind: str, payload_json: str) -> None:
        """Upsert an edition's work-detection cache (one row per edition). Staged."""
    @abc.abstractmethod
    def contributor_names(self, edition_id: int) -> "tuple":
        """(authors, translators) display-name lists for an edition — book-level (edition_author /
        edition_translator) first, then contained-work authors + per-work translator overrides. Raw
        (not deduped); the caller fold-key-dedups. The shared edition-by-line read."""
    @abc.abstractmethod
    def cached_extract_text(self, edition_id: int) -> "str | None":
        """The newest cached front-matter/extract text for an edition (its holding's file_hash →
        raw_extract_cache), or None — the title/work re-derivation source (no re-OCR)."""
    @abc.abstractmethod
    def raw_text_for_hash(self, file_hash) -> "str | None":
        """The newest cached raw_extract_cache text for a file_hash, or None."""
    @abc.abstractmethod
    def first_file_path(self, edition_id: int) -> "str | None":
        """The edition's representative holding file_path (lowest id with a path), or None."""
    @abc.abstractmethod
    def text_row_count(self, edition_id: int) -> int:
        """How many edition_text rows an edition holds — a cheap content-volume proxy (dedup)."""
    @abc.abstractmethod
    def without_holding(self) -> "list[tuple]":
        """(id, title) for editions that have NO holding at all (file-orphaned), id-ordered."""
    @abc.abstractmethod
    def text_content(self, edition_id: int, limit: int) -> "list[str]":
        """Up to `limit` edition_text content strings for an edition (the shingle-match sample)."""
    # ── library display / FRBR-graph reads ───────────────────────────────────────
    @abc.abstractmethod
    def browser_card(self, edition_id: int) -> "tuple | None":
        """(title, review_status, volume) for an edition, or None — the browser-row source."""
    @abc.abstractmethod
    def recent_ids(self, limit: int) -> "list[int]":
        """LIVE edition ids, newest first (id DESC) — the no-query master list."""
    @abc.abstractmethod
    def recently_opened(self, limit: int) -> "list[int]":
        """Genuinely-opened edition ids (a real last_opened), by MAX(holding.last_opened) DESC —
        the home 'recently read' rail. Never-opened books are excluded."""
    @abc.abstractmethod
    def recently_added(self, limit: int) -> "list[int]":
        """Edition ids by MAX(holding.date_added) DESC — the home rail."""
    @abc.abstractmethod
    def contained_work_author_ids(self, edition_id: int, author_roles) -> "list[int]":
        """Distinct person ids authoring (in `author_roles`) any work contained in the edition."""
    @abc.abstractmethod
    def realizations(self, work_id: int) -> "list[tuple]":
        """(id, title, volume, language, volume_set_id, volume_seq) for every edition realizing a
        work, set/seq/id-ordered — the cross-edition navigation groups."""
    @abc.abstractmethod
    def realizations_titled(self, work_id: int) -> "list[tuple]":
        """(id, title, volume, isbn) for every edition realizing a work — the dedup member evidence."""
    @abc.abstractmethod
    def volume_title(self, edition_id: int) -> "tuple | None":
        """(volume, title) for an edition, or None — the volume-number derivation."""
    @abc.abstractmethod
    def set_volume_set(self, edition_id: int, set_id, volume_seq) -> None:
        """Mark an edition's membership in a multi-volume set (volume_set_id + volume_seq). Staged."""
    @abc.abstractmethod
    def translator_names(self, edition_id: int) -> "list[str]":
        """An edition's translator primary_names, seq/name-ordered."""
    @abc.abstractmethod
    def person_book_rows(self, person_id: int) -> "list[tuple]":
        """(edition_id, title, role) for every edition naming `person_id` as edition_author /
        contained-work author / edition_translator / per-work translator — the person-books graph."""
    @abc.abstractmethod
    def other_editions(self, work_id: int) -> "list[tuple]":
        """(edition_id, title) for editions realizing a work, title/id-ordered (the caller drops the
        current one)."""
    @abc.abstractmethod
    def commentary_target_work_ids(self, edition_id: int) -> "set[int]":
        """to_work_ids of an edition's modern-commentary edges (edition_commentary_on)."""
    @abc.abstractmethod
    def edition_work_notes(self, edition_id: int) -> "list[tuple]":
        """(work_id, note) per contained work, sequence-ordered — the 'Works In This Edition' source."""
    @abc.abstractmethod
    def detection_payloads(self) -> "list[str]":
        """The payload_json of every work_detection row — the Books-backlog scan (caller counts the
        unapplied ones)."""
    @abc.abstractmethod
    def detections(self, kind) -> "list[tuple]":
        """(edition_id, kind, payload_json) of work_detection rows — ALL when `kind` is None, else only
        that kind — edition_id-ordered. The Books-review worklist."""
    @abc.abstractmethod
    def detect_meta(self) -> "list[tuple]":
        """(id, structure, title, volume) for every LIVE edition — the detect-view edition metadata."""
    @abc.abstractmethod
    def single_work_ids(self) -> "list[int]":
        """LIVE edition ids that are NOT multi-work (COALESCE(structure,'single_work') != 'multi_work'),
        id-ordered — the single-work detect worklist."""
    @abc.abstractmethod
    def multi_work_ids(self) -> "list[int]":
        """LIVE edition ids whose structure = 'multi_work', id-ordered — the segment-detect worklist."""
    @abc.abstractmethod
    def has_authority_work(self, edition_id: int) -> bool:
        """Whether the edition contains a MAIN (non root/commentary) work carrying a canonical_number."""
    @abc.abstractmethod
    def linked_work_of_type(self, edition_id: int, work_type: str) -> "int | None":
        """The first (by sequence) contained work of `work_type` in the edition, or None."""
    @abc.abstractmethod
    def detect_card_fields(self, edition_id: int) -> "tuple | None":
        """(id, title, structure, notes, isbn, tradition) for the detect edit-card, or None."""
    @abc.abstractmethod
    def summary_card(self, edition_id: int) -> "tuple | None":
        """(id, title, isbn, notes, tradition) for the read-only Browse three-layer summary, or None."""
    @abc.abstractmethod
    def record_card(self, edition_id: int) -> "tuple | None":
        """(id, title, publisher, year, isbn, language, notes, tradition) for the edition record editor, or None."""
    @abc.abstractmethod
    def full_record(self, edition_id: int) -> "tuple | None":
        """(title, subtitle, volume, publisher, year, isbn, language, structure) for the export
        record, or None."""
    @abc.abstractmethod
    def contained_works(self, edition_id: int) -> "list[tuple]":
        """(work_id, sequence, translator_person_id, translator_name, section_locator, title,
        locator_type, note) per contained work, sequence-ordered — the edition record's works list."""
    @abc.abstractmethod
    def next_work_sequence(self, edition_id: int) -> int:
        """MAX(sequence)+1 over the edition's contained works (the append position)."""
    @abc.abstractmethod
    def add_contained(self, edition_id, work_id, sequence, translator, section, locator_type,
                      note) -> None:
        """Insert a full edition_work link (all per-appearance fields). Staged."""
    @abc.abstractmethod
    def remove_contained(self, edition_id, work_id, sequence) -> None:
        """Delete the edition_work link keyed by (edition, work, sequence). Staged."""
    @abc.abstractmethod
    def update_contained(self, edition_id, work_id, old_sequence, new_sequence, translator, section,
                         locator_type, note) -> int:
        """Edit a contained-work link in place (keyed by old sequence); returns rowcount. Staged."""
    @abc.abstractmethod
    def ids_by_isbn_like(self, digits: str) -> "set[int]":
        """LIVE edition ids whose isbn contains the digit fragment `digits` — the ISBN typeahead."""
    @abc.abstractmethod
    def titled_by_ids(self, ids, pin_id) -> "list[tuple]":
        """(id, title, isbn, year-or-'') for the given LIVE edition ids, `pin_id` first then id-order,
        capped at 20 — the merge-target / Browse typeahead rows."""
    @abc.abstractmethod
    def titled_isbn_first(self, isbn, limit: int) -> "list[tuple]":
        """(id, title, isbn) for LIVE editions, the ones matching `isbn` first then id-DESC, capped at
        `limit` — the staging-resolve attach-to-existing suggestion."""
    @abc.abstractmethod
    def text_passages(self) -> "list[tuple]":
        """Every in-book text passage as (id, edition_id, page, content) — the content-index source."""
    @abc.abstractmethod
    def edition_ids_with_text(self) -> "list[int]":
        """Distinct edition ids that carry indexed in-book text."""
    @abc.abstractmethod
    def text_signature(self) -> tuple:
        """(row_count, max_id, total_content_length) over the in-book text — the index ETag input."""
    @abc.abstractmethod
    def volumes(self, edition_ids: "list[int]") -> dict:
        """{edition_id: volume} for the given ids (for series volume-ordering)."""
    @abc.abstractmethod
    def structure_of(self, edition_id: int) -> "str | None":
        """The edition's `structure` ('single_work' | 'multi_work' | None)."""
    @abc.abstractmethod
    def list_with_structure(self) -> "list[tuple]":
        """(id, title, structure, n_contained_works, first_holding_id) per edition, title-sorted —
        the structure-tool worklist."""
    @abc.abstractmethod
    def set_structure(self, edition_id: int, value) -> None:
        """Set/clear the edition's structure (no commit)."""
    @abc.abstractmethod
    def set_columns(self, edition_id: int, values: dict) -> None:
        """Update whitelisted scalar metadata columns (title/subtitle/publisher/year/isbn/volume/
        notes/structure); no commit."""
    @abc.abstractmethod
    def set_review_status(self, edition_id: int, status: str) -> None:
        """Set edition.review_status (staged) — outside the metadata whitelist `set_columns` guards."""
    @abc.abstractmethod
    def add_modern_commentary(self, edition_id: int, work_id: int) -> None:
        """Record (idempotently) that the edition is a modern commentary on `work_id`
        (edition_commentary_on). Staged."""
    @abc.abstractmethod
    def remove_modern_commentary(self, edition_id: int, work_id: int) -> None:
        """Drop one edition→work modern-commentary edge. Staged."""
    @abc.abstractmethod
    def review_verdict(self, edition_id: int) -> "tuple | None":
        """(review_status, review_flags, review_note, reviewed_at) for an edition, or None."""
    @abc.abstractmethod
    def set_review_verdict(self, edition_id: int, status, flags_json, note, *, stamp: bool) -> None:
        """Write the edition's review verdict (status/flags/note); `stamp` sets reviewed_at=now,
        else it is kept. Staged."""
    @abc.abstractmethod
    def has_isbn_alias(self, edition_id: int, isbn: str) -> bool:
        """Whether the edition already carries `isbn` as an `edition_isbn` alias."""
    @abc.abstractmethod
    def add_isbn(self, edition_id: int, isbn: str, note: "str | None") -> None:
        """Record an `edition_isbn` alias (no commit)."""
    @abc.abstractmethod
    def all_ids(self) -> "set[int]":
        """Every LIVE edition id."""
    @abc.abstractmethod
    def subject_descendant_ids(self, name: str) -> "list[int]":
        """Subject ids for `name`, prefix-inclusive (the node + everything nested beneath it)."""
    @abc.abstractmethod
    def ids_for_subjects(self, subject_ids: "list[int]") -> "set[int]":
        """Live edition ids covered by any of `subject_ids` — tagged directly OR via a contained work."""
    @abc.abstractmethod
    def person_ids_by_name(self, query: str) -> "list[int]":
        """Person ids whose primary_name OR any alias contains `query` (case-insensitive)."""
    @abc.abstractmethod
    def ids_with_persons(self, person_ids: "list[int]") -> "set[int]":
        """Live edition ids where any of `person_ids` is a book author, translator, or author of a
        contained work."""
    @abc.abstractmethod
    def ids_added_since(self, since: str) -> "set[int]":
        """Live edition ids whose earliest holding.date_added is on/after `since` (ISO string)."""
    @abc.abstractmethod
    def orphaned_work_ids(self, edition_id: int) -> "list[int]":
        """Live works in this edition that would have no other LIVE edition once it's tombstoned."""
    @abc.abstractmethod
    def art_files(self, edition_id: int) -> "list[str]":
        """Existing cover/spine/pin file paths this edition owns (id-keyed; orphan-audit #3)."""

    # ── intake / cross-format matching (LIVE editions) ───────────────────────────
    @abc.abstractmethod
    def by_holding_isbn(self, isbn: str) -> "list[tuple]":
        """(id, title) of LIVE editions a holding carries `isbn` for (a per-manifestation ISBN)."""
    @abc.abstractmethod
    def by_edition_isbn(self, isbn: str) -> "list[tuple]":
        """(id, title) of LIVE editions carrying `isbn` as an `edition_isbn` variant-printing link."""
    @abc.abstractmethod
    def by_ol_work_key(self, key: str) -> "list[tuple]":
        """(id, title) of LIVE editions sharing OpenLibrary work `key` (cross-format cluster)."""
    @abc.abstractmethod
    def titled(self) -> "list[tuple]":
        """(id, title, publisher) for every LIVE titled edition — the title-containment scan input."""
    @abc.abstractmethod
    def isbn_duplicate_groups(self) -> "list[tuple]":
        """(isbn, [edition_ids]) for every ISBN shared by >1 LIVE edition — the auto-merge worklist."""
    # ── full-text + facet search (read-only) ─────────────────────────────────────
    @abc.abstractmethod
    def fts_search(self, match_query: str, limit: int) -> "list[tuple]":
        """(edition_id, page, snippet, bm25) for an FTS5 MATCH over edition_text_fts, bm25-ordered."""
    @abc.abstractmethod
    def title_fields_all(self) -> "list[tuple]":
        """(id, title, subtitle, sanskrit_title, tibetan_title) for every edition — the Python-fold
        book-title facet scan."""
    @abc.abstractmethod
    def ids_by_work_alias_key(self, needle: str) -> "set[int]":
        """Edition ids linked to a work whose any alias normalized_key contains `needle`."""
    @abc.abstractmethod
    def ids_by_author_person(self, person_id: int) -> "set[int]":
        """Edition ids whose contained works have `person_id` as a role='author' contributor."""
    @abc.abstractmethod
    def ids_by_person(self, person_id: int) -> "set[int]":
        """Edition ids where `person_id` is ANY contributor (work author / edition translator /
        per-work translator override)."""
    @abc.abstractmethod
    def edition_byline(self, edition_id: int) -> "list[tuple]":
        """(name, is_translator) for an edition's OWN by-line (edition_author + edition_translator +
        per-work translator override), in display order."""
    @abc.abstractmethod
    def needs_work_tiers(self, skip_token: str) -> "list[tuple]":
        """Per edition: (id, title, isbn, n_phys, n_clean, n_dirty, n_hold, n_skip) — the
        needs-work dashboard tiering over edition×holding (form / text_status / skip-token counts)."""
    @abc.abstractmethod
    def ol_work_key_state(self, edition_id: int) -> "tuple | None":
        """(isbn, ol_work_key) for the edition, or None — the ensure-key precondition read."""
    @abc.abstractmethod
    def missing_work_key(self, limit: "int | None") -> "list[tuple]":
        """(id, isbn) of LIVE editions with an ISBN but no ol_work_key yet (the backfill worklist)."""
    @abc.abstractmethod
    def set_ol_work_key(self, edition_id: int, key: str, *, only_if_empty: bool) -> None:
        """Store the resolved OL work key (optionally only when still empty). Staged (no commit)."""

    # ── write-side check + staged mutations (no commit) ─────────────────────────
    @abc.abstractmethod
    def current(self, edition_id: int) -> "Edition | None":
        """The live edition as the write transaction sees it, or None — the TOCTOU recheck."""
    @abc.abstractmethod
    def create(self, values: dict) -> int:
        """Insert an edition from a validated payload; return the new id."""
    @abc.abstractmethod
    def update(self, edition_id: int, values: dict) -> None:
        """Apply a validated field payload to a live edition."""
    @abc.abstractmethod
    def purge_holding_cache(self, table: str, file_hash: str) -> None: ...
    @abc.abstractmethod
    def snapshot_holdings(self, edition_id: int) -> "list[dict]":
        """Capture this edition's holding rows (all columns) BEFORE a delete hard-removes them — the
        pre-destructive checkpoint payload that `restore_holdings` re-inserts."""
    @abc.abstractmethod
    def restore_holdings(self, rows: "list[dict]") -> None:
        """Re-insert holding rows from a checkpoint snapshot (idempotent; skips ids that came back)."""
    @abc.abstractmethod
    def delete_holdings(self, edition_id: int) -> None:
        """HARD-delete this edition's holdings (children; ≈1-to-1 with a file)."""
    @abc.abstractmethod
    def tombstone(self, edition_id: int) -> None: ...
    @abc.abstractmethod
    def tombstone_work(self, work_id: int) -> None: ...
    @abc.abstractmethod
    def restore(self, edition_id: int) -> None: ...
    @abc.abstractmethod
    def merge_into(self, loser_id: int, winner_id: int) -> None:
        """Fold edition `loser` into `winner`: re-point holdings + edition-keyed edges onto the
        winner, then HARD-delete the loser row (FK cascades its leftovers). Staged (no commit). The
        loser's cover art + any undo snapshot are the CALLER's responsibility, matching the legacy
        services.entity_undo.merge_editions / match.merge_editions behavior."""


class SqliteEditionStore(EditionStore):
    """SQLite adapter over an `Access`'s RO/RW connections."""

    def __init__(self, access):
        self._a = access

    def get(self, edition_id):
        row = self._a.ro.execute(
            f"SELECT {_COLS} FROM edition WHERE id = ? AND deleted_at IS NULL",
            (edition_id,)).fetchone()
        return _edition(row) if row else None

    def list_by_work(self, work_id):
        return [_edition(r) for r in self._a.ro.execute(
            f"SELECT DISTINCT {_COLS} FROM edition "
            "JOIN edition_work ON edition_work.edition_id = edition.id "
            "WHERE edition_work.work_id = ? AND edition.deleted_at IS NULL "
            "ORDER BY edition.id", (work_id,)).fetchall()]

    def _filter(self, contains):
        clauses, args = ["deleted_at IS NULL"], []
        if contains:
            clauses.append("title LIKE ?")
            args.append(f"%{contains}%")
        return " WHERE " + " AND ".join(clauses), args

    def list_page(self, contains, limit, offset):
        where, args = self._filter(contains)
        return [_edition(r) for r in self._a.ro.execute(
            f"SELECT {_COLS} FROM edition{where} ORDER BY id LIMIT ? OFFSET ?",
            (*args, limit, offset)).fetchall()]

    def count(self, contains):
        where, args = self._filter(contains)
        return self._a.ro.execute(f"SELECT count(*) FROM edition{where}", args).fetchone()[0]

    def holding_ids(self, edition_id):
        return [r[0] for r in self._a.ro.execute(
            "SELECT id FROM holding WHERE edition_id = ? ORDER BY id", (edition_id,)).fetchall()]

    # ── query primitives ────────────────────────────────────────────────────────
    def all(self):
        return [_edition(r) for r in self._a.ro.execute(
            f"SELECT {_COLS} FROM edition WHERE deleted_at IS NULL ORDER BY id").fetchall()]

    def first_isbn(self, edition_id):
        row = self._a.ro.execute(
            "SELECT isbn FROM edition WHERE id = ? AND deleted_at IS NULL", (edition_id,)).fetchone()
        if row and row[0]:
            return row[0]
        alias = self._a.ro.execute(
            "SELECT isbn FROM edition_isbn WHERE edition_id = ? LIMIT 1", (edition_id,)).fetchone()
        return alias[0] if alias else None

    # ── intake / cross-format matching ──────────────────────────────────────────
    def by_holding_isbn(self, isbn):
        return self._a.ro.execute(
            "SELECT DISTINCT e.id, e.title FROM holding h JOIN edition e ON e.id = h.edition_id "
            "WHERE h.isbn = ? AND e.deleted_at IS NULL", (isbn,)).fetchall()

    def by_edition_isbn(self, isbn):
        return self._a.ro.execute(
            "SELECT DISTINCT e.id, e.title FROM edition_isbn x JOIN edition e ON e.id = x.edition_id "
            "WHERE x.isbn = ? AND e.deleted_at IS NULL", (isbn,)).fetchall()

    def by_ol_work_key(self, key):
        return self._a.ro.execute(
            "SELECT id, title FROM edition WHERE ol_work_key = ? AND deleted_at IS NULL",
            (key,)).fetchall()

    def titled(self):
        return self._a.ro.execute(
            "SELECT id, title, publisher FROM edition "
            "WHERE title IS NOT NULL AND deleted_at IS NULL").fetchall()

    def fts_search(self, match_query, limit):
        return self._a.ro.execute(
            "SELECT et.edition_id, et.page, "
            "       snippet(edition_text_fts, 0, '[', ']', '…', 16), bm25(edition_text_fts) "
            "FROM edition_text_fts JOIN edition_text et ON et.id = edition_text_fts.rowid "
            "WHERE edition_text_fts MATCH ? ORDER BY bm25(edition_text_fts) LIMIT ?",
            (match_query, limit)).fetchall()

    def title_fields_all(self):
        return self._a.ro.execute(
            "SELECT id, title, subtitle, sanskrit_title, tibetan_title FROM edition").fetchall()

    def ids_by_work_alias_key(self, needle):
        return {r[0] for r in self._a.ro.execute(
            "SELECT DISTINCT ew.edition_id FROM work_alias wa "
            "JOIN edition_work ew ON ew.work_id = wa.work_id "
            "WHERE wa.normalized_key LIKE '%' || ? || '%'", (needle,)).fetchall()}

    def ids_by_author_person(self, person_id):
        return {r[0] for r in self._a.ro.execute(
            "SELECT DISTINCT ew.edition_id FROM work_author wa "
            "JOIN edition_work ew ON ew.work_id = wa.work_id "
            "WHERE wa.person_id = ? AND wa.role = 'author'", (person_id,)).fetchall()}

    def ids_by_person(self, person_id):
        ro = self._a.ro
        eids = {r[0] for r in ro.execute(
            "SELECT DISTINCT ew.edition_id FROM work_author wa "
            "JOIN edition_work ew ON ew.work_id = wa.work_id WHERE wa.person_id = ?",
            (person_id,)).fetchall()}
        eids |= {r[0] for r in ro.execute(
            "SELECT edition_id FROM edition_translator WHERE person_id = ?", (person_id,)).fetchall()}
        eids |= {r[0] for r in ro.execute(
            "SELECT edition_id FROM edition_work WHERE translator_person_id = ?",
            (person_id,)).fetchall()}
        return eids

    def edition_byline(self, edition_id):
        return self._a.ro.execute(
            "SELECT p.primary_name, src.is_tr FROM ("
            "  SELECT person_id, 0 AS is_tr, 0 AS pri, COALESCE(seq, 0) AS ord "
            "    FROM edition_author WHERE edition_id = ? "
            "  UNION ALL SELECT person_id, 1, 1, 0 FROM edition_translator WHERE edition_id = ? "
            "  UNION ALL SELECT translator_person_id, 1, 2, 0 FROM edition_work "
            "    WHERE edition_id = ? AND translator_person_id IS NOT NULL"
            ") src JOIN person p ON p.id = src.person_id "
            "ORDER BY src.is_tr, src.pri, src.ord, p.primary_name",
            (edition_id, edition_id, edition_id)).fetchall()

    def needs_work_tiers(self, skip_token):
        return self._a.ro.execute(
            "SELECT e.id, e.title, e.isbn, "
            "  COALESCE(SUM(CASE WHEN h.form = 'physical' THEN 1 ELSE 0 END), 0) AS n_phys, "
            "  COALESCE(SUM(CASE WHEN h.form = 'electronic' "
            "    AND h.text_status IN ('native', 'ocr_good') THEN 1 ELSE 0 END), 0) AS n_clean, "
            "  COALESCE(SUM(CASE WHEN h.form = 'electronic' "
            "    AND h.text_status IN ('ocr_poor', 'image_only', 'none') THEN 1 ELSE 0 END), 0) AS n_dirty, "
            "  COUNT(h.id) AS n_hold, "
            "  COALESCE(SUM(CASE WHEN h.file_path LIKE ? THEN 1 ELSE 0 END), 0) AS n_skip "
            "FROM edition e LEFT JOIN holding h ON h.edition_id = e.id "
            "GROUP BY e.id, e.title, e.isbn", (f"%{skip_token}%",)).fetchall()

    def isbn_duplicate_groups(self):
        out = []
        for isbn, ids_csv in self._a.ro.execute(
                "SELECT isbn, GROUP_CONCAT(id) FROM edition "
                "WHERE isbn IS NOT NULL AND TRIM(isbn) != '' AND deleted_at IS NULL "
                "GROUP BY isbn HAVING count(*) > 1").fetchall():
            out.append((isbn, sorted(int(i) for i in ids_csv.split(","))))
        return out

    def ol_work_key_state(self, edition_id):
        return self._a.ro.execute(
            "SELECT isbn, ol_work_key FROM edition WHERE id = ?", (edition_id,)).fetchone()

    def missing_work_key(self, limit):
        sql = ("SELECT id, isbn FROM edition "
               "WHERE isbn IS NOT NULL AND TRIM(isbn) <> '' "
               "AND (ol_work_key IS NULL OR ol_work_key = '') AND deleted_at IS NULL ORDER BY id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._a.ro.execute(sql).fetchall()

    def set_ol_work_key(self, edition_id, key, *, only_if_empty):
        if only_if_empty:
            self._a.rw.execute(
                "UPDATE edition SET ol_work_key = ? WHERE id = ? "
                "AND (ol_work_key IS NULL OR ol_work_key = '')", (key, edition_id))
        else:
            self._a.rw.execute(
                "UPDATE edition SET ol_work_key = ? WHERE id = ?", (key, edition_id))

    def subject_names_kinds(self, edition_id):
        return self._a.ro.execute(
            "SELECT DISTINCT s.name, s.kind FROM subject s WHERE s.id IN ("
            "  SELECT subject_id FROM edition_subject WHERE edition_id = ?"
            "  UNION"
            "  SELECT ws.subject_id FROM edition_work ew"
            "    JOIN work_subject ws ON ws.work_id = ew.work_id WHERE ew.edition_id = ?"
            ") ORDER BY s.name", (edition_id, edition_id)).fetchall()

    def volume_set_id(self, edition_id):
        r = self._a.ro.execute(
            "SELECT volume_set_id FROM edition WHERE id = ?", (edition_id,)).fetchone()
        return r[0] if r else None

    def holding_paths_for_subject(self, subject_id, exclude_eid):
        rows = self._a.ro.execute(
            "SELECT h.file_path FROM holding h "
            "WHERE h.file_path IS NOT NULL AND TRIM(h.file_path) <> '' "
            "AND (? IS NULL OR h.edition_id <> ?) "
            "AND h.edition_id IN ("
            "  SELECT edition_id FROM edition_subject WHERE subject_id = ?"
            "  UNION"
            "  SELECT ew.edition_id FROM edition_work ew"
            "    JOIN work_subject ws ON ws.work_id = ew.work_id WHERE ws.subject_id = ?"
            ")", (exclude_eid, exclude_eid, subject_id, subject_id)).fetchall()
        return [r[0] for r in rows]

    def volume_set_holding_paths(self, set_id, exclude_eid):
        rows = self._a.ro.execute(
            "SELECT h.file_path FROM holding h JOIN edition e ON e.id = h.edition_id "
            "WHERE e.volume_set_id = ? AND e.id <> ? "
            "AND h.file_path IS NOT NULL AND TRIM(h.file_path) <> ''",
            (set_id, exclude_eid)).fetchall()
        return [r[0] for r in rows]

    def all_isbns(self, edition_id):
        vals, seen = [], set()
        for q in ("SELECT isbn FROM edition WHERE id = ?",
                  "SELECT isbn FROM edition_isbn WHERE edition_id = ?",
                  "SELECT isbn FROM holding WHERE edition_id = ?"):
            for (v,) in self._a.ro.execute(q, (edition_id,)).fetchall():
                if v and v not in seen:
                    seen.add(v)
                    vals.append(v)
        return vals

    def topic_subject_names(self, edition_id):
        rows = self._a.ro.execute(
            "SELECT s.name FROM edition_subject es JOIN subject s ON s.id = es.subject_id "
            "  WHERE es.edition_id = ? AND s.kind = 'topic' "
            "UNION "
            "SELECT s.name FROM edition_work ew JOIN work_subject ws ON ws.work_id = ew.work_id "
            "  JOIN subject s ON s.id = ws.subject_id WHERE ew.edition_id = ? AND s.kind = 'topic'",
            (edition_id, edition_id)).fetchall()
        return sorted({r[0] for r in rows if r[0]})

    def contained_work_author_names(self, edition_id):
        rows = self._a.ro.execute(
            "SELECT DISTINCT p.primary_name FROM edition_work ew "
            "JOIN work_author wa ON wa.work_id = ew.work_id "
            "JOIN person p ON p.id = wa.person_id WHERE ew.edition_id = ?", (edition_id,)).fetchall()
        return [r[0] for r in rows if r[0]]

    def contributor_persons(self, edition_id):
        ro = self._a.ro
        authors = ro.execute(
            "SELECT DISTINCT p.id, p.primary_name FROM edition_work ew "
            "JOIN work_author wa ON wa.work_id = ew.work_id "
            "JOIN person p ON p.id = wa.person_id WHERE ew.edition_id = ? "
            "UNION SELECT p.id, p.primary_name FROM edition_author ea "
            "JOIN person p ON p.id = ea.person_id WHERE ea.edition_id = ? ORDER BY 2",
            (edition_id, edition_id)).fetchall()
        translators = ro.execute(
            "SELECT DISTINCT p.id, p.primary_name FROM edition_translator et "
            "JOIN person p ON p.id = et.person_id WHERE et.edition_id = ? "
            "UNION SELECT p.id, p.primary_name FROM edition_work ew "
            "JOIN person p ON p.id = ew.translator_person_id WHERE ew.edition_id = ? ORDER BY 2",
            (edition_id, edition_id)).fetchall()
        return ([(r[0], r[1]) for r in authors], [(r[0], r[1]) for r in translators])

    def detection(self, edition_id):
        return self._a.ro.execute(
            "SELECT kind, payload_json FROM work_detection WHERE edition_id = ?",
            (edition_id,)).fetchone()

    def store_detection(self, edition_id, kind, payload_json):
        self._a.rw.execute(
            "INSERT INTO work_detection (edition_id, kind, payload_json) VALUES (?, ?, ?) "
            "ON CONFLICT(edition_id) DO UPDATE SET kind = excluded.kind, "
            "payload_json = excluded.payload_json, created_at = CURRENT_TIMESTAMP",
            (edition_id, kind, payload_json))

    def contributor_names(self, edition_id):
        ro = self._a.ro

        def names(sql):
            return [r[0] for r in ro.execute(sql, (edition_id,)).fetchall() if r[0]]
        authors = names(
            "SELECT p.primary_name FROM edition_author ea JOIN person p ON p.id = ea.person_id "
            "WHERE ea.edition_id = ? ORDER BY ea.seq, p.primary_name")
        authors += names(
            "SELECT DISTINCT p.primary_name FROM edition_work ew "
            "JOIN work_author wa ON wa.work_id = ew.work_id AND wa.role = 'author' "
            "JOIN person p ON p.id = wa.person_id WHERE ew.edition_id = ? ORDER BY p.primary_name")
        translators = names(
            "SELECT DISTINCT p.primary_name FROM edition_translator et "
            "JOIN person p ON p.id = et.person_id WHERE et.edition_id = ? ORDER BY et.seq, p.primary_name")
        translators += names(
            "SELECT DISTINCT p.primary_name FROM edition_work ew "
            "JOIN person p ON p.id = ew.translator_person_id "
            "WHERE ew.edition_id = ? AND ew.translator_person_id IS NOT NULL ORDER BY p.primary_name")
        return authors, translators

    def cached_extract_text(self, edition_id):
        row = self._a.ro.execute(
            "SELECT file_hash FROM holding WHERE edition_id = ? AND file_hash IS NOT NULL "
            "ORDER BY id LIMIT 1", (edition_id,)).fetchone()
        if not row or not row[0]:
            return None
        t = self._a.ro.execute(
            "SELECT raw_text FROM raw_extract_cache WHERE file_hash = ? "
            "ORDER BY extract_version DESC LIMIT 1", (row[0],)).fetchone()
        return t[0] if t else None

    def first_file_path(self, edition_id):
        r = self._a.ro.execute(
            "SELECT file_path FROM holding WHERE edition_id = ? AND file_path IS NOT NULL "
            "ORDER BY id LIMIT 1", (edition_id,)).fetchone()
        return r[0] if r else None

    def raw_text_for_hash(self, file_hash):
        if not file_hash:
            return None
        r = self._a.ro.execute(
            "SELECT raw_text FROM raw_extract_cache WHERE file_hash = ? "
            "ORDER BY extract_version DESC LIMIT 1", (file_hash,)).fetchone()
        return r[0] if r else None

    def text_row_count(self, edition_id):
        return self._a.ro.execute(
            "SELECT COUNT(*) FROM edition_text WHERE edition_id = ?", (edition_id,)).fetchone()[0]

    def without_holding(self):
        return self._a.ro.execute(
            "SELECT id, title FROM edition e WHERE NOT EXISTS "
            "(SELECT 1 FROM holding h WHERE h.edition_id = e.id) ORDER BY id").fetchall()

    def text_content(self, edition_id, limit):
        return [r[0] for r in self._a.ro.execute(
            "SELECT content FROM edition_text WHERE edition_id = ? LIMIT ?",
            (edition_id, limit)).fetchall()]

    # ── library display / FRBR-graph reads ───────────────────────────────────────
    def browser_card(self, edition_id):
        return self._a.ro.execute(
            "SELECT title, review_status, volume FROM edition WHERE id = ?", (edition_id,)).fetchone()

    def recent_ids(self, limit):
        return [r[0] for r in self._a.ro.execute(
            "SELECT id FROM edition WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()]

    def recently_opened(self, limit):
        # Genuinely-opened editions only (a real last_opened) — the home "recently read"
        # rail. A never-opened book must NOT masquerade as recently opened; this formerly
        # used COALESCE(last_opened, date_added), which ranked never-opened books by their
        # added date and leaked them into "Recent". Recently-ADDED books are surfaced
        # separately by homeVM from date_added.
        return [r[0] for r in self._a.ro.execute(
            "SELECT e.id FROM edition e JOIN holding h ON h.edition_id = e.id "
            "WHERE h.last_opened IS NOT NULL "
            "GROUP BY e.id ORDER BY MAX(h.last_opened) DESC, e.id DESC "
            "LIMIT ?", (int(limit),)).fetchall()]

    def recently_added(self, limit):
        return [r[0] for r in self._a.ro.execute(
            "SELECT e.id FROM edition e JOIN holding h ON h.edition_id = e.id "
            "GROUP BY e.id ORDER BY MAX(h.date_added) DESC, e.id DESC LIMIT ?",
            (int(limit),)).fetchall()]

    def contained_work_author_ids(self, edition_id, author_roles):
        roles = list(author_roles)
        ph = ",".join("?" * len(roles))
        return [r[0] for r in self._a.ro.execute(
            f"SELECT DISTINCT wa.person_id FROM edition_work ew "
            f"JOIN work_author wa ON wa.work_id = ew.work_id AND wa.role IN ({ph}) "
            f"WHERE ew.edition_id = ?", (*roles, edition_id)).fetchall()]

    def realizations(self, work_id):
        return self._a.ro.execute(
            "SELECT e.id, e.title, e.volume, e.language, e.volume_set_id, e.volume_seq "
            "FROM edition_work ew JOIN edition e ON e.id = ew.edition_id "
            "WHERE ew.work_id = ? ORDER BY e.volume_set_id, e.volume_seq, e.id",
            (work_id,)).fetchall()

    def realizations_titled(self, work_id):
        return self._a.ro.execute(
            "SELECT e.id, e.title, e.volume, e.isbn FROM edition_work ew "
            "JOIN edition e ON e.id = ew.edition_id WHERE ew.work_id = ? ORDER BY e.id",
            (work_id,)).fetchall()

    def volume_title(self, edition_id):
        return self._a.ro.execute(
            "SELECT volume, title FROM edition WHERE id = ?", (edition_id,)).fetchone()

    def set_volume_set(self, edition_id, set_id, volume_seq):
        self._a.rw.execute(
            "UPDATE edition SET volume_set_id = ?, volume_seq = ? WHERE id = ?",
            (set_id, volume_seq, edition_id))

    def translator_names(self, edition_id):
        return [r[0] for r in self._a.ro.execute(
            "SELECT p.primary_name FROM edition_translator et JOIN person p ON p.id = et.person_id "
            "WHERE et.edition_id = ? ORDER BY et.seq, p.primary_name", (edition_id,)).fetchall()]

    def person_book_rows(self, person_id):
        return self._a.ro.execute(
            "SELECT e.id, e.title, 'author' AS role FROM edition_author ea "
            "  JOIN edition e ON e.id = ea.edition_id WHERE ea.person_id = ?1 "
            "UNION ALL SELECT DISTINCT e.id, e.title, 'author' FROM work_author wa "
            "  JOIN edition_work ew ON ew.work_id = wa.work_id "
            "  JOIN edition e ON e.id = ew.edition_id WHERE wa.person_id = ?1 "
            "UNION ALL SELECT e.id, e.title, 'translator' FROM edition_translator et "
            "  JOIN edition e ON e.id = et.edition_id WHERE et.person_id = ?1 "
            "UNION ALL SELECT DISTINCT e.id, e.title, 'translator' FROM edition_work ew "
            "  JOIN edition e ON e.id = ew.edition_id WHERE ew.translator_person_id = ?1",
            (person_id,)).fetchall()

    def other_editions(self, work_id):
        return self._a.ro.execute(
            "SELECT DISTINCT ew.edition_id, e.title FROM edition_work ew "
            "JOIN edition e ON e.id = ew.edition_id WHERE ew.work_id = ? ORDER BY e.title, e.id",
            (work_id,)).fetchall()

    def commentary_target_work_ids(self, edition_id):
        return {r[0] for r in self._a.ro.execute(
            "SELECT to_work_id FROM edition_commentary_on WHERE edition_id = ?",
            (edition_id,)).fetchall()}

    def edition_work_notes(self, edition_id):
        return self._a.ro.execute(
            "SELECT ew.work_id, ew.note FROM edition_work ew WHERE ew.edition_id = ? "
            "ORDER BY ew.sequence, ew.work_id", (edition_id,)).fetchall()

    def detection_payloads(self):
        return [r[0] for r in self._a.ro.execute(
            "SELECT payload_json FROM work_detection").fetchall()]

    def detections(self, kind):
        if kind is None:
            return self._a.ro.execute(
                "SELECT edition_id, kind, payload_json FROM work_detection "
                "ORDER BY edition_id").fetchall()
        return self._a.ro.execute(
            "SELECT edition_id, kind, payload_json FROM work_detection WHERE kind = ? "
            "ORDER BY edition_id", (kind,)).fetchall()

    def detect_meta(self):
        return self._a.ro.execute(
            "SELECT id, structure, title, volume FROM edition WHERE deleted_at IS NULL").fetchall()

    def single_work_ids(self):
        return [r[0] for r in self._a.ro.execute(
            "SELECT id FROM edition WHERE deleted_at IS NULL "
            "AND COALESCE(structure, 'single_work') != 'multi_work' ORDER BY id").fetchall()]

    def multi_work_ids(self):
        return [r[0] for r in self._a.ro.execute(
            "SELECT id FROM edition WHERE deleted_at IS NULL AND structure = 'multi_work' "
            "ORDER BY id").fetchall()]

    def container_work_ids(self, edition_id):
        """Live work ids whose English title == THIS edition's title, when the edition is NOT a
        single work (structure='multi_work' OR ≥2 other live contained works) — a whole-book
        work standing for the whole multi-text edition (the cataloguing mistake). Empty = OK."""
        return [r[0] for r in self._a.ro.execute(
            "SELECT DISTINCT w.id FROM work w "
            "JOIN work_alias a ON a.work_id = w.id AND a.scheme = 'english' "
            "JOIN edition e ON e.id = ? AND e.title = a.text AND e.deleted_at IS NULL "
            "WHERE w.deleted_at IS NULL AND ("
            "  e.structure = 'multi_work' OR "
            "  (SELECT COUNT(*) FROM edition_work ew JOIN work w2 ON w2.id = ew.work_id "
            "   WHERE ew.edition_id = e.id AND w2.deleted_at IS NULL AND w2.id <> w.id) >= 2)",
            (edition_id,)).fetchall()]

    def has_authority_work(self, edition_id):
        return self._a.ro.execute(
            "SELECT 1 FROM edition_work ew JOIN work w ON w.id = ew.work_id "
            "WHERE ew.edition_id = ? AND w.canonical_number IS NOT NULL "
            "AND COALESCE(w.work_type, '') NOT IN ('root', 'commentary') LIMIT 1",
            (edition_id,)).fetchone() is not None

    def linked_work_of_type(self, edition_id, work_type):
        r = self._a.ro.execute(
            "SELECT ew.work_id FROM edition_work ew JOIN work w ON w.id = ew.work_id "
            "WHERE ew.edition_id = ? AND w.work_type = ? ORDER BY ew.sequence LIMIT 1",
            (edition_id, work_type)).fetchone()
        return r[0] if r else None

    def detect_card_fields(self, edition_id):
        return self._a.ro.execute(
            "SELECT id, title, structure, notes, isbn, tradition FROM edition WHERE id = ?",
            (edition_id,)).fetchone()

    def summary_card(self, edition_id):
        return self._a.ro.execute(
            "SELECT id, title, isbn, notes, tradition FROM edition WHERE id = ?",
            (edition_id,)).fetchone()

    def record_card(self, edition_id):
        return self._a.ro.execute(
            "SELECT id, title, publisher, year, isbn, language, notes, tradition "
            "FROM edition WHERE id = ?",
            (edition_id,)).fetchone()

    def full_record(self, edition_id):
        return self._a.ro.execute(
            "SELECT title, subtitle, volume, publisher, year, isbn, language, structure "
            "FROM edition WHERE id = ?", (edition_id,)).fetchone()

    def contained_works(self, edition_id):
        return self._a.ro.execute(
            "SELECT ew.work_id, ew.sequence, ew.translator_person_id, "
            "       COALESCE(p.primary_name, ''), ew.section_locator, "
            "       (SELECT text FROM work_alias WHERE work_id = ew.work_id ORDER BY id LIMIT 1), "
            "       ew.locator_type, ew.note "
            "FROM edition_work ew LEFT JOIN person p ON p.id = ew.translator_person_id "
            "WHERE ew.edition_id = ? ORDER BY ew.sequence", (edition_id,)).fetchall()

    def next_work_sequence(self, edition_id):
        return self._a.ro.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM edition_work WHERE edition_id = ?",
            (edition_id,)).fetchone()[0]

    def add_contained(self, edition_id, work_id, sequence, translator, section, locator_type, note):
        self._a.rw.execute(
            "INSERT INTO edition_work (edition_id, work_id, sequence, translator_person_id, "
            "section_locator, locator_type, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (edition_id, work_id, sequence, translator, section, locator_type, note))

    def remove_contained(self, edition_id, work_id, sequence):
        self._a.rw.execute(
            "DELETE FROM edition_work WHERE edition_id = ? AND work_id = ? AND sequence = ?",
            (edition_id, work_id, sequence))

    def update_contained(self, edition_id, work_id, old_sequence, new_sequence, translator, section,
                         locator_type, note):
        return self._a.rw.execute(
            "UPDATE edition_work SET sequence = ?, translator_person_id = ?, section_locator = ?, "
            "locator_type = ?, note = ? WHERE edition_id = ? AND work_id = ? AND sequence = ?",
            (new_sequence, translator, section, locator_type, note, edition_id, work_id,
             old_sequence)).rowcount

    def ids_by_isbn_like(self, digits):
        return {r[0] for r in self._a.ro.execute(
            "SELECT id FROM edition WHERE deleted_at IS NULL AND isbn LIKE ?",
            (f"%{digits}%",)).fetchall()}

    def titled_by_ids(self, ids, pin_id):
        ids = list(ids)
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        return self._a.ro.execute(
            f"SELECT id, title, isbn, COALESCE(year, '') FROM edition "
            f"WHERE deleted_at IS NULL AND id IN ({ph}) ORDER BY (id = ?) DESC, id LIMIT 20",
            (*ids, pin_id if pin_id is not None else -1)).fetchall()

    def titled_isbn_first(self, isbn, limit):
        return self._a.ro.execute(
            "SELECT id, title, isbn FROM edition WHERE deleted_at IS NULL "
            "ORDER BY (isbn = ?) DESC, id DESC LIMIT ?", (isbn, limit)).fetchall()

    def text_passages(self):
        return self._a.ro.execute(
            "SELECT id, edition_id, page, content FROM edition_text").fetchall()

    def edition_ids_with_text(self):
        return [r[0] for r in self._a.ro.execute(
            "SELECT DISTINCT edition_id FROM edition_text").fetchall()]

    def text_signature(self):
        return self._a.ro.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(LENGTH(content)), 0) "
            "FROM edition_text").fetchone()

    def volumes(self, edition_ids):
        if not edition_ids:
            return {}
        ph = ",".join("?" * len(edition_ids))
        return {r[0]: r[1] for r in self._a.ro.execute(
            f"SELECT id, volume FROM edition WHERE id IN ({ph})", list(edition_ids)).fetchall()}

    def structure_of(self, edition_id):
        r = self._a.ro.execute(
            "SELECT structure FROM edition WHERE id = ?", (edition_id,)).fetchone()
        return r[0] if r else None

    def list_with_structure(self):
        return self._a.ro.execute(
            "SELECT e.id, e.title, e.structure, "
            "       (SELECT COUNT(*) FROM edition_work ew WHERE ew.edition_id = e.id), "
            "       (SELECT h.id FROM holding h WHERE h.edition_id = e.id "
            "        AND h.file_path IS NOT NULL ORDER BY h.id LIMIT 1) "
            "FROM edition e ORDER BY e.title COLLATE NOCASE, e.id").fetchall()

    def set_structure(self, edition_id, value):
        self._a.rw.execute(
            "UPDATE edition SET structure = ? WHERE id = ?", (value, edition_id))

    _SCALAR_COLS = ("title", "subtitle", "publisher", "year", "isbn", "volume", "notes", "structure",
                    "language", "tradition")

    def set_columns(self, edition_id, values):
        cols = [c for c in values if c in self._SCALAR_COLS]
        if not cols:
            return
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        self._a.rw.execute(
            f"UPDATE edition SET {set_clause} WHERE id = ?",
            (*[values[c] for c in cols], edition_id))

    def set_review_status(self, edition_id, status):
        self._a.rw.execute(
            "UPDATE edition SET review_status = ? WHERE id = ?", (status, edition_id))

    def add_modern_commentary(self, edition_id, work_id):
        self._a.rw.execute(
            "INSERT OR IGNORE INTO edition_commentary_on (edition_id, to_work_id) VALUES (?, ?)",
            (edition_id, work_id))

    def remove_modern_commentary(self, edition_id, work_id):
        self._a.rw.execute(
            "DELETE FROM edition_commentary_on WHERE edition_id = ? AND to_work_id = ?",
            (edition_id, work_id))

    def review_verdict(self, edition_id):
        return self._a.ro.execute(
            "SELECT review_status, review_flags, review_note, reviewed_at FROM edition WHERE id = ?",
            (edition_id,)).fetchone()

    def set_review_verdict(self, edition_id, status, flags_json, note, *, stamp):
        ts = "CURRENT_TIMESTAMP" if stamp else "reviewed_at"
        self._a.rw.execute(
            f"UPDATE edition SET review_status = ?, review_flags = ?, review_note = ?, "
            f"reviewed_at = {ts} WHERE id = ?", (status, flags_json, note, edition_id))

    def has_isbn_alias(self, edition_id, isbn):
        return self._a.ro.execute(
            "SELECT 1 FROM edition_isbn WHERE edition_id = ? AND isbn = ?",
            (edition_id, isbn)).fetchone() is not None

    def add_isbn(self, edition_id, isbn, note):
        self._a.rw.execute(
            "INSERT INTO edition_isbn (edition_id, isbn, note) VALUES (?, ?, ?)",
            (edition_id, isbn, note))

    def all_ids(self):
        return {r[0] for r in self._a.ro.execute(
            "SELECT id FROM edition WHERE deleted_at IS NULL").fetchall()}

    def subject_descendant_ids(self, name):
        name = (name or "").strip().strip("/")
        if not name:
            return []
        esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return [r[0] for r in self._a.ro.execute(
            "SELECT id FROM subject WHERE name = ? COLLATE NOCASE "
            "OR name LIKE ? ESCAPE '\\' COLLATE NOCASE", (name, esc + "/%")).fetchall()]

    def ids_for_subjects(self, subject_ids):
        if not subject_ids:
            return set()
        ph = ",".join("?" * len(subject_ids))
        return {r[0] for r in self._a.ro.execute(
            f"SELECT DISTINCT eid FROM ("
            f"  SELECT es.edition_id AS eid FROM edition_subject es WHERE es.subject_id IN ({ph})"
            f"  UNION"
            f"  SELECT ew.edition_id FROM edition_work ew"
            f"    JOIN work_subject ws ON ws.work_id = ew.work_id WHERE ws.subject_id IN ({ph})"
            f") e JOIN edition ed ON ed.id = e.eid WHERE ed.deleted_at IS NULL",
            (*subject_ids, *subject_ids)).fetchall()}

    def person_ids_by_name(self, query):
        like = f"%{(query or '').strip()}%"
        return [r[0] for r in self._a.ro.execute(
            "SELECT id FROM person WHERE primary_name LIKE ? COLLATE NOCASE "
            "UNION SELECT person_id FROM person_alias WHERE text LIKE ? COLLATE NOCASE",
            (like, like)).fetchall()]

    def ids_with_persons(self, person_ids):
        if not person_ids:
            return set()
        ph = ",".join("?" * len(person_ids))
        return {r[0] for r in self._a.ro.execute(
            f"SELECT DISTINCT eid FROM ("
            f"  SELECT edition_id AS eid FROM edition_author WHERE person_id IN ({ph})"
            f"  UNION SELECT edition_id FROM edition_translator WHERE person_id IN ({ph})"
            f"  UNION SELECT ew.edition_id FROM edition_work ew"
            f"    JOIN work_author wa ON wa.work_id = ew.work_id WHERE wa.person_id IN ({ph})"
            f") e JOIN edition ed ON ed.id = e.eid WHERE ed.deleted_at IS NULL",
            (*person_ids, *person_ids, *person_ids)).fetchall()}

    def ids_added_since(self, since):
        return {r[0] for r in self._a.ro.execute(
            "SELECT h.edition_id FROM holding h JOIN edition e ON e.id = h.edition_id "
            "WHERE e.deleted_at IS NULL GROUP BY h.edition_id HAVING MIN(h.date_added) >= ?",
            (since,)).fetchall()}

    def orphaned_work_ids(self, edition_id):
        return [w for (w,) in self._a.ro.execute(
            "SELECT DISTINCT ew.work_id FROM edition_work ew JOIN work w ON w.id = ew.work_id "
            "WHERE ew.edition_id = ? AND w.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM edition_work ew2 JOIN edition e2 ON e2.id = ew2.edition_id "
            "                WHERE ew2.work_id = ew.work_id AND ew2.edition_id <> ? "
            "                AND e2.deleted_at IS NULL)",
            (edition_id, edition_id)).fetchall()]

    def art_files(self, edition_id):
        return [p for p in edition_art_files(self._a.cover_cache, self._a.cover_pinned, edition_id)
                if self._a.backing.exists(p)]

    def current(self, edition_id):
        row = self._a.rw.execute(
            f"SELECT {_COLS} FROM edition WHERE id = ? AND deleted_at IS NULL",
            (edition_id,)).fetchone()
        return _edition(row) if row else None

    def create(self, values):
        cols = list(values)
        cur = self._a.rw.execute(
            f"INSERT INTO edition ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(values[c] for c in cols))
        return cur.lastrowid

    def update(self, edition_id, values):
        if not values:
            return
        set_clause = ", ".join(f"{c} = ?" for c in values)
        self._a.rw.execute(
            f"UPDATE edition SET {set_clause}, rev = rev + 1 WHERE id = ? AND deleted_at IS NULL",
            (*values.values(), edition_id))

    def purge_holding_cache(self, table, file_hash):
        self._a.rw.execute(f"DELETE FROM {table} WHERE file_hash = ?", (file_hash,))

    def snapshot_holdings(self, edition_id):
        cur = self._a.rw.execute("SELECT * FROM holding WHERE edition_id = ?", (edition_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def restore_holdings(self, rows):
        for r in rows:
            hid, eid = r.get("id"), r.get("edition_id")
            if hid is not None:
                occ = self._a.rw.execute(
                    "SELECT edition_id FROM holding WHERE id = ?", (hid,)).fetchone()
                # id free → insert; same edition → idempotent re-restore (OR IGNORE no-ops);
                # a DIFFERENT edition recycled the freed id → refuse rather than silently skip.
                if occ is not None and occ[0] != eid:
                    raise StaleWrite(
                        f"cannot restore holding {hid}: its id was recycled to edition {occ[0]}")
            cols = list(r)
            self._a.rw.execute(
                f"INSERT OR IGNORE INTO holding ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})", tuple(r[c] for c in cols))

    def delete_holdings(self, edition_id):
        self._a.rw.execute("DELETE FROM holding WHERE edition_id = ?", (edition_id,))

    def tombstone(self, edition_id):
        self._a.rw.execute(
            "UPDATE edition SET deleted_at = datetime('now') WHERE id = ?", (edition_id,))

    def tombstone_work(self, work_id):
        self._a.rw.execute(
            "UPDATE work SET deleted_at = datetime('now') WHERE id = ?", (work_id,))

    def restore(self, edition_id):
        self._a.rw.execute("UPDATE edition SET deleted_at = NULL WHERE id = ?", (edition_id,))

    # Edition-keyed tables an edition MERGE re-points off the loser onto the winner. Mirrors
    # services.entity_undo._EDITION_TABLES minus `edition` itself and `work_detection` (one detection
    # per edition — the loser's drops with the cascading edition row). UPDATE OR IGNORE skips a row
    # that would collide with one the winner already holds; the loser's leftovers then FK-cascade.
    _MERGE_TABLES = (
        ("holding", "edition_id"), ("edition_work", "edition_id"),
        ("edition_author", "edition_id"), ("edition_translator", "edition_id"),
        ("edition_subject", "edition_id"), ("edition_verify_resolution", "edition_id"),
        ("edition_text", "edition_id"),
    )

    def _table_exists(self, table):
        return self._a.rw.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def merge_into(self, loser_id, winner_id):
        rw = self._a.rw
        for table, col in self._MERGE_TABLES:
            if self._table_exists(table):
                rw.execute(f"UPDATE OR IGNORE {table} SET {col} = ? WHERE {col} = ?",
                           (winner_id, loser_id))
        # A CITED loser must not be hard-deleted (stability S1): tombstone it + forward its token to
        # the winner (superseded_by), so an external tool's citation still resolves. A non-cited
        # loser hard-deletes as before (nobody references its id). See external_deps.supersede.
        flagged = rw.execute(
            "SELECT 1 FROM edition_external_dependency WHERE edition_id = ? LIMIT 1",
            (loser_id,)).fetchone() is not None
        if flagged:
            rw.execute(
                "UPDATE edition SET superseded_by = ?, "
                "deleted_at = COALESCE(deleted_at, datetime('now')) WHERE id = ?",
                (winner_id, loser_id))
        else:
            rw.execute("DELETE FROM edition WHERE id = ?", (loser_id,))   # cascades any leftovers
