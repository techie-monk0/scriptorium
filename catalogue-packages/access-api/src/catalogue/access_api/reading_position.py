"""Per-copy reading position — the gateway-bound access surface (`acc.reading_position`).

`reading_position` records where in a book file the reader last was (one row per holding: locator +
fraction). It is reader state, not a catalogue entity — a small keyed table — so this is a flat
policy-gated repo: the `get` reads over RO; the `upsert` STAGES on the caller's connection and the
route commits. See the catalogue-webui bookfiles reader routes and db_store/schema.sql.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

_RESOURCE = "reading_position"


class ReadingPositionRepo:
    def __init__(self, access):
        self._a = access

    def get(self, holding_id: int):
        """(locator, fraction) for a copy's saved position, or None when none recorded yet."""
        self._a.authorize(Action(_RESOURCE, "get", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT locator, fraction FROM reading_position WHERE holding_id = ?",
            (holding_id,)).fetchone()

    def upsert(self, holding_id: int, locator, fraction) -> None:
        """Upsert the reading position for a copy (the reader posts this as you read). Staged."""
        self._a.authorize(Action(_RESOURCE, "upsert", AccessMode.WRITE))
        self._a.rw.execute(
            "INSERT INTO reading_position (holding_id, locator, fraction, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(holding_id) DO UPDATE SET "
            "locator = excluded.locator, fraction = excluded.fraction, "
            "updated_at = CURRENT_TIMESTAMP",
            (holding_id, locator, fraction))
