"""In-memory store fakes — the test adapter the port-adapter split exists for.

The access layer (reads/writes) programs against a `Store` PORT and holds no SQL, so a store is
injectable: SQLite in production, an HTTP client later, an in-memory fake here. `InMemoryHoldingStore`
implements the `HoldingStore` port over plain dicts, letting a test exercise the Holding access
layer's policy gate + plan→apply + Impact computation with **no database at all** — the concrete
proof of the architecture claim in holdings/store.py. Inject it with::

    acc.holdings = HoldingRepo(acc, InMemoryHoldingStore([{"id": 1, "edition_id": 1, ...}]))

`apply` still calls `Access.commit()`, but with the fake the gateway's RW connection is never opened
(lazy), so the commit is a no-op — the fake is the single source of truth.
"""
from __future__ import annotations

from catalogue.access_api.holdings.store import HoldingStore
from catalogue.contracts import Holding

# Default valid text_status codes (the subset the schema seeds); override per fake if needed.
DEFAULT_TEXT_STATUS_CODES = frozenset(
    {"native", "ocr_good", "ocr_poor", "image_only", "none"})

_FIELDS = ("edition_id", "file_path", "content_hash", "text_status",
           "file_hash", "archival_pdf_path", "shelf_location", "notes",
           "holding_type", "form", "root_id")


