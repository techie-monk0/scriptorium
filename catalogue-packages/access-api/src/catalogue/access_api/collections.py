"""Collection entity — a named grouping of works. Leaf root: only a work edge, trivial delete."""
from __future__ import annotations

from catalogue.contracts import BasicGate, Collection, FieldRule

from ._leaf import LeafRepo, LeafSpec

COLLECTION_SPEC = LeafSpec(
    resource="collection",
    table="collection",
    columns=("id", "name", "rev"),
    make_dto=lambda r: Collection(id=r[0], name=r[1], rev=r[2]),
    work_link=("collection_member", "collection_id"),
    writable=("name",),
    gate=BasicGate({"collection": {"name": FieldRule(required=True, max_len=200)}}),
)


class CollectionRepo(LeafRepo):
    def __init__(self, access):
        super().__init__(access, COLLECTION_SPEC)
