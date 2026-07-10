"""Person persistence — the storage PORT + its SQLite adapter.

The Person access layer (reads/writes) programs against `PersonStore` and holds no SQL: policy, the
plan→apply orchestration and the orphan policy live there. `SqlitePersonStore` is the implementation
(SQL over the gateway's RO/RW connections). Person is a soft-delete root that OWNS its aliases +
external-ids (FK `ON DELETE CASCADE`) — but under soft-delete the row only tombstones, so those parts
stay attached and return on `restore`; nothing is purged. See entity_api_model.md §3/§5/§6.
"""
from __future__ import annotations

import abc

from catalogue.contracts import Person

_COLS = ("id, primary_name, role_hint, dates, external_id, verification_status, "
         "notes, tradition, tenet_system, rev")


def _person(row) -> Person:
    return Person(id=row[0], primary_name=row[1], role_hint=row[2], dates=row[3],
                  external_id=row[4], verification_status=row[5], notes=row[6],
                  tradition=row[7], tenet_system=row[8], rev=row[9])


class PersonStore(abc.ABC):
    """Port: the data operations the Person access layer needs (no policy/transaction logic)."""

    # ── reads (plan) ────────────────────────────────────────────────────────────
    @abc.abstractmethod
    def get(self, person_id: int) -> "Person | None": ...
    @abc.abstractmethod
    def list_by_work(self, work_id: int) -> "list[Person]":
        """The LIVE persons who author `work_id` (via work_author), in id order."""
    @abc.abstractmethod
    def list_page(self, contains, limit: int, offset: int) -> "list[Person]":
        """Live persons matching the optional name substring, id-ordered, one page."""
    @abc.abstractmethod
    def count(self, contains) -> int:
        """Total live persons matching the optional name substring (for pagination)."""
    @abc.abstractmethod
    def directory(self, query: "str | None") -> "list[Person]":
        """Live persons ordered by primary_name. If `query` is given, only those whose ANY alias's
        normalized fold contains it (the people-directory search — over every alias, not just the
        primary name). No pagination."""
    @abc.abstractmethod
    def aliases(self, person_id: int) -> "list[tuple]":
        """(id, text, scheme) for each of the person's aliases, id-ordered."""
    @abc.abstractmethod
    def external_ids(self, person_id: int) -> "list[tuple]":
        """(scheme, value) for each of the person's authority external-ids, scheme-ordered."""
    @abc.abstractmethod
    def all_names(self) -> "dict":
        """{person_id: [primary_name + every alias text]} across all persons — the ordinal/office-
        aware name-match scan (matching happens in Python over the whole corpus)."""
    @abc.abstractmethod
    def live_names(self) -> "list[tuple]":
        """(id, primary_name) for every LIVE person — the fold-collapse scan input."""
    @abc.abstractmethod
    def provisional_ids(self, limit) -> "list[int]":
        """LIVE person ids whose verification_status = 'provisional', id-ordered — the verify worklist."""
    @abc.abstractmethod
    def id_by_name(self, primary_name: str) -> "int | None":
        """The LIVE person id whose primary_name == `primary_name`, or None (the blob lookup)."""
    @abc.abstractmethod
    def has_alias_key(self, person_id: int, key: str) -> bool:
        """Whether the person already carries an alias folding to `key`."""
    @abc.abstractmethod
    def has_alias_scheme_key(self, person_id: int, scheme: str, key: str) -> bool:
        """Whether the person carries an alias of `scheme` folding to `key` (scheme-specific dedup)."""
    @abc.abstractmethod
    def id_by_external_id(self, value: str) -> "int | None":
        """The person id bound to authority id `value` — its own person.external_id OR a harvested
        person_external_id.value — or None."""
    @abc.abstractmethod
    def id_by_alias_key(self, key: str) -> "int | None":
        """The lowest person id carrying an alias whose normalized_key == `key`, or None."""
    @abc.abstractmethod
    def names_by_ids(self, person_ids) -> "list[str]":
        """The primary_names of the given person ids, name-ordered — a display by-line."""
    @abc.abstractmethod
    def live_primary_names(self) -> "list[str]":
        """Every LIVE person's primary_name, name-ordered — the person <datalist> autocomplete."""
    @abc.abstractmethod
    def directory_named(self, limit: int) -> "list[tuple]":
        """(id, primary_name) for the first `limit` LIVE persons, name-ordered — the edition-card
        translator dropdown."""
    @abc.abstractmethod
    def notes_suggestions(self, person_ids) -> dict:
        """{id: (id, notes, suggested_external_id)} for the given persons — the picker curator-note +
        unconfirmed-binding overlay."""
    @abc.abstractmethod
    def confirm_local(self, person_id: int) -> bool:
        """Flag an UNBOUND person 'confirmed_local' (human says no authority exists); returns whether
        a row changed (no-op if already bound). Staged."""
    @abc.abstractmethod
    def set_kind_if_unbound(self, person_id: int, status: str) -> bool:
        """Toggle verification_status to `status` ONLY for an unbound provisional/organization person
        (never touches an authority-bound row); returns whether a row changed. Staged."""
    @abc.abstractmethod
    def insert_person(self, primary_name: str, role_hint, dates, suggested_external_id=None) -> int:
        """Insert a bare person row (the get_or_create mint), optionally parking a
        `suggested_external_id` for the bind worklist; returns the new id. Staged."""
    @abc.abstractmethod
    def bind_external(self, person_id: int, ext_id, status: str) -> None:
        """Set person.external_id + verification_status (the authority bind). Staged."""
    @abc.abstractmethod
    def store_external_id(self, person_id: int, scheme: str, value: str) -> None:
        """Upsert one harvested cross-link id (person_external_id, one row per scheme). Staged."""
    @abc.abstractmethod
    def clear_external_ids(self, person_id: int) -> None:
        """Drop every person_external_id row for a person (a rebind to a new authority). Staged."""
    @abc.abstractmethod
    def authored_work_count(self, person_id: int) -> int:
        """How many DISTINCT works name `person_id` as a work_author — the '→ their works' count."""
    @abc.abstractmethod
    def is_author(self, person_id: int) -> bool:
        """Whether the person authors any work (work_author) — a cheap role probe."""
    @abc.abstractmethod
    def is_translator(self, person_id: int) -> bool:
        """Whether the person translates any edition (edition_translator OR a per-work override)."""
    @abc.abstractmethod
    def authored_work_roles(self, person_id: int) -> "list[tuple]":
        """(work_id, role) for every work_author row of a person, ordered (work_id, role) — the
        contributor-edit reference set (work_author only, distinct from `contributed_works`)."""
    @abc.abstractmethod
    def contributed_works(self, person_id: int) -> "list[tuple]":
        """(work_id, role, label) for works the person authored (work_author) or, failing that,
        translated (a work in an edition they translated). label = the work's first alias text."""
    @abc.abstractmethod
    def appearing_editions(self, person_id: int) -> "list[tuple]":
        """(edition_id, title, year, roles) for each LIVE edition the person appears in — book
        author, translator, per-work translator override, or author of a contained work — with their
        comma-joined role(s), ordered by year then id."""
    @abc.abstractmethod
    def orphaned_work_ids(self, person_id: int) -> "list[int]":
        """Live works authored by this person that would have NO other live author once it is
        tombstoned — the semantic orphans a person delete may leave (authorless work)."""

    # ── authority-dedup reads (identity keys + ranking; LIVE persons only) ───────
    @abc.abstractmethod
    def authority_keys(self, person_id: int) -> "list[str]":
        """Every authority key VALUE a LIVE person owns — its hub `external_id` plus every harvested
        `person_external_id` value (unfiltered by scheme; the caller filters to person schemes). Empty
        for an absent or tombstoned person."""
    @abc.abstractmethod
    def edge_count(self, person_id: int) -> int:
        """Total contributor edges on the person (work_author + edition_translator + edition_work
        translator override) — the survivor-ranking signal for dedup."""
    @abc.abstractmethod
    def keyed_person_ids(self) -> "list[int]":
        """Every LIVE person owning ≥1 authority key (a hub `external_id` or a `person_external_id`
        cross-link) — the candidate set for the dedup union-find."""
    @abc.abstractmethod
    def persons_with_key(self, key: str) -> "list[int]":
        """LIVE person ids owning `key` as a hub `external_id` OR a `person_external_id` value."""
    @abc.abstractmethod
    def harvest_incomplete(self, person_id: int) -> bool:
        """Whether the person's cross-link harvest was incomplete (a partial key-set). False if the
        person is absent or tombstoned."""

    # ── picker reads (worklist + merge-target search; LIVE persons only) ─────────
    @abc.abstractmethod
    def unresolved(self, limit, ids) -> "list[tuple]":
        """The picker worklist: provisional + unbound LIVE persons (or exactly the given `ids`),
        id-ordered, each as (id, primary_name, external_id, aliases) where `aliases` is the person's
        non-primary alias texts. `limit` caps the default (no-`ids`) list."""
    @abc.abstractmethod
    def unresolved_count(self) -> int:
        """Count of provisional + unbound LIVE persons — the worklist total for the 'N of M' header."""
    @abc.abstractmethod
    def search(self, query: str, exclude, limit: int) -> "list[tuple]":
        """LIVE persons whose ANY alias fold contains `query` (the merge-target picker), as
        (id, primary_name, dates, external_id), id-ordered, excluding `exclude`, capped at `limit`."""
    @abc.abstractmethod
    def find_by_alias_fold(self, name: str, exclude=None) -> "int | None":
        """The lowest-id LIVE person owning an alias whose fold equals fold(`name`) (optionally
        EXCLUDING `exclude`), or None — the §4.2 fold-key dedup used when minting a person (so a
        tombstone is never resurrected) and the split's existing-part lookup."""
    @abc.abstractmethod
    def resolve_unique_alias(self, name: str) -> "int | None":
        """The LIVE person id whose alias fold uniquely equals fold(`name`), or None when zero or
        ambiguous (>1) — the safe filename-detection link (never guesses among homonyms)."""

    # ── write-side check + staged mutations (no commit) ─────────────────────────
    @abc.abstractmethod
    def current(self, person_id: int) -> "Person | None":
        """The live person as the write transaction sees it, or None — the TOCTOU recheck."""
    @abc.abstractmethod
    def create(self, values: dict) -> int:
        """Insert a person from a validated payload; return the new id."""
    @abc.abstractmethod
    def update(self, person_id: int, values: dict) -> None:
        """Apply a validated field payload to a live person."""
    # ── alias sub-entity commands (no commit; the writer owns the transaction) ──
    @abc.abstractmethod
    def insert_alias(self, person_id: int, text: str, scheme: str) -> None:
        """Append an alias (normalized_key = fold(text), the §4.2 invariant)."""
    @abc.abstractmethod
    def remove_alias(self, person_id: int, alias_id: int) -> None: ...
    @abc.abstractmethod
    def alias_text(self, person_id: int, alias_id: int) -> "str | None":
        """The alias's display text, or None if it isn't this person's alias."""
    @abc.abstractmethod
    def has_alias_fold(self, person_id: int, name: str) -> bool:
        """Whether the person already has an alias whose fold equals fold(name)."""
    @abc.abstractmethod
    def set_primary_name(self, person_id: int, name: str) -> None:
        """Swap the person's primary_name (bumps rev)."""

    # ── merge (fold loser into winner; no commit) ───────────────────────────────
    @abc.abstractmethod
    def merge(self, loser_id: int, winner_id: int, keep_name_alias: bool = True) -> None:
        """Fold loser into winner: re-point all contributor edges (work_author / edition_author /
        edition_translator / edition_work override) + aliases + external-ids + person-owned non-FK
        refs onto the winner (deduped), backfill the winner's empty dates/external_id, then HARD-delete
        the loser (a merge is absorption — recovery is the caller's snapshot-undo). `keep_name_alias`
        governs the loser's display name on the survivor (kept by default; if False, removed). One
        staged unit (no commit)."""
    @abc.abstractmethod
    def merge_aliases_gained(self, loser_id: int, winner_id: int) -> "list[str]":
        """Alias texts the winner would gain (not already a winner alias by fold) — for the preview."""
    @abc.abstractmethod
    def merge_edge_counts(self, loser_id: int) -> dict:
        """{edge: row_count} for the contributor edges a merge re-points — for the Impact preview."""
    @abc.abstractmethod
    def split(self, blob_id: int, targets: "list[dict]") -> None:
        """Dissolve a conflated `blob_id` person into already-resolved `targets` ([{id, role}]):
        attach each target to ALL the blob's works (author role) or their editions (translator), then
        detach + HARD-delete the blob. One staged unit (no commit)."""

    @abc.abstractmethod
    def tombstone(self, person_id: int) -> None: ...
    @abc.abstractmethod
    def tombstone_work(self, work_id: int) -> None: ...
    @abc.abstractmethod
    def restore(self, person_id: int) -> None: ...


