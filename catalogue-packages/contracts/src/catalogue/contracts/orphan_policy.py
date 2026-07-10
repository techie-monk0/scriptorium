"""`OrphanPolicy` — the client-supplied strategy for roots a mutation would leave unanchored.

Deleting an Edition can strand a Work that had no other edition; merging can strand a Person.
What to do with such an orphan is a *policy*, not a fixed rule: the webui FLAGs it for human
review, a batch job GCs it, an import REFUSEs the whole mutation. So the access-API computes
*which* orphans a plan creates (the mechanism) and asks the caller's `OrphanPolicy` what to do
with each (the strategy) — the MoveResolver split the project favors ([[abstract-protocol-layers]]).
See docs/access/entity_api_model.md §4/§6.
"""
from __future__ import annotations

import abc

from .impact import OrphanDecision
from .refs import Ref


class OrphanPolicy(abc.ABC):
    """Strategy the planner consults for every orphan a mutation would create. `decide` sees the
    orphan's `Ref` and a human reason; it returns GC (delete it), FLAG (keep, queue for review),
    or REFUSE (the whole mutation becomes un-appliable)."""

    @abc.abstractmethod
    def decide(self, ref: Ref, reason: str) -> OrphanDecision:
        ...


class FlagOrphans(OrphanPolicy):
    """Keep every orphan and queue it for human review — the safe default (webui)."""

    def decide(self, ref: Ref, reason: str) -> OrphanDecision:
        return OrphanDecision.FLAG


class GCOrphans(OrphanPolicy):
    """Garbage-collect every orphan in the same transaction — a batch/cleanup caller."""

    def decide(self, ref: Ref, reason: str) -> OrphanDecision:
        return OrphanDecision.GC


class RefuseOrphans(OrphanPolicy):
    """Refuse any mutation that would orphan a root — a conservative importer."""

    def decide(self, ref: Ref, reason: str) -> OrphanDecision:
        return OrphanDecision.REFUSE
