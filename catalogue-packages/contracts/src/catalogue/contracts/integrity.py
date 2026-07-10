"""`IntegrityGate` — validate + normalize a create/update payload before it is applied.

The precondition of a write: `plan_create` / `plan_update` run the gate, turning failures into
`Impact.blocks` (so a blocked plan is un-appliable and `apply` raises `IntegrityViolation`). This is
the *field-level* question — "is this payload well-formed?" (required fields present, no unknown
columns, within length) — kept separate from the writer's recheck, which answers the *state-level*
question ("does it still fit the current row?", the `StaleWrite` fingerprint guard).

`BasicGate` is the declarative default (a per-kind field-rule table); a richer entity supplies its
own gate. No I/O, no DB — pure functions of the payload, so the same gate runs server-side or in a
client previewing a create. See docs/access/entity_api_model.md §4/§5.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

from .impact import Block


class IntegrityGate(abc.ABC):
    """Strategy that validates + normalizes a write payload (the `{field: value}` of a create/update)."""

    @abc.abstractmethod
    def normalize(self, kind: str, changes: dict) -> dict:
        """A cleaned copy of `changes` (e.g. collapse whitespace) — no validation, no dropping of
        unknown keys (so `validate` can still flag them)."""

    @abc.abstractmethod
    def validate(self, kind: str, changes: dict, partial: bool = False) -> "tuple[Block, ...]":
        """Field-level `Block`s for an ALREADY-normalized payload (missing required, unknown field,
        over length, …). `partial` (an update patch) only validates the fields PRESENT — a required
        field merely absent is fine; one present-but-empty is still blocked. Empty ⇒ well-formed."""

    def check(self, kind: str, changes: dict, partial: bool = False) -> "tuple[dict, tuple[Block, ...]]":
        """Normalize then validate — the one call a writer makes. `partial=True` for an update patch.
        Returns (normalized, blocks)."""
        norm = self.normalize(kind, changes)
        return norm, tuple(self.validate(kind, norm, partial))


@dataclass(frozen=True)
class FieldRule:
    """A single field's constraints for `BasicGate`."""
    required: bool = False
    max_len: "int | None" = None
    choices: "tuple[str, ...] | None" = None   # if set, a non-empty value must be one of these
                                               # (the controlled-vocab guard — see contracts.fields)


class BasicGate(IntegrityGate):
    """Declarative gate driven by `{kind: {field: FieldRule}}`. Normalizes string values by
    collapsing whitespace; validates required-ness, max length, and rejects any field not declared
    for the kind (a write may only touch known columns — the same guard the writers' `_UPDATABLE`
    whitelist gives, lifted into a reusable contract)."""

    def __init__(self, rules: "dict[str, dict[str, FieldRule]]"):
        self._rules = rules

    def normalize(self, kind: str, changes: dict) -> dict:
        out = {}
        for k, v in changes.items():
            out[k] = " ".join(v.split()) if isinstance(v, str) else v
        return out

    def validate(self, kind: str, changes: dict, partial: bool = False) -> "tuple[Block, ...]":
        allowed = self._rules.get(kind, {})
        blocks: list[Block] = []
        for k in changes:
            if k not in allowed:
                blocks.append(Block("validation", f"unknown field {k!r} for {kind}"))
        for field, rule in allowed.items():
            present = field in changes
            v = changes.get(field)
            empty = v is None or (isinstance(v, str) and v == "")
            if rule.required:
                # create: the field must be present and non-empty. update (partial): a field merely
                # absent is fine (not being changed), but one set to empty is still rejected.
                if (not partial and (not present or empty)) or (present and empty):
                    blocks.append(Block("validation", f"{field} is required"))
            if rule.max_len and isinstance(v, str) and len(v) > rule.max_len:
                blocks.append(Block("validation", f"{field} exceeds {rule.max_len} characters"))
            if rule.choices and present and not empty and v not in rule.choices:
                blocks.append(Block("validation",
                                    f"{field}: {v!r} is not a valid option "
                                    f"({', '.join(rule.choices)})"))
        return tuple(blocks)


@dataclass(frozen=True)
class Query:
    """A read query: an optional case-insensitive substring filter + pagination. Serializable like
    every other contract, so a client builds it and the access-API / a remote adapter consume the
    same shape. `contains` matches the entity's display field (the store decides which)."""
    contains: "str | None" = None
    limit: int = 50
    offset: int = 0

    def to_dict(self) -> dict:
        return {"contains": self.contains, "limit": self.limit, "offset": self.offset}

    @classmethod
    def from_dict(cls, d: dict) -> "Query":
        return cls(d.get("contains"), int(d.get("limit", 50)), int(d.get("offset", 0)))
