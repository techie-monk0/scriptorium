"""External-tool dependency contracts — the capability/restriction/action model that lets an
external consumer (BuddhistLLM, …) constrain catalogue operations on entities it depends on.

No behavior, no I/O. `ExternalToolDependency` is a client-supplied strategy — the same shape as
`authz.Policy` and the project's abstract-protocol-layers note: the access-api executor gathers the
tools an entity declares, asks each for its `Restriction` on the attempted `Capability`, combines them
most-restrictive-wins, and applies the result. Each tool ships exactly ONE implementation; the core
carries no per-tool logic of its own. The executor, the `ToolRegistry`, `claim()` and the live-join
`resolve()` reader live in access-api, not here.

See docs/access/external_tool_dependency_contract.md and citation_edition_contract_plan.md.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Enum

from .errors import CatalogueError
from .refs import Ref


class Capability(Enum):
    """A catalogue operation an external dependency can constrain. Stable vocabulary that tool
    implementations code against; any capability no tool constrains defaults to ALLOW."""
    PURGE = "purge"                  # hard-delete a root, freeing its id — the one a citer must forbid
    WITHDRAW = "withdraw"            # tombstone (id frozen, record kept, resolves)
    MERGE = "merge"                  # fold this entity into another (retires this id → forwarding pointer)
    SPLIT = "split"                  # divide one entity into several
    IDENTITY_EDIT = "identity_edit"  # re-point to a different work / replace content (forks a new id)
    DISPLAY_EDIT = "display_edit"    # title / attribution / tradition — no identity change


class Severity(Enum):
    """How hard a restriction pushes back on the operator's action. Ordered by value so the
    executor can take the max across all tools (most-restrictive-wins)."""
    ALLOW = 0        # this tool has no objection
    WARN = 1         # proceed, but surface the dependency to the operator
    CONFIRM = 2      # require explicit operator confirmation before proceeding
    DISALLOW = 3     # refuse the operation


@dataclass(frozen=True)
class Restriction:
    """One tool's stance on one capability: how hard to push back, plus side-effects the executor
    must carry out if the operation proceeds. `ALLOW` with no side-effects is abstention."""
    severity: Severity = Severity.ALLOW
    reason: str = ""
    force_redirect: bool = False      # a merge/withdraw must leave a forwarding pointer, never vanish
    enqueue_reconcile: bool = False   # record a change event for the tool (deferred; see plan D7)

    @staticmethod
    def combine(restrictions: "list[Restriction]") -> "Restriction":
        """Most-restrictive-wins across tools: max severity, OR of the side-effects, reasons joined.
        Pure; the access-api executor calls this after gathering each tool's stance."""
        if not restrictions:
            return Restriction()
        top = max(restrictions, key=lambda r: r.severity.value)
        reasons = "; ".join(r.reason for r in restrictions if r.reason)
        return Restriction(
            severity=top.severity,
            reason=reasons,
            force_redirect=any(r.force_redirect for r in restrictions),
            enqueue_reconcile=any(r.enqueue_reconcile for r in restrictions),
        )


class CapabilityRestricted(CatalogueError):
    """An `ExternalToolDependency` refused (or a combined `Restriction` disallowed) a capability on an
    entity an external tool depends on."""
    http_status = 409

    def __init__(self, capability: "Capability", entity: "Ref", reason: str = ""):
        self.capability = capability
        self.entity = entity
        self.reason = reason
        tail = f": {reason}" if reason else ""
        super().__init__(
            f"restricted: {capability.value} on {entity.kind}#{entity.id} "
            f"(external dependency){tail}")


class ExternalToolDependency(abc.ABC):
    """A client-supplied strategy: one external tool's restrictions on catalogue capabilities.

    Registered once per tool (keyed by `tool`); the access-api executor consults it for every
    constrained operation on an entity that declares this tool as a dependency. Pure policy — no I/O.
    The ABC is entity-agnostic (editions first; persons/works may grow dependencies later)."""

    tool: str   # the id stored in edition_external_dependency.tool, e.g. "buddhistllm"

    @abc.abstractmethod
    def restrict(self, capability: "Capability", entity: "Ref") -> "Restriction":
        """This tool's stance on `capability` for `entity`. Return `Restriction()` (ALLOW) to abstain.
        The executor combines stances across tools via `Restriction.combine`."""
        ...
