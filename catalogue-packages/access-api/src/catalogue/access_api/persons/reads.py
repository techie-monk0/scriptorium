"""Person read surface — queries only, storage-agnostic.

Declares a READ `Action`, authorizes it, then delegates to the `PersonStore` (RO connection). No
SQL here — the store is the implementation; tombstoned persons read as absent. See entity_api_model.md §8.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

from .. import _crud


class PersonReader:
    RESOURCE = "person"

    def __init__(self, access, store):
        self._a = access
        self._s = store

    def _read(self, verb: str) -> None:
        self._a.authorize(Action(self.RESOURCE, verb, AccessMode.READ))

    def get(self, person_id: int):
        """One **live** person by id, or None (a soft-deleted tombstone reads as absent)."""
        self._read("get")
        return self._s.get(person_id)

    def by_work(self, work_id: int):
        """Every **live** person who authors `work_id` (via work_author), in id order."""
        self._read("by_work")
        return self._s.list_by_work(work_id)

    def list(self, query=None):
        """One page of **live** persons (id-ordered), filtered by `query.contains` (name substring)
        and paginated by `query.limit`/`offset`. Defaults to the first 50."""
        return _crud.list_page(self._a, self.RESOURCE, self._s, query)

    def count(self, query=None) -> int:
        """Total **live** persons matching `query.contains` — the pagination total for `list`."""
        return _crud.count(self._a, self.RESOURCE, self._s, query)

    def directory(self, query=None):
        """The people directory: **live** persons ordered by primary_name. `query` filters to those
        with ANY alias whose fold matches (search spans every alias, not just the primary name)."""
        self._read("directory")
        return self._s.directory(query)

    def aliases(self, person_id: int):
        """The person's aliases as (id, text, scheme), id-ordered."""
        self._read("aliases")
        return self._s.aliases(person_id)

    def external_ids(self, person_id: int):
        """The person's authority external-ids as (scheme, value), scheme-ordered."""
        self._read("external_ids")
        return self._s.external_ids(person_id)

    def all_names(self):
        """{person_id: [primary_name + every alias]} across all persons — the search name-match scan."""
        self._read("all_names")
        return self._s.all_names()

    def live_names(self):
        """(id, primary_name) for every LIVE person — the fold-collapse scan input."""
        self._read("live_names")
        return self._s.live_names()

    def provisional_ids(self, limit=None):
        """LIVE person ids whose verification_status = 'provisional', id-ordered — the verify worklist."""
        self._read("provisional_ids")
        return self._s.provisional_ids(limit)

    def id_by_name(self, primary_name: str):
        """The LIVE person id whose primary_name == `primary_name`, or None."""
        self._read("id_by_name")
        return self._s.id_by_name(primary_name)

    def has_alias_key(self, person_id: int, key: str) -> bool:
        """Whether the person already carries an alias folding to `key`."""
        self._read("has_alias_key")
        return self._s.has_alias_key(person_id, key)

    def has_alias_scheme_key(self, person_id: int, scheme: str, key: str) -> bool:
        """Whether the person carries an alias of `scheme` folding to `key`."""
        self._read("has_alias_scheme_key")
        return self._s.has_alias_scheme_key(person_id, scheme, key)

    def id_by_external_id(self, value: str):
        """The person id bound to authority id `value` (own external_id or harvested), or None."""
        self._read("id_by_external_id")
        return self._s.id_by_external_id(value)

    def id_by_alias_key(self, key: str):
        """The lowest person id carrying an alias whose normalized_key == `key`, or None."""
        self._read("id_by_alias_key")
        return self._s.id_by_alias_key(key)

    def names_by_ids(self, person_ids):
        """The primary_names of the given person ids, name-ordered."""
        self._read("names_by_ids")
        return self._s.names_by_ids(person_ids)

    def live_primary_names(self):
        """Every LIVE person's primary_name, name-ordered — the person <datalist> autocomplete."""
        self._read("live_primary_names")
        return self._s.live_primary_names()

    def directory_named(self, limit: int = 100):
        """(id, primary_name) for the first `limit` LIVE persons, name-ordered."""
        self._read("directory_named")
        return self._s.directory_named(limit)

    def notes_suggestions(self, person_ids):
        """{id: (id, notes, suggested_external_id)} for the given persons — the picker note overlay."""
        self._read("notes_suggestions")
        return self._s.notes_suggestions(person_ids)

    def authored_work_count(self, person_id: int) -> int:
        """How many DISTINCT works name a person as a work_author — the '→ their works' count."""
        self._read("authored_work_count")
        return self._s.authored_work_count(person_id)

    def is_author(self, person_id: int) -> bool:
        """Whether the person authors any work (work_author)."""
        self._read("is_author")
        return self._s.is_author(person_id)

    def is_translator(self, person_id: int) -> bool:
        """Whether the person translates any edition (edition_translator or per-work override)."""
        self._read("is_translator")
        return self._s.is_translator(person_id)

    def authored_work_roles(self, person_id: int):
        """(work_id, role) for every work_author row of a person (work_author only) — the
        contributor-edit reference set."""
        self._read("authored_work_roles")
        return self._s.authored_work_roles(person_id)

    def contributed_works(self, person_id: int):
        """Works the person authored or translated, as (work_id, role, label)."""
        self._read("contributed_works")
        return self._s.contributed_works(person_id)

    def appearing_editions(self, person_id: int):
        """LIVE editions the person appears in, as (edition_id, title, year, roles)."""
        self._read("appearing_editions")
        return self._s.appearing_editions(person_id)

    # ── authority-dedup reads (identity keys + ranking; LIVE persons only) ───────
    def authority_keys(self, person_id: int):
        """The person's authority key VALUES (hub external_id + cross-link values), live only."""
        self._read("authority_keys")
        return self._s.authority_keys(person_id)

    def edge_count(self, person_id: int) -> int:
        """The person's total contributor edges — the dedup survivor-ranking signal."""
        self._read("edge_count")
        return self._s.edge_count(person_id)

    def keyed_person_ids(self):
        """Every LIVE person owning ≥1 authority key — the dedup union-find candidate set."""
        self._read("keyed_person_ids")
        return self._s.keyed_person_ids()

    def persons_with_key(self, key: str):
        """LIVE person ids owning `key` (hub external_id OR a person_external_id value)."""
        self._read("persons_with_key")
        return self._s.persons_with_key(key)

    def harvest_incomplete(self, person_id: int) -> bool:
        """Whether the person's cross-link harvest was incomplete (partial key-set)."""
        self._read("harvest_incomplete")
        return self._s.harvest_incomplete(person_id)

    # ── picker reads (worklist + merge-target search; LIVE persons only) ─────────
    def unresolved(self, *, limit=None, ids=None):
        """The picker worklist: provisional+unbound LIVE persons (or exactly `ids`), each as
        (id, primary_name, external_id, aliases)."""
        self._read("unresolved")
        return self._s.unresolved(limit, ids)

    def unresolved_count(self) -> int:
        """Count of provisional+unbound LIVE persons (the worklist total)."""
        self._read("unresolved")
        return self._s.unresolved_count()

    def search(self, query: str, *, exclude=None, limit: int = 20):
        """LIVE persons whose any alias fold contains `query` (merge-target picker), as
        (id, primary_name, dates, external_id)."""
        self._read("search")
        return self._s.search(query, exclude, limit)

    def find_by_alias_fold(self, name: str, exclude=None):
        """The lowest-id LIVE person owning an alias whose fold equals fold(`name`) (optionally
        EXCLUDING `exclude`), or None — the mint dedup + the split's existing-part lookup."""
        self._read("find_by_alias_fold")
        return self._s.find_by_alias_fold(name, exclude)

    def resolve_unique_alias(self, name: str):
        """The LIVE person id whose alias fold UNIQUELY equals fold(`name`), or None (zero/ambiguous)."""
        self._read("resolve_unique_alias")
        return self._s.resolve_unique_alias(name)
