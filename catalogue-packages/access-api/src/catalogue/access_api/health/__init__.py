"""Health / maintenance surface — cross-entity integrity primitives.

Unlike the per-entity repos, these reconcile the WHOLE database against the non-FK registry.
`OrphanSweep` is the first: scan (read) / apply (write) the non-FK orphan classes the cascade can't
reach. Wired onto the gateway as `acc.health`. See sweep.py / docs/access/entity_api_model.md §6.
"""
from .store import SqliteSweepStore, SweepStore
from .sweep import OrphanSweep

__all__ = ["OrphanSweep", "SweepStore", "SqliteSweepStore"]
