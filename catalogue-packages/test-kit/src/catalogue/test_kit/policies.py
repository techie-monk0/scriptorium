"""Test principals + policies — exercise the access-API authz gate without app role-mapping.

The gateway calls `policy.check(principal, action)` before every dispatch (see contracts.authz).
Production binds a `Policy` that maps webui roles / api-key scopes; tests want cheaper shapes:
`DenyAll` to assert the gate actually fires, `RecordingPolicy` to capture which `Action`
(resource / verb / mode) each call declared, and `principal()` to mint a caller in one line.
"""
from __future__ import annotations

from catalogue.contracts import Action, AllowAll, Policy, Principal


def principal(id: str = "tester", roles=(), scopes=()) -> Principal:
    """A `Principal` in one line (roles/scopes default empty)."""
    return Principal(id=id, roles=frozenset(roles), scopes=frozenset(scopes))


class DenyAll(Policy):
    """Refuses every action — for asserting the gateway's authz gate is actually consulted
    (a read or write through it must raise `Denied`)."""

    def allows(self, principal: Principal, action: Action) -> bool:
        return False


class RecordingPolicy(Policy):
    """Delegates to an inner policy (`AllowAll` by default) and records every `(principal, action)`
    the gateway checked, so a test can assert which `Action` a call declared — e.g. that reads
    declare `AccessMode.READ` and a delete declares `("edition", "delete", WRITE)`."""

    def __init__(self, inner: "Policy | None" = None):
        self.inner = inner or AllowAll()
        self.calls: list[tuple[Principal, Action]] = []

    def allows(self, principal: Principal, action: Action) -> bool:
        self.calls.append((principal, action))
        return self.inner.allows(principal, action)

    def actions(self) -> "list[Action]":
        """Just the Actions checked, in order (the common assertion target)."""
        return [a for _p, a in self.calls]
