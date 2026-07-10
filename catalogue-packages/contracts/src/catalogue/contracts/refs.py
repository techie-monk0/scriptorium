"""`Ref` — a revalidatable reference to an entity.

Every cross-entity reference is a `Ref`, never a bare id, so a consumer can re-validate
identity (the `fingerprint`) before acting on it. This neutralizes the SQLite id-reuse
hazard: a stale `Ref` whose fingerprint no longer matches is caught instead of silently
rebinding onto a recycled id. See docs/access/entity_api_model.md §4/§6.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Ref:
    kind: str                       # "edition" | "work" | "person" | "holding" | "subject" | …
    id: int
    fingerprint: "str | None" = None   # identity guard; None when not yet/applicable
    rev: "int | None" = None           # optimistic-concurrency version; None when N/A (leaf files, plans)

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "id": self.id}
        if self.fingerprint is not None:
            d["fingerprint"] = self.fingerprint
        if self.rev is not None:
            d["rev"] = self.rev
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Ref":
        return cls(kind=d["kind"], id=d["id"], fingerprint=d.get("fingerprint"), rev=d.get("rev"))
