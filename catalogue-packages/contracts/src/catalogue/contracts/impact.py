"""`Impact` — the serializable plan a mutation will execute.

Produced by `plan_*` (a read), inspected/authorized, then handed to `apply` (a write). It is
the **single boundary object** shared by server and clients: the access-API computes it, the
webui renders the blast radius, the PWA receives it as JSON, a future Swift client decodes the
same shape. It round-trips through JSON losslessly. See docs/access/entity_api_model.md §4/§5.

An `Impact` covers both the FK closure (informational `cascades`) and the **non-FK closure**
(`ref_purges`, `file_ops`, `link_repoints`, `orphans`) — the part no DB cascade can see.
`blocks` are integrity violations that make the plan un-appliable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .refs import Ref


class OrphanDecision(str, Enum):
    """What the client-supplied OrphanPolicy chose for an orphan a mutation would create."""
    GC = "gc"          # delete the now-unanchored entity
    FLAG = "flag"      # queue it for human review, keep it
    REFUSE = "refuse"  # refuse the whole mutation (becomes a block)


@dataclass(frozen=True)
class Orphan:
    """A root left unanchored by the mutation (Work with 0 editions, Person with 0 works…)."""
    ref: Ref
    reason: str
    decision: OrphanDecision

    def to_dict(self):
        return {"ref": self.ref.to_dict(), "reason": self.reason, "decision": self.decision.value}

    @classmethod
    def from_dict(cls, d):
        return cls(Ref.from_dict(d["ref"]), d["reason"], OrphanDecision(d["decision"]))


@dataclass(frozen=True)
class RefPurge:
    """A NON-FK reference to remove (a cover file, a hash-keyed cache row, a JSON-payload entry)
    — invisible to cascade, so it must be enumerated here and purged with the mutation."""
    kind: str          # "cover_file" | "cache_row" | "queue_payload" | "promotion_json" | …
    locator: str       # path, or "table:key", or a row id — enough to perform the purge
    owner: Ref         # the entity whose mutation triggers this purge

    def to_dict(self):
        return {"kind": self.kind, "locator": self.locator, "owner": self.owner.to_dict()}

    @classmethod
    def from_dict(cls, d):
        return cls(d["kind"], d["locator"], Ref.from_dict(d["owner"]))


@dataclass(frozen=True)
class FileOp:
    """A filesystem effect (trash a deleted holding's file, move a relinked one)."""
    op: str            # "trash" | "move"
    path: str
    dest: "str | None" = None

    def to_dict(self):
        d = {"op": self.op, "path": self.path}
        if self.dest is not None:
            d["dest"] = self.dest
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(d["op"], d["path"], d.get("dest"))


@dataclass(frozen=True)
class LinkRepoint:
    """An edge re-pointed instead of dropped (e.g. edition_commentary_on → the merge winner)."""
    edge: str
    from_ref: Ref
    to_ref: Ref

    def to_dict(self):
        return {"edge": self.edge, "from_ref": self.from_ref.to_dict(), "to_ref": self.to_ref.to_dict()}

    @classmethod
    def from_dict(cls, d):
        return cls(d["edge"], Ref.from_dict(d["from_ref"]), Ref.from_dict(d["to_ref"]))


@dataclass(frozen=True)
class Block:
    """An integrity violation that makes the plan un-appliable. `code` maps to the error taxonomy."""
    code: str
    message: str

    def to_dict(self):
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, d):
        return cls(d["code"], d["message"])


@dataclass(frozen=True)
class Impact:
    op: str            # "delete" | "merge" | "create" | "update" | "relink"
    target: Ref
    # The field values to write for create/update (the previewable payload); empty for
    # delete/merge whose effect is fully described by the structural fields below.
    changes: dict = field(default_factory=dict)
    cascades: tuple[Ref, ...] = ()
    orphans: tuple[Orphan, ...] = ()
    ref_purges: tuple[RefPurge, ...] = ()
    file_ops: tuple[FileOp, ...] = ()
    link_repoints: tuple[LinkRepoint, ...] = ()
    blocks: tuple[Block, ...] = ()

    @property
    def appliable(self) -> bool:
        """An Impact with any `blocks` (or a REFUSE orphan) cannot be applied."""
        return not self.blocks

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "target": self.target.to_dict(),
            "changes": dict(self.changes),
            "cascades": [r.to_dict() for r in self.cascades],
            "orphans": [o.to_dict() for o in self.orphans],
            "ref_purges": [p.to_dict() for p in self.ref_purges],
            "file_ops": [f.to_dict() for f in self.file_ops],
            "link_repoints": [l.to_dict() for l in self.link_repoints],
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Impact":
        return cls(
            op=d["op"],
            target=Ref.from_dict(d["target"]),
            changes=dict(d.get("changes", {})),
            cascades=tuple(Ref.from_dict(x) for x in d.get("cascades", [])),
            orphans=tuple(Orphan.from_dict(x) for x in d.get("orphans", [])),
            ref_purges=tuple(RefPurge.from_dict(x) for x in d.get("ref_purges", [])),
            file_ops=tuple(FileOp.from_dict(x) for x in d.get("file_ops", [])),
            link_repoints=tuple(LinkRepoint.from_dict(x) for x in d.get("link_repoints", [])),
            blocks=tuple(Block.from_dict(x) for x in d.get("blocks", [])),
        )
