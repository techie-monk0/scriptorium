"""Tool-policy executor + registry — turns a `Capability` on an edition into the combined
`Restriction` its external-tool dependencies impose (most-restrictive-wins), and enforces it.

The catalogue core stays tool-ignorant: it knows only the `ExternalToolDependency` ABC (from
`contracts`) and this registry. Each trusted, first-party tool ships one impl under
`access_api/integrations/`, registered into `DEFAULT_REGISTRY` here. No authn, runtime
self-registration, or approval gate — tools are the owner's own (see citation_edition_contract_plan.md
D5). The DB `edition_purge_guard` trigger remains the hard backstop *under* this policy layer.

See docs/access/external_tool_dependency_contract.md.
"""
from __future__ import annotations

from catalogue.contracts import (
    Capability,
    CapabilityRestricted,
    ExternalToolDependency,
    Ref,
    Restriction,
    Severity,
)

from . import external_deps
from .integrations.buddhistllm import BuddhistLLMDependency


class ToolRegistry:
    """Maps a tool id → its `ExternalToolDependency` impl. Populated in code (trusted tools)."""
    def __init__(self):
        self._tools: "dict[str, ExternalToolDependency]" = {}

    def register(self, impl: "ExternalToolDependency") -> None:
        self._tools[impl.tool] = impl

    def get(self, tool: str) -> "ExternalToolDependency | None":
        return self._tools.get(tool)

    def all(self) -> "dict[str, ExternalToolDependency]":
        return dict(self._tools)


DEFAULT_REGISTRY = ToolRegistry()
DEFAULT_REGISTRY.register(BuddhistLLMDependency())


def combined_restriction(conn, capability: "Capability", edition_id: int, *,
                         registry: "ToolRegistry | None" = None) -> "Restriction":
    """Most-restrictive-wins combination of every registered dependent tool's stance on `capability`
    for this edition. Returns ALLOW (no side-effects) when the edition has no dependencies, or when
    its dependent tools have no registered policy (a dependency row without an impl abstains)."""
    registry = registry or DEFAULT_REGISTRY
    ref = Ref("edition", edition_id)
    stances = []
    for dep in external_deps.dependents(conn, edition_id):
        impl = registry.get(dep.tool)
        if impl is not None:
            stances.append(impl.restrict(capability, ref))
    return Restriction.combine(stances)


def enforce(conn, capability: "Capability", edition_id: int, *, confirmed: bool = False,
            registry: "ToolRegistry | None" = None) -> "Restriction":
    """Apply the combined restriction: raise `CapabilityRestricted` on DISALLOW (or on CONFIRM when
    `confirmed` is False); otherwise return it so the caller can surface a WARN reason. This is the
    policy layer — the DB purge-guard trigger is the backstop beneath it."""
    r = combined_restriction(conn, capability, edition_id, registry=registry)
    if r.severity is Severity.DISALLOW or (r.severity is Severity.CONFIRM and not confirmed):
        raise CapabilityRestricted(capability, Ref("edition", edition_id), r.reason)
    return r
