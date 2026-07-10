"""Stability conformance suite (S1–S2) — a reusable, framework-agnostic check that any
`resolve()`-shaped implementation upholds the edition-identity stability contract.

Test-support, not runtime: pure logic over an injected `StabilityProvider`, no I/O of its own.
The catalogue runs it against its real store; each external tool runs it against its stub — both
in their own CI, so a future id-churn turns the offending side's suite red. S3 (opacity) is a
consumer-side test (substitute the token format and assert identical behaviour), not part of this
provider suite.

See docs/access/external_tool_dependency_contract.md and citation_edition_contract_plan.md §3.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Resolution:
    """What `resolve(token)` returns for any token ever minted — it always resolves to
    *something* (S2). `canonical` is the token now standing for this identity: the queried
    token itself when live, or the survivor's token after a merge/supersede. `status` is
    advisory ("active" | "merged" | "superseded" | "withdrawn")."""
    status: str
    canonical: str


@runtime_checkable
class StabilityProvider(Protocol):
    """The seam the conformance suite drives. An implementation wraps a real store (the
    catalogue) or a stub (a tool's test double). Tokens are opaque strings — the suite never
    parses them (it upholds S3 by construction)."""
    def mint(self) -> str: ...
    def resolve(self, token: str) -> "Resolution | None": ...
    def merge(self, src: str, dst: str) -> None: ...   # fold src into dst; src forwards to dst
    def tombstone(self, token: str) -> None: ...       # withdraw; token stays resolvable


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _follow(provider: "StabilityProvider", token: str, _bound: int = 64) -> str:
    """Walk the forwarding chain to its terminal canonical, with a cycle bound (S2: chains
    terminate, no cycles). Raises if a cycle or an over-long chain is found."""
    seen: set[str] = set()
    cur = token
    for _ in range(_bound):
        r = provider.resolve(cur)
        _assert(r is not None, f"S2: {cur!r} failed to resolve mid-chain")
        if r.canonical == cur:
            return cur
        _assert(r.canonical not in seen, f"S2: forwarding cycle at {cur!r}")
        seen.add(cur)
        cur = r.canonical
    raise AssertionError("S2: forwarding chain did not terminate within bound (cycle?)")


def run_stability_conformance(provider: "StabilityProvider") -> None:
    """Assert S1 (no rebind) + S2 (total resolvability) against `provider`. Raises
    `AssertionError` on the first violation; returns None if the provider conforms."""
    # S1: distinct mints; S2: a live token resolves to itself.
    a, b = provider.mint(), provider.mint()
    _assert(a != b, "S1: two mints returned the same token")
    ra, rb = provider.resolve(a), provider.resolve(b)
    _assert(ra is not None and rb is not None, "S2: a freshly minted token failed to resolve")
    _assert(ra.canonical == a, f"S2: a live token should be its own canonical ({a!r} -> {ra.canonical!r})")

    # S2: merge leaves a forwarding pointer — the retired token still resolves, to the survivor.
    provider.merge(a, b)
    ra2 = provider.resolve(a)
    _assert(ra2 is not None, "S2: a merged-away token stopped resolving (orphaned)")
    _assert(ra2.canonical == b, f"S2: merged token {a!r} must forward to {b!r}, got {ra2.canonical!r}")
    _assert(provider.resolve(b).canonical == b, "S2: the survivor must remain its own canonical")

    # S2: chains terminate — a->b then b->c ⇒ a resolves through to c.
    c = provider.mint()
    provider.merge(b, c)
    _assert(_follow(provider, a) == c, f"S2: chain {a!r}->{b!r}->{c!r} must resolve to {c!r}")
    _assert(_follow(provider, b) == c, "S2: chain must resolve through the intermediate")

    # S1: tombstone keeps the token resolvable and never reissues it.
    t = provider.mint()
    provider.tombstone(t)
    _assert(provider.resolve(t) is not None, "S2: a tombstoned token stopped resolving")
    _assert(provider.mint() != t, "S1: a token was reused after tombstone")
