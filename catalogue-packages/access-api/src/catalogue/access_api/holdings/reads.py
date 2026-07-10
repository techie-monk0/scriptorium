"""Holding read surface — queries only, storage-agnostic.

Declares a READ `Action`, authorizes it, then delegates to the `HoldingStore` (which reads over the
RO connection, so a read path physically cannot write). No SQL here — the store is the implementation.
See docs/access/entity_api_model.md §8.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action


class HoldingReader:
    RESOURCE = "holding"

    def __init__(self, access, store):
        self._a = access
        self._s = store

    def _read(self, verb: str) -> None:
        self._a.authorize(Action(self.RESOURCE, verb, AccessMode.READ))

    def get(self, holding_id: int):
        """One holding by id, or None."""
        self._read("get")
        return self._s.get(holding_id)

    def all(self):
        """Every holding, in id order — the bulk/maintenance read (the exclusion sweep walks
        every file_path)."""
        self._read("all")
        return self._s.list_all()

    def by_edition(self, edition_id: int):
        """All holdings of an edition, in id order."""
        self._read("by_edition")
        return self._s.list_by_edition(edition_id)

    def format_rows(self, edition_id: int):
        """(holding_type, form, file_path, archival_pdf_path) per holding of an edition, id-ordered —
        the raw facets the 'which formats do I already hold' derivation needs (not on the DTO)."""
        self._read("format_rows")
        return self._s.format_rows(edition_id)

    def by_file_path(self, path: str):
        """The holding whose file_path == `path`, or None (sidecar / relink lookup)."""
        self._read("by_file_path")
        return self._s.by_file_path(path)

    def ocr_fields(self, holding_id: int):
        """(file_path, text_status, file_hash) for a holding, or None — the digitize precondition."""
        self._read("ocr_fields")
        return self._s.ocr_fields(holding_id)

    def primary_file(self, edition_id: int):
        """(holding_id, file_path, file_hash) of an edition's first file-bearing holding, or None."""
        self._read("primary_file")
        return self._s.primary_file(edition_id)

    def process_fields(self, holding_id: int):
        """(edition_id, file_path, file_hash, text_status) for a holding, or None — process precond."""
        self._read("process_fields")
        return self._s.process_fields(holding_id)

    def paths_of(self, holding_id: int):
        """(file_path, archival_pdf_path) for a holding, or None — the send/relink path lookup."""
        self._read("paths_of")
        return self._s.paths_of(holding_id)

    def ids_by_text_status(self, statuses):
        """Holding ids whose text_status is in `statuses`, id-ordered — the re-OCR worklist."""
        self._read("ids_by_text_status")
        return self._s.ids_by_text_status(statuses)

    def total(self) -> int:
        """How many holding rows exist (the settings dashboard stat)."""
        self._read("total")
        return self._s.total()

    def count_by_root(self, root_id: int) -> int:
        """How many holdings are attributed to a library root."""
        self._read("count_by_root")
        return self._s.count_by_root(root_id)

    def file_paths(self, root_id=None):
        """Every holding file_path (optionally scoped to a root) — the repoint-preview scan."""
        self._read("file_paths")
        return self._s.file_paths(root_id)

    def with_file_path(self):
        """(id, file_path) for every holding with a non-empty path — the root-backfill scan."""
        self._read("with_file_path")
        return self._s.with_file_path()

    def relocation_rows(self):
        """(id, file_path, file_hash, root_id) for every holding — the prefix-repoint scan."""
        self._read("relocation_rows")
        return self._s.relocation_rows()

    def with_files(self):
        """(id, edition_id, file_path, file_hash, content_hash) for every holding with a non-empty
        file_path — the broken-link / relink scan."""
        self._read("with_files")
        return self._s.with_files()

    def reconcile_index(self):
        """(id, file_path, file_hash, content_hash, edition_id) for every holding — reconcile dedup."""
        self._read("reconcile_index")
        return self._s.reconcile_index()

    def by_file_hash(self, file_hash: str):
        """(id, file_path, edition_id) for the holding carrying `file_hash`, or None — sweep upsert key."""
        self._read("by_file_hash")
        return self._s.by_file_hash(file_hash)

    def cover_handle(self, edition_id: int):
        """(id, file_path, archival_pdf_path) for an edition's first holding, or None — the Library
        open-in-viewer / cover handle."""
        self._read("cover_handle")
        return self._s.cover_handle(edition_id)

    def edition_card_rows(self, edition_id: int):
        """(id, form, text_status, file_path, shelf_location) per holding of an edition."""
        self._read("edition_card_rows")
        return self._s.edition_card_rows(edition_id)

    def formats(self, edition_id: int):
        """The distinct non-null holding_type values across an edition's holdings."""
        self._read("formats")
        return self._s.formats(edition_id)

    def fields_card(self, holding_id: int):
        """(id, edition_id, form, text_status, holding_type, shelf_location, ocr_quality_score, notes,
        file_path) for the per-holding fields editor, or None."""
        self._read("fields_card")
        return self._s.fields_card(holding_id)

    def shares_file(self, path: str, exclude_holding_id: int) -> bool:
        """Whether any OTHER holding references `path` — the delete-to-trash safety check."""
        self._read("shares_file")
        return self._s.shares_file(path, exclude_holding_id)

    def display_rows(self, edition_id: int):
        """(id, form, holding_type, file_path, archival_pdf_path) for every holding of an edition."""
        self._read("display_rows")
        return self._s.display_rows(edition_id)

    def full_rows(self, edition_id: int):
        """(id, form, file_path, archival_pdf_path, shelf_location, holding_type, text_status) per
        holding of an edition — the export record's holdings."""
        self._read("full_rows")
        return self._s.full_rows(edition_id)

    def earliest_added(self, edition_id: int):
        """The earliest holding.date_added across an edition's holdings, or None."""
        self._read("earliest_added")
        return self._s.earliest_added(edition_id)

    def detect_paths(self, edition_id: int):
        """Every distinct file_path + archival_pdf_path of an edition's holdings (non-null)."""
        self._read("detect_paths")
        return self._s.detect_paths(edition_id)

    def read_target(self, holding_id: int):
        """(file_path, archival_pdf_path, edition_id, edition_title) for the in-app reader, or None."""
        self._read("read_target")
        return self._s.read_target(holding_id)

    def cover_source(self, holding_id: int, edition_id: int):
        """(file_path, archival_pdf_path) for a holding scoped to its edition, or None."""
        self._read("cover_source")
        return self._s.cover_source(holding_id, edition_id)

    def text_status_counts(self):
        """{text_status: count} over every holding (None key for NULL) — the sweep status dashboard."""
        self._read("text_status_counts")
        return self._s.text_status_counts()

    def by_text_status(self, statuses, include_null: bool = True):
        """(file_path, file_hash) for holdings whose text_status is in `statuses` and/or NULL."""
        self._read("by_text_status")
        return self._s.by_text_status(statuses, include_null)

    def ocr_review_holding(self, file_hash: str):
        """(id, edition_id, file_path, text_status, ocr_quality_score) for any holding with `file_hash`,
        or None — the low_quality_ocr review-detail link."""
        self._read("ocr_review_holding")
        return self._s.ocr_review_holding(file_hash)

    def text_status_by_hash(self, file_hash: str):
        """(text_status,) for any holding carrying `file_hash`, or None when none has it (a row tuple,
        so a NULL text_status is distinguishable from no holding)."""
        self._read("text_status_by_hash")
        return self._s.text_status_by_hash(file_hash)

    def location_of(self, holding_id: int):
        """(file_path, file_hash) for a holding, or None — the relink before/after read."""
        self._read("location_of")
        return self._s.location_of(holding_id)

    def openable(self, edition_id: int):
        """(id, file_path, archival_pdf_path, form) per holding of an edition — replica export."""
        self._read("openable")
        return self._s.openable(edition_id)

    def file_referenced(self, path: str) -> bool:
        """Whether ANY holding still points at `path` — the keep-a-shared-file guard."""
        self._read("file_referenced")
        return self._s.file_referenced(path)
