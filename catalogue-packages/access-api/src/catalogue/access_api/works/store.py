"""Work persistence — the storage PORT + its SQLite adapter.

The Work access layer (reads/writes) programs against `WorkStore` and holds no SQL: policy, the
plan→apply orchestration, the merge contract and the over-purge-safe registry use live there.
`SqliteWorkStore` is the implementation. Work is a soft-delete root that owns its aliases and is the
hub of the FRBR graph (editions via `edition_work`, authors, subjects/traditions/collections, and
work↔work relationships); `merge` re-points every one of those edges onto the winner before the
loser tombstones. See entity_api_model.md §3/§5/§6.
"""
from __future__ import annotations

import abc
import json

from catalogue.contracts import (
    IntegrityViolation, Work, query_closure, validate_categorical, writable_field_names,
)
from catalogue.db_store import fold_key

from ..registry import review_items_owned_by

_SCALAR_COLS = ("canonical_system", "canonical_number")
# Scalar work columns the work-authority writes (create_work/identity fill/native-title resync) may
# touch — the whitelist that keeps a column name out of an interpolated UPDATE unless it's one of
# ours. The controlled-vocab columns (work_type/tradition/tenet_system/genre) are contributed by the
# CategoricalField registry so a new such field joins the whitelist automatically.
_WRITABLE_SCALARS = tuple(dict.fromkeys(
    ("work_type", "original_language", "era", "canonical_system",
     "canonical_number", "sanskrit_title", "tibetan_title", "notes")
    + writable_field_names("work")))
# Scalar work columns the winner inherits from the loser ONLY where the winner's own value is empty
# (winner authoritative; loser backfills gaps). Mirrors services.work_merge._CARRY_COLS.
_CARRY_COLS = ("work_type", "original_language", "era", "canonical_system",
               "canonical_number", "sanskrit_title", "tibetan_title", "notes")
# work↔work / work-keyed join tables re-pointed with UPDATE OR IGNORE (skip a row that would collide
# with one the winner already holds) then DELETE the loser's leftovers.
_WORKID_JOIN_TABLES = ("collection_member", "work_subject", "work_tradition")
_WORK_OWNED_TYPES = tuple(t for t, _ in review_items_owned_by("work"))


