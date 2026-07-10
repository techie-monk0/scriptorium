"""Self-test for the stability conformance suite: it must PASS a conformant provider and
CATCH the two ways a provider can break the contract (orphan a token on merge; reuse a token).
This is the suite testing itself; the catalogue and each tool run `run_stability_conformance`
against their own provider.
"""
from __future__ import annotations

import pytest

from catalogue.contracts import Resolution, run_stability_conformance


class FakeStableStore:
    """A correct in-memory provider: forwarding pointers on merge, frozen (never-reused) tokens."""
    def __init__(self):
        self._status: dict[str, str] = {}
        self._forward: dict[str, str] = {}
        self._n = 0

    def mint(self) -> str:
        self._n += 1
        tok = f"ed-{self._n}"               # monotonic — never reused
        self._status[tok] = "active"
        return tok

    def resolve(self, token: str):
        if token not in self._status:
            return None
        cur = token
        while cur in self._forward:         # follow to terminal canonical
            cur = self._forward[cur]
        return Resolution(status=self._status[token], canonical=cur)

    def merge(self, src: str, dst: str) -> None:
        self._forward[src] = dst
        self._status[src] = "merged"

    def tombstone(self, token: str) -> None:
        self._status[token] = "withdrawn"


class OrphaningStore(FakeStableStore):
    """Broken: a merge hard-deletes the source instead of forwarding it (violates S2)."""
    def merge(self, src: str, dst: str) -> None:
        del self._status[src]               # token now resolves to None → orphaned


class ReusingStore(FakeStableStore):
    """Broken: mint recycles a previously tombstoned token (violates S1)."""
    def tombstone(self, token: str) -> None:
        super().tombstone(token)
        self._recyclable = token

    def mint(self) -> str:
        tok = getattr(self, "_recyclable", None)
        if tok is not None:
            self._recyclable = None
            self._status[tok] = "active"    # hand the old token back out
            return tok
        return super().mint()


def test_suite_passes_a_conformant_provider():
    run_stability_conformance(FakeStableStore())        # must not raise


def test_suite_catches_orphaning_on_merge():
    with pytest.raises(AssertionError, match="orphaned"):
        run_stability_conformance(OrphaningStore())


def test_suite_catches_token_reuse():
    with pytest.raises(AssertionError, match="reused"):
        run_stability_conformance(ReusingStore())
