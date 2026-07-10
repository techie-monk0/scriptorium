"""Tradition entity — a lineage/tradition tag on works. Leaf root: only a work edge, trivial delete."""
from __future__ import annotations

from catalogue.contracts import BasicGate, FieldRule, Tradition

from ._leaf import LeafRepo, LeafSpec

TRADITION_SPEC = LeafSpec(
    resource="tradition",
    table="tradition",
    columns=("id", "name", "rev"),
    make_dto=lambda r: Tradition(id=r[0], name=r[1], rev=r[2]),
    work_link=("work_tradition", "tradition_id"),
    writable=("name",),
    gate=BasicGate({"tradition": {"name": FieldRule(required=True, max_len=200)}}),
)


class TraditionRepo(LeafRepo):
    def __init__(self, access):
        super().__init__(access, TRADITION_SPEC)