class WorkStore(abc.ABC):
    """Port: the data operations the Work access layer needs (no policy/transaction logic)."""

    # ── reads (plan) ────────────────────────────────────────────────────────────
    @abc.abstractmethod
    def get(self, work_id: int) -> "Work | None": ...
    @abc.abstractmethod
    def list_by_edition(self, edition_id: int) -> "list[Work]":
        """The LIVE works this edition contains (via edition_work), in id order."""
    @abc.abstractmethod
    def list_page(self, contains, limit: int, offset: int) -> "list[Work]":
        """Live works whose representative alias matches the optional substring, id-ordered, one page."""
    @abc.abstractmethod
    def count(self, contains) -> int:
        """Total live works matching the optional alias substring (for pagination)."""
    @abc.abstractmethod
    def ids_by_categorical(self, name: str, value: str) -> "list[int]":
        """LIVE work ids whose categorical field `name` matches `value` — a taxonomy superset
        expands to its subtree closure (contracts.fields), a flat field is plain equality."""
    @abc.abstractmethod
    def edge_counts(self, work_id: int) -> "dict[str, int]":
        """Row counts per edge that a merge would re-point off this work (for the LinkRepoint preview)."""
    @abc.abstractmethod
    def alias_gain(self, loser_id: int, winner_id: int) -> "list[str]":
        """The loser's alias texts the winner does not already hold (fold-keyed) — gained on merge."""
    @abc.abstractmethod
    def owned_review_item_ids(self, work_id: int) -> "list[int]":
        """Pending review-queue ids of WORK-owned types pointing at this work (over-purge-safe set)."""
    @abc.abstractmethod
    def promotion_arrays_with(self, work_id: int) -> "list[int]":
        """promotion.review_item_id whose work_ids JSON array contains this work."""

    # ── work-authority reads (identity / aliases / commentary; LIVE works only) ──
    @abc.abstractmethod
    def find_by_canonical(self, system, number) -> "int | None":
        """A LIVE work carrying this canonical (system, number), or None."""
    @abc.abstractmethod
    def work_ids_by_alias_key(self, key: str) -> "list[int]":
        """LIVE work ids carrying any alias whose normalized_key == `key`."""
    @abc.abstractmethod
    def author_ids(self, work_id: int) -> "list[int]":
        """person_ids on work_author for this work (EVERY contributor role), id-ordered."""
    @abc.abstractmethod
    def has_alias_key(self, work_id: int, key: str) -> bool:
        """Whether the work already carries an alias folding to `key`."""
    @abc.abstractmethod
    def commentary_root_id(self, work_id: int) -> "int | None":
        """The root work this commentary points at (relationship 'commentary_on'), or None."""
    @abc.abstractmethod
    def first_alias_text(self, work_id: int, scheme: str) -> "str | None":
        """The earliest alias text of `scheme` for this work (native-title resync source), or None."""
    @abc.abstractmethod
    def representative_title(self, work_id: int) -> "str | None":
        """The work's first alias text regardless of scheme (its display title), or None."""
    @abc.abstractmethod
    def search_hits(self, contains: str, limit: int) -> "list[tuple]":
        """(id, canonical_system, canonical_number, title) for LIVE works whose any alias fold
        contains `contains`, id-ordered — the unified-search work hits."""
    @abc.abstractmethod
    def hit_by_id(self, work_id: int) -> "tuple | None":
        """(id, canonical_system, canonical_number, title) for one LIVE work, or None."""
    @abc.abstractmethod
    def aliases_with_id(self, work_id: int) -> "list[tuple]":
        """(id, text, scheme) for every alias of the work, id-ordered — the Work Details source that
        needs the alias id to single out the display alias."""
    # ── works-route display / edit reads ─────────────────────────────────────────
    @abc.abstractmethod
    def list_rows(self, contains) -> "list[tuple]":
        """(id, original_language, label) for works whose any alias fold contains `contains` (or ALL
        works when None), id-DESC — the /works browse list."""
    @abc.abstractmethod
    def recent_labels(self, limit: int) -> "list[tuple]":
        """(id, label) for the most recent `limit` works (id-DESC) — the edition-card work dropdown."""
    @abc.abstractmethod
    def all_ids(self) -> "list[int]":
        """Every work id (incl. tombstoned — the dedup pass walks all), id-ordered."""
    @abc.abstractmethod
    def canonical_duplicate_groups(self) -> "list[tuple]":
        """(canonical_system, canonical_number, [work_ids]) for each canonical id shared by >1 work
        — the Tier-1 dedup groups."""
    @abc.abstractmethod
    def card_fields(self, work_id: int) -> "tuple | None":
        """(id, work_type, original_language, era, canonical_system, canonical_number, notes) for the
        work-detail card, or None."""
    @abc.abstractmethod
    def aliases_full(self, work_id: int) -> "list[tuple]":
        """(id, text, scheme, normalized_key) for every alias, id-ordered — the alias-edit source."""
    @abc.abstractmethod
    def has_alias_scheme_key(self, work_id: int, scheme: str, key: str) -> bool:
        """Whether the work carries an alias of `scheme` folding to `key` (scheme-specific dedup)."""
    @abc.abstractmethod
    def author_rows_named(self, work_id: int) -> "list[tuple]":
        """(person_id, role, primary_name) of the work's authors, role/name-ordered."""
    @abc.abstractmethod
    def editions_of(self, work_id: int) -> "list[tuple]":
        """(edition_id, title, sequence, section_locator) for LIVE editions containing the work."""
    @abc.abstractmethod
    def commentaries_of(self, work_id: int) -> "list[int]":
        """from_work_ids of works that are a commentary_on this work, id-ordered."""
    @abc.abstractmethod
    def linked_with_type(self, edition_id: int) -> "list[tuple]":
        """(work_id, work_type) of the works an edition contains, sequence-ordered."""
    @abc.abstractmethod
    def alias_keys(self, work_id: int) -> "list[str]":
        """Distinct normalized_keys of the work's aliases — the merge-candidate signal."""
    @abc.abstractmethod
    def ids_by_alias_keys(self, keys, exclude_work_id: int) -> "list[int]":
        """Distinct work ids sharing any normalized_key in `keys`, excluding `exclude_work_id`."""
    @abc.abstractmethod
    def alias_title(self, work_id: int) -> "str | None":
        """The work's display title preferring scheme='english', then any non-filename alias, then a
        filename alias (the English-first rule), or None when the work has no alias."""
    @abc.abstractmethod
    def summary_fields(self, work_id: int) -> "tuple | None":
        """(work_type, original_language, era, canonical_system, canonical_number, sanskrit_title,
        tibetan_title, notes, tradition) for a work, or None — the Work Basics/Details source."""
    @abc.abstractmethod
    def primary_alias(self, work_id: int) -> "tuple | None":
        """(alias_id, text) of the work's primary (first) alias, or None — the in-place rename target."""
    @abc.abstractmethod
    def ids_in_edition(self, edition_id: int) -> "list[int]":
        """work_ids this edition contains, in (sequence, rowid) order — the contained-work worklist."""
    @abc.abstractmethod
    def edition_work_rows(self, edition_id: int) -> "list[tuple]":
        """(work_id, sequence, section_locator) per contained work, in (sequence, work_id) order."""
    @abc.abstractmethod
    def notes(self, work_id: int) -> "str | None":
        """The work's `notes` column (used as a root/commentary kind marker by curation)."""
    @abc.abstractmethod
    def link_to_edition(self, edition_id: int, work_id: int, sequence: int, locator) -> None:
        """Upsert the edition_work link for (edition, work): set sequence + section_locator if a row
        exists, else insert one. Staged."""
    @abc.abstractmethod
    def unlink_from_edition(self, edition_id: int, work_id: int) -> None:
        """Remove the edition_work link(s) for (edition, work). Staged."""
    @abc.abstractmethod
    def books_of_work(self, work_id: int) -> "list[tuple]":
        """(edition_id, edition_title, holding_id, file_path) for every book a work appears in — the
        contributor-edit "which physical book is this edge on" display."""
    @abc.abstractmethod
    def has_author_link(self, work_id: int, person_id: int) -> bool:
        """Whether a work_author edge links this (work, person) — the merge re-point post-check."""
    @abc.abstractmethod
    def has_edition_link(self, work_id: int) -> bool:
        """Whether the work is still linked to any edition (the orphan-GC guard)."""
    @abc.abstractmethod
    def edition_link_count(self, work_id: int) -> int:
        """How many edition_work rows reference this work (the degenerate-placeholder gate)."""
    @abc.abstractmethod
    def subject_ids(self, work_id: int) -> "list[int]":
        """subject_ids tagged on this work (work_subject) — moved to the edition on placeholder GC."""
    @abc.abstractmethod
    def hard_delete(self, work_id: int) -> None:
        """HARD-delete a work row (the auto-minted placeholder GC; cascades its aliases/authors).
        NOT a tombstone — this is the throwaway-work removal `works_apply`/`promote.revert` do."""
    @abc.abstractmethod
    def update_alias(self, alias_id: int, text: str) -> None:
        """Rewrite a work_alias's text (+ its fold-keyed normalized_key) in place. Staged."""
    @abc.abstractmethod
    def delete_alias(self, alias_id: int) -> None:
        """Delete one work_alias by id. Staged."""
    @abc.abstractmethod
    def rename_alias_checked(self, alias_id: int, work_id: int, text: str) -> bool:
        """Rewrite an alias's text (+ refolded key) ONLY if it belongs to `work_id`; returns whether a
        row changed (the route 404s on False). Staged."""
    @abc.abstractmethod
    def set_alias_fields(self, alias_id: int, text: str, scheme: str) -> None:
        """Set an alias's text + scheme (+ refolded normalized_key) — the primary-title swap. Staged."""
    @abc.abstractmethod
    def remove_author(self, work_id: int, person_id: int, role: str) -> None:
        """Drop one work_author (work_id, person_id, role) link. Staged."""
    @abc.abstractmethod
    def set_edition_work_note(self, edition_id: int, work_id: int, note) -> None:
        """Set the per-appearance note on an edition_work join row. Staged."""
    @abc.abstractmethod
    def unrelate_commentary(self, work_id: int, *, as_root: bool = False) -> None:
        """Drop the work's commentary_on edge(s): by default the edges where it is the commentary
        (from_work_id); `as_root` drops the edges where it is the root (to_work_id). Staged."""
    @abc.abstractmethod
    def delete_aliases_by_scheme(self, work_id: int, scheme: str) -> None:
        """Delete every alias of a work in one scheme (placeholder-title resync). Staged."""
    @abc.abstractmethod
    def aliases(self, work_id: int) -> "list[tuple]":
        """(text, scheme) for every alias of the work, id-ordered."""
    @abc.abstractmethod
    def review_fields(self, work_id: int) -> "dict | None":
        """The work's review/identity scalar columns as a dict, or None if absent (NO live filter —
        the review pane curates works regardless of tombstone state)."""
    @abc.abstractmethod
    def has_subject(self, work_id: int) -> bool:
        """Whether the work carries any work_subject link."""
    @abc.abstractmethod
    def has_author(self, work_id: int) -> bool:
        """Whether the work carries any work_author link."""
    @abc.abstractmethod
    def has_author_role(self, work_id: int) -> bool:
        """Whether the work has a contributor with role='author' (the authorship-walk gate —
        narrower than `has_author`, which counts EVERY contributor role)."""
    @abc.abstractmethod
    def author_less_ids(self, limit: "int | None") -> "list[int]":
        """LIVE work ids with NO role='author' contributor, id-ordered (the authorship worklist)."""
    @abc.abstractmethod
    def canonical_unresolved_ids(self, limit, ids) -> "list[int]":
        """LIVE work ids with NO canonical_number (or exactly `ids`), id-ordered — the canonical-
        identity picker worklist."""
    @abc.abstractmethod
    def canonical_unresolved_count(self) -> int:
        """Count of LIVE works with no canonical_number that carry ≥1 alias (the picker 'N of M')."""
    @abc.abstractmethod
    def backing_filename(self, work_id: int) -> str:
        """Basename of a file backing this work via its edition's holding, or '' — CLI provenance."""
    @abc.abstractmethod
    def incomplete_rows(self) -> "list[tuple]":
        """(id, review_status) for every work, id-ordered — the review-queue scan input."""
    @abc.abstractmethod
    def count_incomplete(self) -> int:
        """Count of works needing review (not 'ok' AND missing subject/author/canonical-identity)."""
    @abc.abstractmethod
    def set_review_status(self, work_id: int, status) -> None:
        """Set work.review_status (+ stamp/clear reviewed_at). Staged (no commit)."""

    # ── write-side check + staged mutations (no commit) ─────────────────────────
    @abc.abstractmethod
    def current(self, work_id: int) -> "Work | None":
        """The live work as the write transaction sees it, or None — the TOCTOU recheck."""
    @abc.abstractmethod
    def create(self, values: dict) -> int:
        """Insert a work from a validated payload (SCALAR columns only); return the new id. Title
        (a representative alias) and authors are edges, managed separately."""
    @abc.abstractmethod
    def update(self, work_id: int, values: dict) -> None:
        """Apply a validated SCALAR field payload to a live work."""
    @abc.abstractmethod
    def tombstone(self, work_id: int) -> None: ...
    @abc.abstractmethod
    def restore(self, work_id: int) -> None: ...
    @abc.abstractmethod
    def purge_review_item(self, rid: int) -> None: ...
    @abc.abstractmethod
    def scrub_promotion_work(self, review_item_id: int, work_id: int) -> None:
        """Remove `work_id` from this promotion row's work_ids array (a delete; the work is gone)."""
    @abc.abstractmethod
    def merge(self, loser_id: int, winner_id: int) -> None:
        """Re-point every loser edge + non-FK ref onto the winner, backfill the winner's empty scalar
        fields, then tombstone the loser. One staged unit (no commit)."""

    # ── work-authority staged writes (identity / titles / types / commentary) ────
    @abc.abstractmethod
    def insert_scalars(self, values: dict) -> int:
        """Insert a work from SCALAR columns only; return the new id (aliases/edges are separate)."""
    @abc.abstractmethod
    def coalesce_scalars(self, work_id: int, values: dict) -> None:
        """Fill the given whitelisted scalar columns ONLY where empty (`COALESCE(col, new)`)."""
    @abc.abstractmethod
    def set_scalars(self, work_id: int, values: dict) -> None:
        """Set the given whitelisted scalar columns OUTRIGHT (overwrite; the manual-add path)."""
    @abc.abstractmethod
    def set_native_title(self, work_id: int, column: str, value) -> None:
        """Set a native-title column outright (value may be None — resync clears a vanished alias)."""
    @abc.abstractmethod
    def set_work_type(self, work_id: int, work_type) -> None:
        """Register the (open-vocab) type code, then set `work.work_type` outright."""
    @abc.abstractmethod
    def relate_commentary(self, commentary_wid: int, root_wid: int) -> None:
        """Idempotently record commentary_wid --commentary_on--> root_wid + mark both works' types."""


