"""Authorization contracts — the read/write + policy gate, shared by server and clients.

No behavior beyond the gate decision: `Policy` is a client-supplied strategy (the webui maps
its viewer/editor roles + api-key scopes to one; internal callers use `AllowAll`). Every access
op declares an `Action` (resource + verb + mode); the access-api gateway calls `policy.allows()`
before dispatch and raises `Denied`. See docs/access/entity_api_model.md §4/§9, and the
strategy/executor pattern in the project's abstract-protocol-layers note.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum

from .errors import CatalogueError


class AccessMode(Enum):
    """Reads and writes are separate surfaces, so this is the coarse authz axis."""
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class Principal:
    """Who is making the request. `roles`/`scopes` are what a `Policy` reasons over."""
    id: str
    roles: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)


# The internal full-access identity — services / cli / populate run as this.
SYSTEM = Principal(id="system", roles=frozenset({"system"}))


@dataclass(frozen=True)
class Action:
    """What a call wants to do: a resource + verb at a read/write mode.
    e.g. Action("edition", "delete", AccessMode.WRITE), Action("scan_ocr", "provenance", READ)."""
    resource: str
    verb: str
    mode: AccessMode


class Denied(CatalogueError):
    """A `Policy` refused an `Action` for a `Principal`."""
    http_status = 403

    def __init__(self, principal: "Principal", action: "Action", reason: str = ""):
        self.principal = principal
        self.action = action
        self.reason = reason
        tail = f": {reason}" if reason else ""
        super().__init__(
            f"denied: {principal.id} → {action.resource}.{action.verb} "
            f"({action.mode.value}){tail}")


class Policy(abc.ABC):
    """Client-supplied authorization strategy. The gateway calls `allows()` before every
    dispatch; access modules carry no auth logic of their own."""

    @abc.abstractmethod
    def allows(self, principal: Principal, action: Action) -> bool:
        ...

    def check(self, principal: Principal, action: Action) -> None:
        """Raise `Denied` if not allowed (the gateway's convenience wrapper)."""
        if not self.allows(principal, action):
            raise Denied(principal, action)


class AllowAll(Policy):
    """Full-access policy for internal/SYSTEM callers."""

    def allows(self, principal: Principal, action: Action) -> bool:
        return True
