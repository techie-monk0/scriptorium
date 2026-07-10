"""Shared test kit: fixtures, fake stores, test principals/policies, and sample data.

The reusable test surface for the catalogue workspace, so tests assert behavior instead of
re-deriving setup:

  * **fixtures** (`plugin.py`, auto-loaded via the `pytest11` entry point): `cat_db` / `cat_conn` /
    `cat_acc` — a throwaway initialized DB, a seeding connection, and a SYSTEM-bound `Access`.
  * **fakes** (`fakes.py`): `InMemoryHoldingStore` — the in-memory `Store` adapter the port-adapter
    split exists for; exercise the access layer with no database.
  * **policies** (`policies.py`): `DenyAll`, `RecordingPolicy`, `principal()` — drive the authz gate.
  * **sample** (`sample.py`): `seed_minimal` — one fully-linked edition/holding/work/person graph.
"""
from .fakes import DEFAULT_TEXT_STATUS_CODES, InMemoryHoldingStore
from .policies import DenyAll, RecordingPolicy, principal
from .sample import seed_minimal

__all__ = [
    "InMemoryHoldingStore", "DEFAULT_TEXT_STATUS_CODES",
    "DenyAll", "RecordingPolicy", "principal",
    "seed_minimal",
]