class InMemoryHoldingStore(HoldingStore):
    """A `HoldingStore` backed by dicts. Construct with an iterable of holding dicts (each needs at
    least `edition_id`; `id` auto-assigns if omitted). Records cache purges in `.purged` for asserts."""

    def __init__(self, holdings=None, text_status_codes=DEFAULT_TEXT_STATUS_CODES):
        self._h: dict[int, dict] = {}
        self._codes = frozenset(text_status_codes)
        self.purged: list[tuple[str, str]] = []
        self._next = 1
        for h in (holdings or []):
            self.add(**h)

    # ── construction ────────────────────────────────────────────────────────────
    def add(self, id=None, **fields) -> int:
        hid = id if id is not None else self._next
        self._next = max(self._next, hid + 1)
        row = {k: None for k in _FIELDS}
        row.update({k: v for k, v in fields.items() if k in _FIELDS})
        if row["edition_id"] is None:
            raise ValueError("a fake holding needs an edition_id")
        self._h[hid] = row
        return hid

    def _dto(self, hid) -> Holding:
        r = self._h[hid]
        return Holding(id=hid, edition_id=r["edition_id"], file_path=r["file_path"],
                       content_hash=r["content_hash"], text_status=r["text_status"])

    # ── reads ─────────────────────────────────────────────────────────────────
    def get(self, holding_id):
        return self._dto(holding_id) if holding_id in self._h else None

    def list_all(self):
        return [self._dto(i) for i in sorted(self._h)]

    def list_by_edition(self, edition_id):
        return [self._dto(i) for i in sorted(self._h)
                if self._h[i]["edition_id"] == edition_id]

    def format_rows(self, edition_id):
        return [(self._h[i]["holding_type"], self._h[i]["form"],
                 self._h[i]["file_path"], self._h[i]["archival_pdf_path"])
                for i in sorted(self._h) if self._h[i]["edition_id"] == edition_id]

    def by_file_path(self, path):
        for i in sorted(self._h):
            if self._h[i]["file_path"] == path:
                return self._dto(i)
        return None

    def ocr_fields(self, holding_id):
        if holding_id not in self._h:
            return None
        r = self._h[holding_id]
        return (r["file_path"], r["text_status"], r["file_hash"])

    def primary_file(self, edition_id):
        for i in sorted(self._h):
            r = self._h[i]
            if r["edition_id"] == edition_id and (r["file_path"] or "").strip():
                return (i, r["file_path"], r["file_hash"])
        return None

    def process_fields(self, holding_id):
        if holding_id not in self._h:
            return None
        r = self._h[holding_id]
        return (r["edition_id"], r["file_path"], r["file_hash"], r["text_status"])

    def paths_of(self, holding_id):
        if holding_id not in self._h:
            return None
        r = self._h[holding_id]
        return (r["file_path"], r["archival_pdf_path"])

    def with_files(self):
        return [(i, self._h[i]["edition_id"], self._h[i]["file_path"],
                 self._h[i]["file_hash"], self._h[i]["content_hash"])
                for i in sorted(self._h) if (self._h[i]["file_path"] or "").strip()]

    def reconcile_index(self):
        return [(i, self._h[i]["file_path"], self._h[i]["file_hash"],
                 self._h[i]["content_hash"], self._h[i]["edition_id"]) for i in sorted(self._h)]

    def by_file_hash(self, file_hash):
        for i in sorted(self._h):
            if self._h[i]["file_hash"] == file_hash:
                return (i, self._h[i]["file_path"], self._h[i]["edition_id"])
        return None

    def cover_handle(self, edition_id):
        for i in sorted(self._h):
            if self._h[i]["edition_id"] == edition_id:
                return (i, self._h[i]["file_path"], self._h[i].get("archival_pdf_path"))
        return None

    def edition_card_rows(self, edition_id):
        return [(i, self._h[i].get("form"), self._h[i].get("text_status"),
                 self._h[i]["file_path"], self._h[i].get("shelf_location"))
                for i in sorted(self._h) if self._h[i]["edition_id"] == edition_id]

    def formats(self, edition_id):
        seen = []
        for i in sorted(self._h):
            ht = self._h[i].get("holding_type")
            if self._h[i]["edition_id"] == edition_id and ht and ht not in seen:
                seen.append(ht)
        return sorted(seen)

    def fields_card(self, holding_id):
        h = self._h.get(holding_id)
        return (holding_id, h["edition_id"], h.get("form"), h.get("text_status"),
                h.get("holding_type"), h.get("shelf_location"), h.get("ocr_quality_score"),
                h.get("notes"), h["file_path"]) if h else None

    def shares_file(self, path, exclude_holding_id):
        return any(i != exclude_holding_id and path in
                   (self._h[i].get("file_path"), self._h[i].get("archival_pdf_path"))
                   for i in self._h)

    def display_rows(self, edition_id):
        return [(i, self._h[i].get("form"), self._h[i].get("holding_type"),
                 self._h[i]["file_path"], self._h[i].get("archival_pdf_path"))
                for i in sorted(self._h) if self._h[i]["edition_id"] == edition_id]

    def total(self):
        return len(self._h)

    def read_target(self, holding_id):
        h = self._h.get(holding_id)
        return (h["file_path"], h.get("archival_pdf_path"), h["edition_id"], None) if h else None

    def cover_source(self, holding_id, edition_id):
        h = self._h.get(holding_id)
        return (h["file_path"], h.get("archival_pdf_path")) \
            if (h and h["edition_id"] == edition_id) else None

    def mark_opened(self, holding_id):
        if holding_id in self._h:
            self._h[holding_id]["last_opened"] = "now"

    def full_rows(self, edition_id):
        return [(i, self._h[i].get("form"), self._h[i]["file_path"],
                 self._h[i].get("archival_pdf_path"), self._h[i].get("shelf_location"),
                 self._h[i].get("holding_type"), self._h[i].get("text_status"))
                for i in sorted(self._h) if self._h[i]["edition_id"] == edition_id]

    def earliest_added(self, edition_id):
        dates = [self._h[i].get("date_added") for i in self._h
                 if self._h[i]["edition_id"] == edition_id and self._h[i].get("date_added")]
        return min(dates) if dates else None

    def detect_paths(self, edition_id):
        out = []
        for i in sorted(self._h):
            if self._h[i]["edition_id"] != edition_id:
                continue
            for key in ("file_path", "archival_pdf_path"):
                p = self._h[i].get(key)
                if p and p not in out:
                    out.append(p)
        return out

    def text_status_counts(self):
        out: dict = {}
        for i in self._h:
            ts = self._h[i]["text_status"]
            out[ts] = out.get(ts, 0) + 1
        return out

    def by_text_status(self, statuses, include_null):
        statuses = set(statuses)
        out = []
        for i in sorted(self._h):
            ts = self._h[i]["text_status"]
            if ts in statuses or (include_null and ts is None):
                out.append((self._h[i]["file_path"], self._h[i]["file_hash"]))
        return out

    def ocr_review_holding(self, file_hash):
        for i in sorted(self._h):
            if self._h[i]["file_hash"] == file_hash:
                h = self._h[i]
                return (i, h["edition_id"], h["file_path"], h.get("text_status"),
                        h.get("ocr_quality_score"))
        return None

    def set_text_status_by_hash(self, file_hash, status):
        for i in self._h:
            if self._h[i]["file_hash"] == file_hash:
                self._h[i]["text_status"] = status

    def text_status_by_hash(self, file_hash):
        for i in self._h:
            if self._h[i]["file_hash"] == file_hash:
                return (self._h[i]["text_status"],)
        return None

    def set_file_path(self, holding_id, path):
        self._h[holding_id]["file_path"] = path

    def set_hashes(self, holding_id, file_hash, content_hash):
        self._h[holding_id]["file_hash"] = file_hash
        self._h[holding_id]["content_hash"] = content_hash

    def set_file_hash(self, holding_id, file_hash):
        self._h[holding_id]["file_hash"] = file_hash

    def set_path_hashes(self, holding_id, path, file_hash, content_hash):
        self._h[holding_id].update(file_path=path, file_hash=file_hash, content_hash=content_hash)

    def insert_holding(self, **cols):
        return self.add(**cols)

    def location_of(self, holding_id):
        if holding_id not in self._h:
            return None
        r = self._h[holding_id]
        return (r["file_path"], r["file_hash"])

    def openable(self, edition_id):
        return [(i, self._h[i]["file_path"], self._h[i]["archival_pdf_path"], self._h[i]["form"])
                for i in sorted(self._h) if self._h[i]["edition_id"] == edition_id]

    def file_referenced(self, path):
        return any(self._h[i]["file_path"] == path or self._h[i]["archival_pdf_path"] == path
                   for i in self._h)

    def ids_by_text_status(self, statuses):
        s = set(statuses)
        return [i for i in sorted(self._h) if self._h[i]["text_status"] in s]

    def count_by_root(self, root_id):
        return sum(1 for i in self._h if self._h[i].get("root_id") == root_id)

    def file_paths(self, root_id=None):
        return [self._h[i]["file_path"] for i in sorted(self._h)
                if root_id is None or self._h[i].get("root_id") == root_id]

    def with_file_path(self):
        return [(i, self._h[i]["file_path"]) for i in sorted(self._h)
                if (self._h[i]["file_path"] or "").strip()]

    def relocation_rows(self):
        return [(i, self._h[i]["file_path"], self._h[i]["file_hash"], self._h[i].get("root_id"))
                for i in sorted(self._h)]

    def append_note(self, holding_id, note):
        r = self._h[holding_id]
        r["notes"] = ((r["notes"] + "\n") if r["notes"] else "") + note

    def set_root(self, holding_id, root_id):
        self._h[holding_id]["root_id"] = root_id

    def set_location(self, holding_id, file_path, file_hash):
        self._h[holding_id]["file_path"] = file_path
        self._h[holding_id]["file_hash"] = file_hash

    def text_status_codes(self):
        return self._codes

    def delete_fields(self, holding_id):
        if holding_id not in self._h:
            return None
        r = self._h[holding_id]
        return {"file_path": r["file_path"], "archival_pdf_path": r["archival_pdf_path"],
                "file_hash": r["file_hash"], "content_hash": r["content_hash"]}

    def shares_file_hash(self, file_hash, exclude_ids):
        return any(i not in exclude_ids and self._h[i]["file_hash"] == file_hash
                   for i in self._h)

    def shares_file_path(self, path, exclude_ids):
        return any(i not in exclude_ids and path in
                   (self._h[i]["file_path"], self._h[i]["archival_pdf_path"])
                   for i in self._h)

    # ── write-side check + staged mutations (no commit) ─────────────────────────
    def current_fingerprint(self, holding_id):
        if holding_id not in self._h:
            return (False, None)
        return (True, self._h[holding_id]["content_hash"])

    def update(self, holding_id, changes):
        self._h[holding_id].update(changes)

    def purge_cache(self, table, file_hash):
        self.purged.append((table, file_hash))

    def delete(self, holding_id):
        self._h.pop(holding_id, None)