class SqliteWorkStore(WorkStore):
    """SQLite adapter over an `Access`'s RO/RW connections."""

    def __init__(self, access):
        self._a = access

    # ── DTO assembly ──────────────────────────────────────────────────────────
    def _assemble(self, conn, work_id):
        row = conn.execute(
            f"SELECT id, {', '.join(_SCALAR_COLS)}, tradition, genre, rev FROM work "
            "WHERE id = ? AND deleted_at IS NULL",
            (work_id,)).fetchone()
        if not row:
            return None
        title_row = conn.execute(
            "SELECT text FROM work_alias WHERE work_id = ? ORDER BY id LIMIT 1", (work_id,)).fetchone()
        author_ids = tuple(r[0] for r in conn.execute(
            "SELECT person_id FROM work_author WHERE work_id = ? AND role = 'author' "
            "ORDER BY person_id", (work_id,)).fetchall())
        return Work(id=row[0], title=title_row[0] if title_row else None,
                    canonical_system=row[1], canonical_number=row[2], author_ids=author_ids,
                    tradition=row[3], genre=row[4], rev=row[5])

    def get(self, work_id):
        return self._assemble(self._a.ro, work_id)

    def current(self, work_id):
        return self._assemble(self._a.rw, work_id)

    def create(self, values):
        cols = list(values)
        if cols:
            cur = self._a.rw.execute(
                f"INSERT INTO work ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(values[c] for c in cols))
        else:
            cur = self._a.rw.execute("INSERT INTO work DEFAULT VALUES")
        return cur.lastrowid

    def update(self, work_id, values):
        if not values:
            return
        set_clause = ", ".join(f"{c} = ?" for c in values)
        self._a.rw.execute(
            f"UPDATE work SET {set_clause}, rev = rev + 1 WHERE id = ? AND deleted_at IS NULL",
            (*values.values(), work_id))

    def list_by_edition(self, edition_id):
        ids = [r[0] for r in self._a.ro.execute(
            "SELECT ew.work_id FROM edition_work ew JOIN work w ON w.id = ew.work_id "
            "WHERE ew.edition_id = ? AND w.deleted_at IS NULL ORDER BY ew.work_id",
            (edition_id,)).fetchall()]
        return [self._assemble(self._a.ro, wid) for wid in ids]

    def list_page(self, contains, limit, offset):
        # A work has no title column — search the representative alias (work_alias.text).
        if contains:
            ids = [r[0] for r in self._a.ro.execute(
                "SELECT DISTINCT w.id FROM work w JOIN work_alias a ON a.work_id = w.id "
                "WHERE w.deleted_at IS NULL AND a.text LIKE ? ORDER BY w.id LIMIT ? OFFSET ?",
                (f"%{contains}%", limit, offset)).fetchall()]
        else:
            ids = [r[0] for r in self._a.ro.execute(
                "SELECT id FROM work WHERE deleted_at IS NULL ORDER BY id LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()]
        return [self._assemble(self._a.ro, wid) for wid in ids]

    def count(self, contains):
        if contains:
            return self._a.ro.execute(
                "SELECT count(DISTINCT w.id) FROM work w JOIN work_alias a ON a.work_id = w.id "
                "WHERE w.deleted_at IS NULL AND a.text LIKE ?", (f"%{contains}%",)).fetchone()[0]
        return self._a.ro.execute(
            "SELECT count(*) FROM work WHERE deleted_at IS NULL").fetchone()[0]

    # ── merge preview ─────────────────────────────────────────────────────────
    def edge_counts(self, work_id):
        ro = self._a.ro
        one = lambda sql, p: ro.execute(sql, p).fetchone()[0]
        return {
            "edition_work": one("SELECT count(*) FROM edition_work WHERE work_id=?", (work_id,)),
            "work_author": one("SELECT count(*) FROM work_author WHERE work_id=?", (work_id,)),
            "relationship": one("SELECT count(*) FROM relationship WHERE from_work_id=? OR to_work_id=?",
                                (work_id, work_id)),
            "edition_commentary_on": one("SELECT count(*) FROM edition_commentary_on WHERE to_work_id=?",
                                         (work_id,)),
            "collection_member": one("SELECT count(*) FROM collection_member WHERE work_id=?", (work_id,)),
            "work_subject": one("SELECT count(*) FROM work_subject WHERE work_id=?", (work_id,)),
            "work_tradition": one("SELECT count(*) FROM work_tradition WHERE work_id=?", (work_id,)),
            "work_alias": one("SELECT count(*) FROM work_alias WHERE work_id=?", (work_id,)),
        }

    def alias_gain(self, loser_id, winner_id):
        winner_keys = {r[0] for r in self._a.ro.execute(
            "SELECT normalized_key FROM work_alias WHERE work_id=?", (winner_id,)).fetchall()}
        return [t for (t,) in self._a.ro.execute(
            "SELECT text FROM work_alias WHERE work_id=? ORDER BY id", (loser_id,)).fetchall()
            if fold_key(t) not in winner_keys]

    # ── non-FK refs (over-purge-safe: WORK-owned review items only) ──────────────
    def _owned_pending(self, conn, work_id):
        out = []
        for rid, item_type, raw in conn.execute(
                "SELECT id, item_type, payload_json FROM review_queue WHERE status='pending'").fetchall():
            if item_type not in _WORK_OWNED_TYPES:
                continue                                   # secondary work_id ⇒ NOT owned ⇒ leave it
            try:
                p = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                continue
            if p.get("work_id") == work_id:
                out.append((rid, p))
        return out

    def owned_review_item_ids(self, work_id):
        return [rid for rid, _ in self._owned_pending(self._a.ro, work_id)]

    def promotion_arrays_with(self, work_id):
        out = []
        for rid, raw in self._a.ro.execute(
                "SELECT review_item_id, work_ids FROM promotion WHERE work_ids LIKE ?",
                (f"%{work_id}%",)).fetchall():             # coarse prefilter; verify below
            try:
                ids = json.loads(raw) if raw else []
            except (ValueError, TypeError):
                continue
            if work_id in ids:
                out.append(rid)
        return out

    # ── work-authority reads (LIVE works only) ──────────────────────────────────
    def find_by_canonical(self, system, number):
        if not system or not number:
            return None
        r = self._a.ro.execute(
            "SELECT id FROM work WHERE canonical_system = ? AND canonical_number = ? "
            "AND deleted_at IS NULL", (system, number)).fetchone()
        return r[0] if r else None

    def work_ids_by_alias_key(self, key):
        return [r[0] for r in self._a.ro.execute(
            "SELECT DISTINCT a.work_id FROM work_alias a JOIN work w ON w.id = a.work_id "
            "WHERE a.normalized_key = ? AND w.deleted_at IS NULL", (key,)).fetchall()]

    def author_ids(self, work_id):
        return [r[0] for r in self._a.ro.execute(
            "SELECT person_id FROM work_author WHERE work_id = ? ORDER BY person_id",
            (work_id,)).fetchall()]

    def has_alias_key(self, work_id, key):
        return self._a.ro.execute(
            "SELECT 1 FROM work_alias WHERE work_id = ? AND normalized_key = ? LIMIT 1",
            (work_id, key)).fetchone() is not None

    def commentary_root_id(self, work_id):
        r = self._a.ro.execute(
            "SELECT to_work_id FROM relationship WHERE from_work_id = ? AND "
            "relation = 'commentary_on' LIMIT 1", (work_id,)).fetchone()
        return r[0] if r else None

    def first_alias_text(self, work_id, scheme):
        r = self._a.ro.execute(
            "SELECT text FROM work_alias WHERE work_id = ? AND scheme = ? ORDER BY id LIMIT 1",
            (work_id, scheme)).fetchone()
        return r[0] if r else None

    def representative_title(self, work_id):
        r = self._a.ro.execute(
            "SELECT text FROM work_alias WHERE work_id = ? ORDER BY id LIMIT 1", (work_id,)).fetchone()
        return r[0] if r else None

    def search_hits(self, contains, limit):
        return self._a.ro.execute(
            "SELECT DISTINCT w.id, w.canonical_system, w.canonical_number, "
            "  (SELECT text FROM work_alias WHERE work_id = w.id ORDER BY id LIMIT 1) "
            "FROM work w JOIN work_alias a ON a.work_id = w.id "
            "WHERE w.deleted_at IS NULL AND a.normalized_key LIKE '%' || ? || '%' "
            "ORDER BY w.id LIMIT ?", (contains, limit)).fetchall()

    def hit_by_id(self, work_id):
        return self._a.ro.execute(
            "SELECT w.id, w.canonical_system, w.canonical_number, "
            "  (SELECT text FROM work_alias WHERE work_id = w.id ORDER BY id LIMIT 1) "
            "FROM work w WHERE w.id = ? AND w.deleted_at IS NULL", (work_id,)).fetchone()

    def aliases_with_id(self, work_id):
        return self._a.ro.execute(
            "SELECT id, text, scheme FROM work_alias WHERE work_id = ? ORDER BY id",
            (work_id,)).fetchall()

    _LABEL_SUB = "(SELECT text FROM work_alias WHERE work_id = w.id ORDER BY id LIMIT 1)"

    def list_rows(self, contains):
        if contains:
            return self._a.ro.execute(
                f"SELECT DISTINCT w.id, w.original_language, {self._LABEL_SUB} AS label "
                "FROM work w JOIN work_alias a ON a.work_id = w.id "
                "WHERE a.normalized_key LIKE ? ORDER BY w.id DESC",
                (f"%{contains}%",)).fetchall()
        return self._a.ro.execute(
            f"SELECT w.id, w.original_language, {self._LABEL_SUB} AS label "
            "FROM work w ORDER BY w.id DESC").fetchall()

    def recent_labels(self, limit):
        return self._a.ro.execute(
            f"SELECT w.id, {self._LABEL_SUB} AS label FROM work w ORDER BY w.id DESC LIMIT ?",
            (limit,)).fetchall()

    def all_ids(self):
        return [r[0] for r in self._a.ro.execute("SELECT id FROM work ORDER BY id").fetchall()]

    def canonical_duplicate_groups(self):
        rows = self._a.ro.execute(
            "SELECT canonical_system, canonical_number, GROUP_CONCAT(id) FROM work "
            "WHERE canonical_number IS NOT NULL AND TRIM(canonical_number) != '' "
            "GROUP BY canonical_system, canonical_number HAVING COUNT(*) > 1").fetchall()
        return [(sys, num, sorted(int(i) for i in ids_csv.split(",")))
                for sys, num, ids_csv in rows]

    def card_fields(self, work_id):
        return self._a.ro.execute(
            "SELECT id, work_type, original_language, era, canonical_system, canonical_number, "
            "notes, tradition, genre, tenet_system FROM work WHERE id = ?", (work_id,)).fetchone()

    def aliases_full(self, work_id):
        return self._a.ro.execute(
            "SELECT id, text, scheme, normalized_key FROM work_alias WHERE work_id = ? ORDER BY id",
            (work_id,)).fetchall()

    def has_alias_scheme_key(self, work_id, scheme, key):
        return self._a.ro.execute(
            "SELECT 1 FROM work_alias WHERE work_id = ? AND scheme = ? AND normalized_key = ?",
            (work_id, scheme, key)).fetchone() is not None

    def author_rows_named(self, work_id):
        return self._a.ro.execute(
            "SELECT wa.person_id, wa.role, p.primary_name FROM work_author wa "
            "JOIN person p ON p.id = wa.person_id WHERE wa.work_id = ? ORDER BY wa.role, p.primary_name",
            (work_id,)).fetchall()

    def editions_of(self, work_id):
        return self._a.ro.execute(
            "SELECT ew.edition_id, e.title, ew.sequence, ew.section_locator "
            "FROM edition_work ew JOIN edition e ON e.id = ew.edition_id "
            "WHERE ew.work_id = ? AND e.deleted_at IS NULL ORDER BY ew.edition_id",
            (work_id,)).fetchall()

    def commentaries_of(self, work_id):
        return [r[0] for r in self._a.ro.execute(
            "SELECT from_work_id FROM relationship WHERE to_work_id = ? AND relation = 'commentary_on' "
            "ORDER BY from_work_id", (work_id,)).fetchall()]

    def linked_with_type(self, edition_id):
        return self._a.ro.execute(
            "SELECT ew.work_id, w.work_type FROM edition_work ew JOIN work w ON w.id = ew.work_id "
            "WHERE ew.edition_id = ? ORDER BY ew.sequence", (edition_id,)).fetchall()

    def alias_keys(self, work_id):
        return [r[0] for r in self._a.ro.execute(
            "SELECT DISTINCT normalized_key FROM work_alias WHERE work_id = ?", (work_id,)).fetchall()]

    def ids_by_alias_keys(self, keys, exclude_work_id):
        keys = list(keys)
        if not keys:
            return []
        ph = ",".join("?" * len(keys))
        return [r[0] for r in self._a.ro.execute(
            f"SELECT DISTINCT work_id FROM work_alias WHERE normalized_key IN ({ph}) AND work_id != ?",
            (*keys, exclude_work_id)).fetchall()]

    def alias_title(self, work_id):
        r = self._a.ro.execute(
            "SELECT text FROM work_alias WHERE work_id = ? "
            "ORDER BY (scheme = 'english') DESC, (scheme = 'filename') ASC, id LIMIT 1",
            (work_id,)).fetchone()
        return r[0] if r else None

    def summary_fields(self, work_id):
        return self._a.ro.execute(
            "SELECT work_type, original_language, era, canonical_system, canonical_number, "
            "sanskrit_title, tibetan_title, notes, tradition FROM work WHERE id = ?",
            (work_id,)).fetchone()

    def primary_alias(self, work_id):
        r = self._a.ro.execute(
            "SELECT id, text FROM work_alias WHERE work_id = ? ORDER BY id LIMIT 1",
            (work_id,)).fetchone()
        return (r[0], r[1]) if r else None

    def ids_in_edition(self, edition_id):
        return [r[0] for r in self._a.ro.execute(
            "SELECT work_id FROM edition_work WHERE edition_id = ? ORDER BY sequence, rowid",
            (edition_id,)).fetchall()]

    def edition_work_rows(self, edition_id):
        return self._a.ro.execute(
            "SELECT work_id, sequence, section_locator FROM edition_work "
            "WHERE edition_id = ? ORDER BY sequence, work_id", (edition_id,)).fetchall()

    def notes(self, work_id):
        r = self._a.ro.execute("SELECT notes FROM work WHERE id = ?", (work_id,)).fetchone()
        return r[0] if r else None

    def link_to_edition(self, edition_id, work_id, sequence, locator):
        rw = self._a.rw
        if rw.execute("SELECT 1 FROM edition_work WHERE edition_id = ? AND work_id = ?",
                      (edition_id, work_id)).fetchone():
            rw.execute("UPDATE edition_work SET sequence = ?, section_locator = ? "
                       "WHERE edition_id = ? AND work_id = ?",
                       (sequence, locator, edition_id, work_id))
        else:
            rw.execute("INSERT INTO edition_work (edition_id, work_id, sequence, section_locator) "
                       "VALUES (?, ?, ?, ?)", (edition_id, work_id, sequence, locator))

    def unlink_from_edition(self, edition_id, work_id):
        self._a.rw.execute("DELETE FROM edition_work WHERE edition_id = ? AND work_id = ?",
                           (edition_id, work_id))

    def books_of_work(self, work_id):
        return self._a.ro.execute(
            "SELECT DISTINCT e.id, e.title, h.id, h.file_path "
            "FROM edition_work ew JOIN edition e ON e.id = ew.edition_id "
            "LEFT JOIN holding h ON h.edition_id = e.id WHERE ew.work_id = ?", (work_id,)).fetchall()

    def has_author_link(self, work_id, person_id):
        return self._a.ro.execute(
            "SELECT 1 FROM work_author WHERE work_id = ? AND person_id = ?",
            (work_id, person_id)).fetchone() is not None

    def has_edition_link(self, work_id):
        return self._a.ro.execute(
            "SELECT 1 FROM edition_work WHERE work_id = ? LIMIT 1", (work_id,)).fetchone() is not None

    def edition_link_count(self, work_id):
        return self._a.ro.execute(
            "SELECT COUNT(*) FROM edition_work WHERE work_id = ?", (work_id,)).fetchone()[0]

    def subject_ids(self, work_id):
        return [r[0] for r in self._a.ro.execute(
            "SELECT subject_id FROM work_subject WHERE work_id = ?", (work_id,)).fetchall()]

    def hard_delete(self, work_id):
        self._a.rw.execute("DELETE FROM work WHERE id = ?", (work_id,))

    def update_alias(self, alias_id, text):
        self._a.rw.execute("UPDATE work_alias SET text = ?, normalized_key = ? WHERE id = ?",
                           (text, fold_key(text), alias_id))

    def delete_alias(self, alias_id):
        self._a.rw.execute("DELETE FROM work_alias WHERE id = ?", (alias_id,))

    def rename_alias_checked(self, alias_id, work_id, text):
        return self._a.rw.execute(
            "UPDATE work_alias SET text = ?, normalized_key = ? WHERE id = ? AND work_id = ?",
            (text, fold_key(text), alias_id, work_id)).rowcount > 0

    def set_alias_fields(self, alias_id, text, scheme):
        self._a.rw.execute(
            "UPDATE work_alias SET text = ?, scheme = ?, normalized_key = ? WHERE id = ?",
            (text, scheme, fold_key(text), alias_id))

    def remove_author(self, work_id, person_id, role):
        self._a.rw.execute(
            "DELETE FROM work_author WHERE work_id = ? AND person_id = ? AND role = ?",
            (work_id, person_id, role))

    def set_edition_work_note(self, edition_id, work_id, note):
        self._a.rw.execute(
            "UPDATE edition_work SET note = ? WHERE edition_id = ? AND work_id = ?",
            (note, edition_id, work_id))

    def unrelate_commentary(self, work_id, *, as_root=False):
        col = "to_work_id" if as_root else "from_work_id"
        self._a.rw.execute(
            f"DELETE FROM relationship WHERE {col} = ? AND relation = 'commentary_on'",
            (work_id,))

    def delete_aliases_by_scheme(self, work_id, scheme):
        self._a.rw.execute("DELETE FROM work_alias WHERE work_id = ? AND scheme = ?",
                           (work_id, scheme))

    def aliases(self, work_id):
        return [(t, sc) for t, sc in self._a.ro.execute(
            "SELECT text, scheme FROM work_alias WHERE work_id = ? ORDER BY id", (work_id,)).fetchall()]

    _REVIEW_COLS = ("canonical_system", "canonical_number", "sanskrit_title", "tibetan_title",
                    "work_type", "original_language", "review_status")

    def review_fields(self, work_id):
        row = self._a.ro.execute(
            f"SELECT {', '.join(self._REVIEW_COLS)} FROM work WHERE id = ?", (work_id,)).fetchone()
        return dict(zip(self._REVIEW_COLS, row)) if row else None

    def has_subject(self, work_id):
        return self._a.ro.execute(
            "SELECT 1 FROM work_subject WHERE work_id = ? LIMIT 1", (work_id,)).fetchone() is not None

    def has_author(self, work_id):
        return self._a.ro.execute(
            "SELECT 1 FROM work_author WHERE work_id = ? LIMIT 1", (work_id,)).fetchone() is not None

    def has_author_role(self, work_id):
        return self._a.ro.execute(
            "SELECT 1 FROM work_author WHERE work_id = ? AND role = 'author' LIMIT 1",
            (work_id,)).fetchone() is not None

    def author_less_ids(self, limit=None):
        sql = ("SELECT w.id FROM work w WHERE w.deleted_at IS NULL AND NOT EXISTS ("
               "  SELECT 1 FROM work_author wa WHERE wa.work_id = w.id AND wa.role = 'author') "
               "ORDER BY w.id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r[0] for r in self._a.ro.execute(sql).fetchall()]

    def canonical_unresolved_ids(self, limit=None, ids=None):
        if ids:
            ph = ",".join("?" * len(ids))
            return [r[0] for r in self._a.ro.execute(
                f"SELECT id FROM work WHERE id IN ({ph}) AND deleted_at IS NULL ORDER BY id",
                list(ids)).fetchall()]
        sql = ("SELECT id FROM work WHERE canonical_number IS NULL AND deleted_at IS NULL "
               "ORDER BY id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r[0] for r in self._a.ro.execute(sql).fetchall()]

    def canonical_unresolved_count(self):
        return self._a.ro.execute(
            "SELECT COUNT(*) FROM work WHERE canonical_number IS NULL AND deleted_at IS NULL "
            "AND EXISTS (SELECT 1 FROM work_alias a WHERE a.work_id = work.id)").fetchone()[0]

    def backing_filename(self, work_id):
        import os
        r = self._a.ro.execute(
            "SELECT h.file_path FROM edition_work ew "
            "JOIN holding h ON h.edition_id = ew.edition_id "
            "WHERE ew.work_id = ? AND h.file_path IS NOT NULL "
            "ORDER BY h.id LIMIT 1", (work_id,)).fetchone()
        return os.path.basename(r[0]) if r and r[0] else ""

    def incomplete_rows(self):
        return [(r[0], r[1]) for r in self._a.ro.execute(
            "SELECT id, review_status FROM work ORDER BY id").fetchall()]

    def count_incomplete(self):
        return self._a.ro.execute(
            "SELECT COUNT(*) FROM work w WHERE COALESCE(w.review_status, '') <> 'ok' AND ("
            "  NOT EXISTS (SELECT 1 FROM work_subject ws WHERE ws.work_id = w.id)"
            "  OR NOT EXISTS (SELECT 1 FROM work_author wa WHERE wa.work_id = w.id)"
            "  OR (w.canonical_number IS NULL AND COALESCE(TRIM(w.sanskrit_title), '') = '' "
            "      AND COALESCE(TRIM(w.tibetan_title), '') = '')"
            "  OR w.review_status = 'needs_fix')").fetchone()[0]

    def set_review_status(self, work_id, status):
        self._a.rw.execute(
            "UPDATE work SET review_status = ?, "
            "reviewed_at = CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END "
            "WHERE id = ?", (status, status, work_id))

    # ── work-authority staged writes (RW, no commit) ────────────────────────────
    def _check_vocab(self, values):
        """Reject a controlled-vocab value outside its field's vocabulary — the direct-command
        (webui / work-authority) counterpart to the gate's `choices` check on the entity-CRUD
        path. Both read the same CategoricalField registry, so validation is defined once."""
        errs = validate_categorical("work", values)
        if errs:
            raise IntegrityViolation("; ".join(errs))

    def insert_scalars(self, values):
        bad = set(values) - set(_WRITABLE_SCALARS)
        if bad:
            raise ValueError(f"refusing to insert unknown work columns: {sorted(bad)}")
        self._check_vocab(values)
        cols = list(values)
        if cols:
            cur = self._a.rw.execute(
                f"INSERT INTO work ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(values[c] for c in cols))
        else:
            cur = self._a.rw.execute("INSERT INTO work DEFAULT VALUES")
        return cur.lastrowid

    def coalesce_scalars(self, work_id, values):
        self._check_vocab(values)
        for col, value in values.items():
            if col not in _WRITABLE_SCALARS:
                raise ValueError(f"refusing to fill unknown work column: {col!r}")
            self._a.rw.execute(
                f"UPDATE work SET {col} = COALESCE({col}, ?) WHERE id = ?", (value, work_id))

    def set_scalars(self, work_id, values):
        self._check_vocab(values)
        for col, value in values.items():
            if col not in _WRITABLE_SCALARS:
                raise ValueError(f"refusing to set unknown work column: {col!r}")
            self._a.rw.execute(f"UPDATE work SET {col} = ? WHERE id = ?", (value, work_id))

    def ids_by_categorical(self, name, value):
        """LIVE work ids whose categorical field `name` matches `value` — a taxonomy SUPERSET
        expands to its whole subtree (e.g. tenet_system='Madhyamaka' matches every descendant
        leaf), a leaf/flat field is plain equality. The set-union query, realised as `IN (…)`."""
        if name not in _WRITABLE_SCALARS:
            raise ValueError(f"not a categorical work column: {name!r}")
        vals = query_closure("work", name, value)
        ph = ",".join("?" * len(vals))
        return [r[0] for r in self._a.ro.execute(
            f"SELECT id FROM work WHERE {name} IN ({ph}) AND deleted_at IS NULL ORDER BY id",
            vals).fetchall()]

    def set_native_title(self, work_id, column, value):
        if column not in ("sanskrit_title", "tibetan_title"):
            raise ValueError(f"not a native-title column: {column!r}")
        self._a.rw.execute(f"UPDATE work SET {column} = ? WHERE id = ?", (value, work_id))

    def set_work_type(self, work_id, work_type):
        wt = (work_type or "").strip() or None
        if wt:
            self._a.rw.execute("INSERT OR IGNORE INTO work_type (code, label) VALUES (?, ?)", (wt, wt))
        self._a.rw.execute("UPDATE work SET work_type = ? WHERE id = ?", (wt, work_id))

    def relate_commentary(self, commentary_wid, root_wid):
        rw = self._a.rw
        rw.execute("INSERT OR IGNORE INTO relation_type (code, label) VALUES "
                   "('commentary_on', 'Commentary on')")
        if not rw.execute("SELECT 1 FROM relationship WHERE from_work_id = ? AND "
                          "relation = 'commentary_on' AND to_work_id = ?",
                          (commentary_wid, root_wid)).fetchone():
            rw.execute("INSERT INTO relationship (from_work_id, relation, to_work_id) "
                       "VALUES (?, 'commentary_on', ?)", (commentary_wid, root_wid))
        self.set_work_type(commentary_wid, "commentary")
        self.set_work_type(root_wid, "root")

    # ── staged mutations (RW, no commit) ────────────────────────────────────────
    def tombstone(self, work_id):
        self._a.rw.execute("UPDATE work SET deleted_at = datetime('now') WHERE id = ?", (work_id,))

    def restore(self, work_id):
        self._a.rw.execute("UPDATE work SET deleted_at = NULL WHERE id = ?", (work_id,))

    def purge_review_item(self, rid):
        self._a.rw.execute("DELETE FROM review_queue WHERE id = ?", (rid,))

    def scrub_promotion_work(self, review_item_id, work_id):
        row = self._a.rw.execute(
            "SELECT work_ids FROM promotion WHERE review_item_id = ?", (review_item_id,)).fetchone()
        if not row:
            return
        try:
            ids = json.loads(row[0]) if row[0] else []
        except (ValueError, TypeError):
            return
        self._a.rw.execute("UPDATE promotion SET work_ids = ? WHERE review_item_id = ?",
                           (json.dumps([i for i in ids if i != work_id]), review_item_id))

    def merge(self, loser_id, winner_id):
        # Fold loser into winner: re-point every edge + non-FK ref, backfill the winner's empty
        # scalars, then HARD-delete the loser (a merge is absorption — recovery is the caller's
        # snapshot-undo, not a tombstone). One staged unit (no commit).
        rw = self._a.rw
        # 1. edition_work — PK (edition_id, work_id, sequence); a collision keeps the winner's row.
        rw.execute("UPDATE OR IGNORE edition_work SET work_id=? WHERE work_id=?", (winner_id, loser_id))
        rw.execute("DELETE FROM edition_work WHERE work_id=?", (loser_id,))
        # 2. work_author — PK (work_id, person_id, role); dedup via OR IGNORE.
        rw.execute("UPDATE OR IGNORE work_author SET work_id=? WHERE work_id=?", (winner_id, loser_id))
        rw.execute("DELETE FROM work_author WHERE work_id=?", (loser_id,))
        # 3. relationship — both ends point at works (no PK on the pair); plain UPDATE, then drop the
        #    self-loops a merge can create (loser↔winner becomes winner↔winner).
        rw.execute("UPDATE relationship SET from_work_id=? WHERE from_work_id=?", (winner_id, loser_id))
        rw.execute("UPDATE relationship SET to_work_id=? WHERE to_work_id=?", (winner_id, loser_id))
        rw.execute("DELETE FROM relationship WHERE from_work_id = to_work_id")
        # 4. edition_commentary_on — PK (edition_id, to_work_id); dedup via OR IGNORE.
        rw.execute("UPDATE OR IGNORE edition_commentary_on SET to_work_id=? WHERE to_work_id=?",
                   (winner_id, loser_id))
        rw.execute("DELETE FROM edition_commentary_on WHERE to_work_id=?", (loser_id,))
        # 5. collection / subject / tradition join tables — composite PK; dedup via OR IGNORE.
        for tbl in _WORKID_JOIN_TABLES:
            rw.execute(f"UPDATE OR IGNORE {tbl} SET work_id=? WHERE work_id=?", (winner_id, loser_id))
            rw.execute(f"DELETE FROM {tbl} WHERE work_id=?", (loser_id,))
        # 6. work_alias — move, deduped on fold-key against the winner's existing keys.
        for aid, text in rw.execute(
                "SELECT id, text FROM work_alias WHERE work_id=?", (loser_id,)).fetchall():
            if rw.execute("SELECT 1 FROM work_alias WHERE work_id=? AND normalized_key=?",
                          (winner_id, fold_key(text))).fetchone():
                rw.execute("DELETE FROM work_alias WHERE id=?", (aid,))
            else:
                rw.execute("UPDATE work_alias SET work_id=? WHERE id=?", (winner_id, aid))
        # 7. Backfill the winner's empty scalar fields from the loser (winner authoritative).
        canon = rw.execute(f"SELECT {', '.join(_CARRY_COLS)} FROM work WHERE id=?", (winner_id,)).fetchone()
        dup = rw.execute(f"SELECT {', '.join(_CARRY_COLS)} FROM work WHERE id=?", (loser_id,)).fetchone()
        for i, col in enumerate(_CARRY_COLS):
            if not canon[i] and dup[i]:
                rw.execute(f"UPDATE work SET {col}=? WHERE id=?", (dup[i], winner_id))
        # 8. Non-FK refs (WORK-owned only): re-point loser→winner — the decision survives onto the
        #    surviving work, it is NOT discarded.
        for rid, payload in self._owned_pending(rw, loser_id):
            payload["work_id"] = winner_id
            rw.execute("UPDATE review_queue SET payload_json=? WHERE id=?", (json.dumps(payload), rid))
        for review_item_id in self.promotion_arrays_with(loser_id):
            row = rw.execute("SELECT work_ids FROM promotion WHERE review_item_id=?",
                             (review_item_id,)).fetchone()
            ids, new = json.loads(row[0]), []
            for i in ids:
                v = winner_id if i == loser_id else i
                if v not in new:
                    new.append(v)
            rw.execute("UPDATE promotion SET work_ids=? WHERE review_item_id=?",
                       (json.dumps(new), review_item_id))
        # 9. HARD-delete the loser — a merge is ABSORPTION (its identity is fused into the winner,
        # not "deleted for later restore"); recovery is the caller's snapshot-undo. Its edges + refs
        # were re-pointed above, so the cascade drops nothing.
        rw.execute("DELETE FROM work WHERE id=?", (loser_id,))