class SqlitePersonStore(PersonStore):
    """SQLite adapter over an `Access`'s RO/RW connections."""

    def __init__(self, access):
        self._a = access

    def get(self, person_id):
        row = self._a.ro.execute(
            f"SELECT {_COLS} FROM person WHERE id = ? AND deleted_at IS NULL",
            (person_id,)).fetchone()
        return _person(row) if row else None

    def list_by_work(self, work_id):
        return [_person(r) for r in self._a.ro.execute(
            f"SELECT {_COLS} FROM person "
            "JOIN work_author ON work_author.person_id = person.id "
            "WHERE work_author.work_id = ? AND person.deleted_at IS NULL "
            "ORDER BY person.id", (work_id,)).fetchall()]

    def _filter(self, contains):
        clauses, args = ["deleted_at IS NULL"], []
        if contains:
            clauses.append("primary_name LIKE ?")
            args.append(f"%{contains}%")
        return " WHERE " + " AND ".join(clauses), args

    def list_page(self, contains, limit, offset):
        where, args = self._filter(contains)
        return [_person(r) for r in self._a.ro.execute(
            f"SELECT {_COLS} FROM person{where} ORDER BY id LIMIT ? OFFSET ?",
            (*args, limit, offset)).fetchall()]

    def count(self, contains):
        where, args = self._filter(contains)
        return self._a.ro.execute(f"SELECT count(*) FROM person{where}", args).fetchone()[0]

    def directory(self, query):
        if query:
            from catalogue.db_store import fold_key
            cols = ("person.id, person.primary_name, person.role_hint, person.dates, "
                    "person.external_id, person.verification_status, person.notes, "
                    "person.tradition, person.tenet_system, person.rev")
            return [_person(r) for r in self._a.ro.execute(
                f"SELECT DISTINCT {cols} FROM person "
                "JOIN person_alias a ON a.person_id = person.id "
                "WHERE a.normalized_key LIKE ? AND person.deleted_at IS NULL "
                "ORDER BY person.primary_name", (f"%{fold_key(query)}%",)).fetchall()]
        return [_person(r) for r in self._a.ro.execute(
            f"SELECT {_COLS} FROM person WHERE deleted_at IS NULL "
            "ORDER BY primary_name").fetchall()]

    def aliases(self, person_id):
        return [(r[0], r[1], r[2]) for r in self._a.ro.execute(
            "SELECT id, text, scheme FROM person_alias WHERE person_id = ? ORDER BY id",
            (person_id,)).fetchall()]

    def external_ids(self, person_id):
        return [(r[0], r[1]) for r in self._a.ro.execute(
            "SELECT scheme, value FROM person_external_id WHERE person_id = ? ORDER BY scheme",
            (person_id,)).fetchall()]

    def all_names(self):
        names: dict = {}
        for pid, nm in self._a.ro.execute("SELECT id, primary_name FROM person").fetchall():
            names.setdefault(pid, []).append(nm)
        for pid, txt in self._a.ro.execute("SELECT person_id, text FROM person_alias").fetchall():
            names.setdefault(pid, []).append(txt)
        return names

    def live_names(self):
        return self._a.ro.execute(
            "SELECT id, primary_name FROM person WHERE deleted_at IS NULL").fetchall()

    def provisional_ids(self, limit=None):
        sql = ("SELECT id FROM person WHERE verification_status = 'provisional' "
               "AND deleted_at IS NULL ORDER BY id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r[0] for r in self._a.ro.execute(sql).fetchall()]

    def id_by_name(self, primary_name):
        r = self._a.ro.execute(
            "SELECT id FROM person WHERE primary_name = ? AND deleted_at IS NULL",
            (primary_name,)).fetchone()
        return r[0] if r else None

    def has_alias_key(self, person_id, key):
        return self._a.ro.execute(
            "SELECT 1 FROM person_alias WHERE person_id = ? AND normalized_key = ?",
            (person_id, key)).fetchone() is not None

    def has_alias_scheme_key(self, person_id, scheme, key):
        return self._a.ro.execute(
            "SELECT 1 FROM person_alias WHERE person_id = ? AND scheme = ? AND normalized_key = ?",
            (person_id, scheme, key)).fetchone() is not None

    def id_by_external_id(self, value):
        r = self._a.ro.execute(
            "SELECT id FROM person WHERE external_id = ? LIMIT 1", (value,)).fetchone()
        if r:
            return r[0]
        r = self._a.ro.execute(
            "SELECT person_id FROM person_external_id WHERE value = ? LIMIT 1", (value,)).fetchone()
        return r[0] if r else None

    def id_by_alias_key(self, key):
        r = self._a.ro.execute(
            "SELECT person_id FROM person_alias WHERE normalized_key = ? ORDER BY person_id LIMIT 1",
            (key,)).fetchone()
        return r[0] if r else None

    def names_by_ids(self, person_ids):
        ids = list(person_ids)
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        return [r[0] for r in self._a.ro.execute(
            f"SELECT primary_name FROM person WHERE id IN ({ph}) ORDER BY primary_name",
            ids).fetchall()]

    def live_primary_names(self):
        return [r[0] for r in self._a.ro.execute(
            "SELECT primary_name FROM person WHERE deleted_at IS NULL ORDER BY primary_name").fetchall()]

    def directory_named(self, limit):
        return self._a.ro.execute(
            "SELECT id, primary_name FROM person WHERE deleted_at IS NULL "
            "ORDER BY primary_name LIMIT ?", (limit,)).fetchall()

    def notes_suggestions(self, person_ids):
        ids = list(person_ids)
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        return {r[0]: r for r in self._a.ro.execute(
            f"SELECT id, notes, suggested_external_id FROM person WHERE id IN ({ph})",
            ids).fetchall()}

    def confirm_local(self, person_id):
        cur = self._a.rw.execute(
            "UPDATE person SET verification_status = 'confirmed_local' "
            "WHERE id = ? AND external_id IS NULL AND deleted_at IS NULL", (person_id,))
        return cur.rowcount > 0

    def set_kind_if_unbound(self, person_id, status):
        cur = self._a.rw.execute(
            "UPDATE person SET verification_status = ? "
            "WHERE id = ? AND external_id IS NULL AND deleted_at IS NULL "
            "AND verification_status IN ('provisional', 'organization')", (status, person_id))
        return cur.rowcount > 0

    def insert_person(self, primary_name, role_hint, dates, suggested_external_id=None):
        if suggested_external_id is not None:
            return self._a.rw.execute(
                "INSERT INTO person (primary_name, role_hint, dates, suggested_external_id) "
                "VALUES (?, ?, ?, ?)",
                (primary_name, role_hint, dates, suggested_external_id)).lastrowid
        return self._a.rw.execute(
            "INSERT INTO person (primary_name, role_hint, dates) VALUES (?, ?, ?)",
            (primary_name, role_hint, dates)).lastrowid

    def bind_external(self, person_id, ext_id, status):
        self._a.rw.execute(
            "UPDATE person SET external_id = ?, verification_status = ? WHERE id = ?",
            (ext_id, status, person_id))

    def store_external_id(self, person_id, scheme, value):
        self._a.rw.execute(
            "INSERT OR REPLACE INTO person_external_id (person_id, scheme, value) VALUES (?, ?, ?)",
            (person_id, scheme, value))

    def clear_external_ids(self, person_id):
        self._a.rw.execute(
            "DELETE FROM person_external_id WHERE person_id = ?", (person_id,))

    def authored_work_count(self, person_id):
        return self._a.ro.execute(
            "SELECT COUNT(DISTINCT work_id) FROM work_author WHERE person_id = ?",
            (person_id,)).fetchone()[0]

    def is_author(self, person_id):
        return self._a.ro.execute(
            "SELECT 1 FROM work_author WHERE person_id = ? LIMIT 1", (person_id,)).fetchone() is not None

    def is_translator(self, person_id):
        ro = self._a.ro
        return (ro.execute("SELECT 1 FROM edition_translator WHERE person_id = ? LIMIT 1",
                           (person_id,)).fetchone() is not None
                or ro.execute("SELECT 1 FROM edition_work WHERE translator_person_id = ? LIMIT 1",
                              (person_id,)).fetchone() is not None)

    def authored_work_roles(self, person_id):
        return self._a.ro.execute(
            "SELECT work_id, role FROM work_author WHERE person_id = ? ORDER BY work_id, role",
            (person_id,)).fetchall()

    def contributed_works(self, person_id):
        from catalogue.db_store import contributor_store as cs
        authored = self._a.ro.execute(
            "SELECT work_id, role FROM work_author WHERE person_id = ? ORDER BY work_id",
            (person_id,)).fetchall()
        authored_wids = {w for w, _ in authored}
        rows = list(authored) + [(w, "translator") for w in cs.person_work_ids(self._a.ro, person_id)
                                 if w not in authored_wids]
        out = []
        for wid, role in rows:
            lbl = self._a.ro.execute(
                "SELECT text FROM work_alias WHERE work_id = ? ORDER BY id LIMIT 1",
                (wid,)).fetchone()
            out.append((wid, role, lbl[0] if lbl else None))
        return out

    def appearing_editions(self, person_id):
        rows = self._a.ro.execute(
            "SELECT e.id, e.title, e.year, src.role FROM ("
            "  SELECT edition_id, role FROM edition_author WHERE person_id = ?"
            "  UNION SELECT edition_id, 'translator' FROM edition_translator WHERE person_id = ?"
            "  UNION SELECT edition_id, 'translator' FROM edition_work WHERE translator_person_id = ?"
            "  UNION SELECT ew.edition_id, wa.role || ' (contained work)' FROM work_author wa "
            "         JOIN edition_work ew ON ew.work_id = wa.work_id WHERE wa.person_id = ?"
            ") src JOIN v_live_edition e ON e.id = src.edition_id",
            (person_id, person_id, person_id, person_id)).fetchall()
        ed_map: dict = {}
        for eid, title, year, role in rows:
            slot = ed_map.setdefault(eid, {"id": eid, "title": title, "year": year, "roles": set()})
            slot["roles"].add(role)
        return [(d["id"], d["title"], d["year"], ", ".join(sorted(d["roles"])))
                for d in sorted(ed_map.values(), key=lambda d: (d["year"] or 0, d["id"]))]

    # ── alias sub-entity commands (RW, no commit) ───────────────────────────────
    def insert_alias(self, person_id, text, scheme):
        from catalogue.db_store import add_alias
        add_alias(self._a.rw, "person", person_id, text, scheme)   # folds normalized_key

    def remove_alias(self, person_id, alias_id):
        self._a.rw.execute("DELETE FROM person_alias WHERE id = ? AND person_id = ?",
                           (alias_id, person_id))

    def alias_text(self, person_id, alias_id):
        row = self._a.rw.execute("SELECT text FROM person_alias WHERE id = ? AND person_id = ?",
                                 (alias_id, person_id)).fetchone()
        return row[0] if row else None

    def has_alias_fold(self, person_id, name):
        from catalogue.db_store import fold_key
        return self._a.rw.execute(
            "SELECT 1 FROM person_alias WHERE person_id = ? AND normalized_key = ?",
            (person_id, fold_key(name))).fetchone() is not None

    def set_primary_name(self, person_id, name):
        self._a.rw.execute("UPDATE person SET primary_name = ?, rev = rev + 1 WHERE id = ?",
                           (name, person_id))

    # ── merge ───────────────────────────────────────────────────────────────────
    def merge_aliases_gained(self, loser_id, winner_id):
        from catalogue.db_store import fold_key
        win = {fold_key(t) for (t,) in self._a.rw.execute(
            "SELECT text FROM person_alias WHERE person_id=?", (winner_id,)).fetchall()}
        return [t for (t,) in self._a.rw.execute(
            "SELECT text FROM person_alias WHERE person_id=?", (loser_id,)).fetchall()
            if fold_key(t) not in win]

    def merge_edge_counts(self, loser_id):
        rw, n = self._a.rw, lambda sql: rw.execute(sql, (loser_id,)).fetchone()[0]
        return {
            "work_author.person_id": n("SELECT COUNT(*) FROM work_author WHERE person_id=?"),
            "edition_author.person_id": n("SELECT COUNT(*) FROM edition_author WHERE person_id=?"),
            "edition_translator.person_id": n("SELECT COUNT(*) FROM edition_translator WHERE person_id=?"),
            "edition_work.translator_person_id":
                n("SELECT COUNT(*) FROM edition_work WHERE translator_person_id=?"),
        }

    def merge(self, loser_id, winner_id, keep_name_alias=True):
        import json
        from catalogue.db_store import add_alias, fold_key
        from catalogue.db_store import contributor_store as cs
        from ..registry import PROMOTION_ID_ARRAYS, review_items_owned_by
        rw = self._a.rw
        # 1. contributor edges (work_author / edition_author / edition_translator / override).
        cs.repoint_person(rw, loser_id, winner_id)
        # 2. aliases — move, deduped on fold-key against the winner's existing keys.
        for aid, text in rw.execute(
                "SELECT id, text FROM person_alias WHERE person_id=?", (loser_id,)).fetchall():
            if rw.execute("SELECT 1 FROM person_alias WHERE person_id=? AND normalized_key=?",
                          (winner_id, fold_key(text))).fetchone():
                rw.execute("DELETE FROM person_alias WHERE id=?", (aid,))
            else:
                rw.execute("UPDATE person_alias SET person_id=? WHERE id=?", (winner_id, aid))
        # 2b. the loser's DISPLAY NAME on the survivor: keep it searchable (default) or, if the
        # caller declined, ensure it is NOT an alias of the survivor.
        loser_name = rw.execute("SELECT primary_name FROM person WHERE id=?", (loser_id,)).fetchone()[0]
        name_key = fold_key(loser_name) if loser_name else None
        has_name = name_key and rw.execute(
            "SELECT 1 FROM person_alias WHERE person_id=? AND normalized_key=?",
            (winner_id, name_key)).fetchone() is not None
        if keep_name_alias and loser_name and not has_name:
            add_alias(rw, "person", winner_id, loser_name, "english")
        elif not keep_name_alias and has_name:
            rw.execute("DELETE FROM person_alias WHERE person_id=? AND normalized_key=?",
                       (winner_id, name_key))
        # 3. external-ids — carry over (deduped), then backfill the winner's empty external_id.
        for scheme, value in rw.execute(
                "SELECT scheme, value FROM person_external_id WHERE person_id=?", (loser_id,)).fetchall():
            rw.execute("INSERT OR IGNORE INTO person_external_id (person_id, scheme, value) "
                       "VALUES (?,?,?)", (winner_id, scheme, value))
        rw.execute("DELETE FROM person_external_id WHERE person_id=?", (loser_id,))
        we = rw.execute("SELECT external_id FROM person WHERE id=?", (winner_id,)).fetchone()[0]
        le = rw.execute("SELECT external_id FROM person WHERE id=?", (loser_id,)).fetchone()[0]
        if not we and le:
            rw.execute("UPDATE person SET external_id=? WHERE id=?", (le, winner_id))
        # 4. dates — winner authoritative; backfill if empty.
        wd = rw.execute("SELECT dates FROM person WHERE id=?", (winner_id,)).fetchone()[0]
        ld = rw.execute("SELECT dates FROM person WHERE id=?", (loser_id,)).fetchone()[0]
        if not wd and ld:
            rw.execute("UPDATE person SET dates=? WHERE id=?", (ld, winner_id))
        # 5. non-FK refs (person-owned): re-point pending review items + promotion id-arrays.
        for item_type, key in review_items_owned_by("person"):
            for rid, pj in rw.execute(
                    "SELECT id, payload_json FROM review_queue "
                    "WHERE item_type=? AND status='pending'", (item_type,)).fetchall():
                payload = json.loads(pj)
                if payload.get(key) == loser_id:
                    payload[key] = winner_id
                    rw.execute("UPDATE review_queue SET payload_json=? WHERE id=?",
                               (json.dumps(payload), rid))
        col = PROMOTION_ID_ARRAYS["person"]
        for rid, arr in rw.execute(f"SELECT review_item_id, {col} FROM promotion").fetchall():
            ids = json.loads(arr or "[]")
            if loser_id in ids:
                new: list = []
                for i in ids:
                    v = winner_id if i == loser_id else i
                    if v not in new:
                        new.append(v)
                rw.execute(f"UPDATE promotion SET {col}=? WHERE review_item_id=?",
                           (json.dumps(new), rid))
        # 6. HARD-delete the loser — a merge is ABSORPTION (its identity is fused into the winner,
        # not "deleted for later restore"); recovery is the caller's snapshot-undo. Its FK children
        # (alias/external-id) were already moved in steps 2–3, so the cascade drops nothing.
        rw.execute("DELETE FROM person WHERE id=?", (loser_id,))

    def split(self, blob_id, targets):
        from catalogue.db_store import contributor_store as cs
        rw = self._a.rw
        # the blob's works (authored) + the editions it translated, plus those works' editions.
        work_ids = {w for (w,) in rw.execute(
            "SELECT work_id FROM work_author WHERE person_id=?", (blob_id,)).fetchall()}
        edition_ids = set(cs.person_edition_ids_as_translator(rw, blob_id))
        for wid in work_ids:
            for (eid,) in rw.execute(
                    "SELECT edition_id FROM edition_work WHERE work_id=?", (wid,)).fetchall():
                edition_ids.add(eid)
        for t in targets:
            if t["role"] == "translator":
                for eid in edition_ids:
                    cs.add_edition_translator(rw, eid, t["id"])
            else:
                for wid in work_ids:
                    cs.add_work_author(rw, wid, t["id"], t["role"])
        # remove the blob from every edge, then HARD-delete it + its now-orphan aliases/ids.
        cs.detach_person(rw, blob_id)
        rw.execute("DELETE FROM person_alias WHERE person_id=?", (blob_id,))
        rw.execute("DELETE FROM person_external_id WHERE person_id=?", (blob_id,))
        rw.execute("DELETE FROM person WHERE id=?", (blob_id,))

    def orphaned_work_ids(self, person_id):
        # A live work this person authors that has no OTHER live author once this person tombstones.
        # Mirrors SqliteEditionStore.orphaned_work_ids (other-live-edition → other-live-author).
        return [w for (w,) in self._a.ro.execute(
            "SELECT DISTINCT wa.work_id FROM work_author wa JOIN work w ON w.id = wa.work_id "
            "WHERE wa.person_id = ? AND w.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM work_author wa2 JOIN person p2 ON p2.id = wa2.person_id "
            "                WHERE wa2.work_id = wa.work_id AND wa2.person_id <> ? "
            "                AND p2.deleted_at IS NULL)",
            (person_id, person_id)).fetchall()]

    # ── authority-dedup reads (LIVE persons only) ───────────────────────────────
    def authority_keys(self, person_id):
        ro = self._a.ro
        row = ro.execute("SELECT external_id FROM person WHERE id = ? AND deleted_at IS NULL",
                         (person_id,)).fetchone()
        if row is None:
            return []                                   # absent or tombstoned → no identity keys
        keys = [row[0]] if row[0] else []
        keys += [v for (v,) in ro.execute(
            "SELECT value FROM person_external_id WHERE person_id = ?", (person_id,)).fetchall()]
        return keys

    def edge_count(self, person_id):
        return self._a.ro.execute(
            "SELECT (SELECT COUNT(*) FROM work_author WHERE person_id = ?) "
            "     + (SELECT COUNT(*) FROM edition_translator WHERE person_id = ?) "
            "     + (SELECT COUNT(*) FROM edition_work WHERE translator_person_id = ?)",
            (person_id, person_id, person_id)).fetchone()[0]

    def keyed_person_ids(self):
        return [p for (p,) in self._a.ro.execute(
            "SELECT id FROM person WHERE external_id IS NOT NULL AND deleted_at IS NULL "
            "UNION SELECT pe.person_id FROM person_external_id pe "
            "  JOIN person p ON p.id = pe.person_id WHERE p.deleted_at IS NULL").fetchall()]

    def persons_with_key(self, key):
        ro = self._a.ro
        ids = {p for (p,) in ro.execute(
            "SELECT id FROM person WHERE external_id = ? AND deleted_at IS NULL", (key,)).fetchall()}
        ids |= {p for (p,) in ro.execute(
            "SELECT pe.person_id FROM person_external_id pe JOIN person p ON p.id = pe.person_id "
            "WHERE pe.value = ? AND p.deleted_at IS NULL", (key,)).fetchall()}
        return sorted(ids)

    def harvest_incomplete(self, person_id):
        row = self._a.ro.execute(
            "SELECT harvest_incomplete FROM person WHERE id = ? AND deleted_at IS NULL",
            (person_id,)).fetchone()
        return bool(row[0]) if row else False

    # ── picker reads (worklist + merge-target search; LIVE persons only) ─────────
    def unresolved(self, limit, ids):
        ro = self._a.ro
        if ids:
            ph = ",".join("?" * len(ids))
            rows = ro.execute(
                f"SELECT id, primary_name, external_id FROM person "
                f"WHERE id IN ({ph}) AND deleted_at IS NULL ORDER BY id", list(ids)).fetchall()
        else:
            rows = ro.execute(
                "SELECT id, primary_name, external_id FROM person "
                "WHERE verification_status = 'provisional' AND external_id IS NULL "
                "AND deleted_at IS NULL ORDER BY id"
                + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
        out = []
        for pid, name, cur in rows:
            al = [t for (t,) in ro.execute(
                "SELECT text FROM person_alias WHERE person_id = ? ORDER BY id",
                (pid,)).fetchall() if t]
            out.append((pid, name, cur, tuple(a for a in al if a != name)))
        return out

    def unresolved_count(self):
        return self._a.ro.execute(
            "SELECT COUNT(*) FROM person WHERE verification_status = 'provisional' "
            "AND external_id IS NULL AND deleted_at IS NULL").fetchone()[0]

    def search(self, query, exclude, limit):
        from catalogue.db_store import fold_key
        return [(r[0], r[1], r[2], r[3]) for r in self._a.ro.execute(
            "SELECT DISTINCT p.id, p.primary_name, p.dates, p.external_id "
            "FROM person p JOIN person_alias a ON a.person_id = p.id "
            "WHERE a.normalized_key LIKE ? AND p.id != ? AND p.deleted_at IS NULL "
            "ORDER BY p.id LIMIT ?",
            (f"%{fold_key(query)}%", exclude if exclude is not None else -1, limit)).fetchall()]

    def find_by_alias_fold(self, name, exclude=None):
        from catalogue.db_store import fold_key
        row = self._a.ro.execute(
            "SELECT pa.person_id FROM person_alias pa JOIN person p ON p.id = pa.person_id "
            "WHERE pa.normalized_key = ? AND p.deleted_at IS NULL AND pa.person_id != ? "
            "ORDER BY pa.person_id LIMIT 1",
            (fold_key(name), exclude if exclude is not None else -1)).fetchone()
        return row[0] if row else None

    def resolve_unique_alias(self, name):
        from catalogue.db_store import fold_key
        key = fold_key(name or "")
        if not key:
            return None
        rows = self._a.ro.execute(
            "SELECT DISTINCT p.id FROM person p JOIN person_alias a ON a.person_id = p.id "
            "WHERE a.normalized_key = ? AND p.deleted_at IS NULL LIMIT 2", (key,)).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def current(self, person_id):
        row = self._a.rw.execute(
            f"SELECT {_COLS} FROM person WHERE id = ? AND deleted_at IS NULL",
            (person_id,)).fetchone()
        return _person(row) if row else None

    def create(self, values):
        cols = list(values)
        cur = self._a.rw.execute(
            f"INSERT INTO person ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(values[c] for c in cols))
        return cur.lastrowid

    def update(self, person_id, values):
        if not values:
            return
        set_clause = ", ".join(f"{c} = ?" for c in values)
        self._a.rw.execute(
            f"UPDATE person SET {set_clause}, rev = rev + 1 WHERE id = ? AND deleted_at IS NULL",
            (*values.values(), person_id))

    def tombstone(self, person_id):
        self._a.rw.execute(
            "UPDATE person SET deleted_at = datetime('now') WHERE id = ?", (person_id,))

    def tombstone_work(self, work_id):
        self._a.rw.execute(
            "UPDATE work SET deleted_at = datetime('now') WHERE id = ?", (work_id,))

    def restore(self, person_id):
        self._a.rw.execute("UPDATE person SET deleted_at = NULL WHERE id = ?", (person_id,))
