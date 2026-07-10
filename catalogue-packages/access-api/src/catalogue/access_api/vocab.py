"""Controlled-vocabulary code lists — the gateway-bound access surface (`acc.vocab`).

The small reference tables that back edit-form dropdowns (`work_type`, `alias_scheme`, …) are static
code lists, not catalogue entities. A flat READ-only policy-gated repo keeps their trivial `SELECT
code` queries out of the route/service layers. See db_store/schema.sql.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action, get_field

_RESOURCE = "vocab"
# Whitelist of code-list tables this repo may read — never an injected table name.
_CODE_TABLES = ("work_type", "alias_scheme", "locator_type", "text_status", "form_type",
                "holding_type")


class VocabRepo:
    def __init__(self, access):
        self._a = access

    def _codes(self, table: str):
        if table not in _CODE_TABLES:
            raise ValueError(f"not a known vocab table: {table!r}")
        self._a.authorize(Action(_RESOURCE, "codes", AccessMode.READ))
        return [r[0] for r in self._a.ro.execute(
            f"SELECT code FROM {table} ORDER BY code").fetchall()]

    def work_types(self):
        """Every work_type code (the work-card type dropdown)."""
        return self._codes("work_type")

    def alias_schemes(self):
        """Every alias_scheme code (the alias-edit scheme dropdown)."""
        return self._codes("alias_scheme")

    def locator_types(self):
        """Every locator_type code (the contained-work locator dropdown)."""
        return self._codes("locator_type")

    def traditions(self):
        """Every live tradition name (the tradition-field datalist / picker suggestions).
        The `tradition` table is name-keyed (seeded from vocab.json `_tradition`), not a
        code/label list, so it needs its own reader rather than `_codes`."""
        self._a.authorize(Action(_RESOURCE, "codes", AccessMode.READ))
        return [r[0] for r in self._a.ro.execute(
            "SELECT name FROM tradition WHERE deleted_at IS NULL ORDER BY name").fetchall()]

    def codes(self, table: str):
        """The codes of a whitelisted vocab table by name (the holding-fields editor's `codes()`)."""
        return self._codes(table)

    def field_values(self, entity: str, name: str):
        """The allowed values of a CategoricalField (entity, column) — the options a
        <select>/<datalist> renders. A fixed-vocab field returns its declared `values`; a
        table-backed field reads its live rows (`tradition` names, `work_type` codes, …). The one
        entry point the edit forms use for every scalar controlled-vocab field."""
        f = get_field(entity, name)
        if f is None:
            raise ValueError(f"no categorical field {entity}.{name}")
        if f.values:
            self._a.authorize(Action(_RESOURCE, "codes", AccessMode.READ))
            return list(f.values)
        if f.vocab_table == "tradition":
            return self.traditions()
        return self._codes(f.vocab_table)
