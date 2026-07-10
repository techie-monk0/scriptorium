"""contracts.authz + contracts.errors — the foundational shared contracts (reorg Phase 2).

These are consumed by the access-API gateway (server) and serialized to clients, so the
shapes + the HTTP mapping are pinned here. See docs/access/entity_api_model.md §4.
"""
from __future__ import annotations

import dataclasses

import pytest

from catalogue.contracts import (
    SYSTEM,
    AccessMode,
    Action,
    AllowAll,
    CatalogueError,
    Conflict,
    Denied,
    IntegrityViolation,
    NotFound,
    Policy,
    Principal,
    StaleWrite,
    ValidationError,
)


def test_error_taxonomy_http_status():
    assert NotFound().http_status == 404
    assert ValidationError().http_status == 422
    assert Conflict().http_status == 409
    assert IntegrityViolation().http_status == 409 and issubclass(IntegrityViolation, Conflict)
    assert issubclass(StaleWrite, Conflict)
    assert issubclass(NotFound, CatalogueError)


def test_principal_is_frozen_with_defaults():
    p = Principal(id="u1")
    assert p.roles == frozenset() and p.scopes == frozenset()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.id = "x"            # type: ignore[misc]
    assert SYSTEM.roles == frozenset({"system"})


def test_action_round_trips_for_serialization():
    a = Action("edition", "delete", AccessMode.WRITE)
    wire = {"resource": a.resource, "verb": a.verb, "mode": a.mode.value}
    assert wire == {"resource": "edition", "verb": "delete", "mode": "write"}
    assert Action(wire["resource"], wire["verb"], AccessMode(wire["mode"])) == a


def test_policy_is_abstract():
    with pytest.raises(TypeError):
        Policy()              # type: ignore[abstract]


def test_allow_all_permits_everything():
    pol = AllowAll()
    act = Action("edition", "delete", AccessMode.WRITE)
    assert pol.allows(SYSTEM, act) is True
    pol.check(SYSTEM, act)    # no raise


def test_deny_policy_raises_denied_with_context():
    class ReadOnlyPolicy(Policy):
        def allows(self, principal, action):
            return action.mode is AccessMode.READ

    pol = ReadOnlyPolicy()
    pol.check(SYSTEM, Action("edition", "get", AccessMode.READ))      # ok
    write = Action("edition", "delete", AccessMode.WRITE)
    with pytest.raises(Denied) as ei:
        pol.check(SYSTEM, write)
    assert ei.value.action is write and ei.value.principal is SYSTEM
    assert ei.value.http_status == 403 and isinstance(ei.value, CatalogueError)
