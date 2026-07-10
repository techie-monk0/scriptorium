"""Tool-policy executor + registry (P3): each external tool declares its restrictions on catalogue
capabilities; the executor combines them most-restrictive-wins and enforces. BuddhistLLM is the first
impl. See docs/access/external_tool_dependency_contract.md and citation_edition_contract_plan.md §D4/D5.
"""
from __future__ import annotations

import pytest

from catalogue.access_api import external_deps as X
from catalogue.access_api import tool_policy
from catalogue.access_api.integrations.buddhistllm import BuddhistLLMDependency
from catalogue.contracts import (
    Capability, CapabilityRestricted, ExternalToolDependency, Restriction, Severity,
)
from catalogue.db_store import init_db


def _cited_edition(conn, tool="buddhistllm", title="Bk"):
    eid = conn.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    pub = conn.execute("SELECT pub_id FROM edition WHERE id=?", (eid,)).fetchone()[0]
    X.claim(conn, pub_id=pub, tool=tool)
    return eid, pub


# ── registry ────────────────────────────────────────────────────────────────────
def test_default_registry_has_buddhistllm():
    impl = tool_policy.DEFAULT_REGISTRY.get("buddhistllm")
    assert isinstance(impl, BuddhistLLMDependency)
    assert tool_policy.DEFAULT_REGISTRY.get("nope") is None


# ── BuddhistLLM's stances ─────────────────────────────────────────────────────────
def test_buddhistllm_disallows_purge_warns_merge_allows_display():
    d = BuddhistLLMDependency()
    assert d.restrict(Capability.PURGE, None).severity is Severity.DISALLOW
    m = d.restrict(Capability.MERGE, None)
    assert m.severity is Severity.WARN and m.force_redirect
    assert d.restrict(Capability.DISPLAY_EDIT, None).severity is Severity.ALLOW
    assert d.restrict(Capability.WITHDRAW, None).severity is Severity.ALLOW


# ── executor ───────────────────────────────────────────────────────────────────
def test_uncited_edition_is_allow(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid = conn.execute("INSERT INTO edition (title) VALUES ('x')").lastrowid
    assert tool_policy.combined_restriction(conn, Capability.PURGE, eid).severity is Severity.ALLOW
    tool_policy.enforce(conn, Capability.PURGE, eid)   # no raise


def test_enforce_raises_on_purge_of_a_cited_edition(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid, _ = _cited_edition(conn)
    with pytest.raises(CapabilityRestricted, match="frozen"):
        tool_policy.enforce(conn, Capability.PURGE, eid)


def test_enforce_returns_warn_for_merge_of_a_cited_edition(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid, _ = _cited_edition(conn)
    r = tool_policy.enforce(conn, Capability.MERGE, eid)   # WARN → returned, not raised
    assert r.severity is Severity.WARN and r.force_redirect


class _DisallowMerge(ExternalToolDependency):
    tool = "strict"
    def restrict(self, capability, entity):
        if capability is Capability.MERGE:
            return Restriction(Severity.DISALLOW, "strict tool forbids merges")
        return Restriction()


def test_most_restrictive_wins_across_tools(tmp_path):
    conn = init_db(tmp_path / "t.db")
    reg = tool_policy.ToolRegistry()
    reg.register(BuddhistLLMDependency())     # MERGE → WARN
    reg.register(_DisallowMerge())            # MERGE → DISALLOW
    eid = conn.execute("INSERT INTO edition (title) VALUES ('x')").lastrowid
    pub = conn.execute("SELECT pub_id FROM edition WHERE id=?", (eid,)).fetchone()[0]
    X.claim(conn, pub_id=pub, tool="buddhistllm")
    X.claim(conn, pub_id=pub, tool="strict")
    with pytest.raises(CapabilityRestricted, match="forbids merges"):
        tool_policy.enforce(conn, Capability.MERGE, eid, registry=reg)


# ── wired into the edition merge writer ───────────────────────────────────────────
def test_writer_merge_allows_a_buddhistllm_cited_loser_and_forwards(cat_acc):
    w = cat_acc.editions.writes
    loser = w.create({"title": "Loser"}).target.id
    winner = w.create({"title": "Winner"}).target.id
    lpub = cat_acc.rw.execute("SELECT pub_id FROM edition WHERE id=?", (loser,)).fetchone()[0]
    wpub = cat_acc.rw.execute("SELECT pub_id FROM edition WHERE id=?", (winner,)).fetchone()[0]
    X.claim(cat_acc.rw, pub_id=lpub, tool="buddhistllm")
    w.merge(loser, winner)   # BuddhistLLM WARNs (not blocks) → proceeds, loser forwards
    r = X.resolve(cat_acc.rw, lpub)
    assert r.status == "superseded" and r.canonical == wpub
