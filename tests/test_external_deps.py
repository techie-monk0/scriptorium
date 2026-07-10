"""External-tool dependency flag + purge-guard (stability contract P2). A flagged edition is
un-deletable (tombstone instead), so its id + pub_id stay frozen. See
docs/access/external_tool_dependency_contract.md and citation_edition_contract_plan.md §3.
"""
from __future__ import annotations

import pytest

from catalogue.access_api import external_deps as X
from catalogue.contracts import run_stability_conformance
from catalogue.db_store import init_db


def _edition(conn, title="Bk"):
    eid = conn.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    pub = conn.execute("SELECT pub_id FROM edition WHERE id=?", (eid,)).fetchone()[0]
    return eid, pub


# ── claim ─────────────────────────────────────────────────────────────────────
def test_claim_flags_the_edition(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid, pub = _edition(conn)
    assert not X.is_flagged(conn, eid)
    dep = X.claim(conn, pub_id=pub, tool="buddhistllm", corpus="v2")
    assert dep.edition_id == eid and dep.tool == "buddhistllm" and dep.corpus == "v2"
    assert X.is_flagged(conn, eid)
    assert X.is_flagged(conn, eid, tool="buddhistllm")
    assert not X.is_flagged(conn, eid, tool="other")


def test_claim_is_idempotent_and_monotonic(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid, pub = _edition(conn)
    X.claim(conn, pub_id=pub, tool="buddhistllm", corpus="v1")
    X.claim(conn, pub_id=pub, tool="buddhistllm", corpus="v2")   # re-claim → one row, corpus refreshed
    deps = X.dependents(conn, eid)
    assert len(deps) == 1 and deps[0].corpus == "v2"


def test_claim_unknown_pub_id_raises(tmp_path):
    conn = init_db(tmp_path / "t.db")
    with pytest.raises(ValueError, match="no edition"):
        X.claim(conn, pub_id="00000000-0000-4000-8000-000000000000", tool="buddhistllm")


def test_editions_for_tool_is_segregated(tmp_path):
    conn = init_db(tmp_path / "t.db")
    e1, p1 = _edition(conn, "A")
    e2, p2 = _edition(conn, "B")
    X.claim(conn, pub_id=p1, tool="buddhistllm")
    X.claim(conn, pub_id=p2, tool="othertool")
    assert X.editions_for_tool(conn, "buddhistllm") == [e1]
    assert X.editions_for_tool(conn, "othertool") == [e2]


# ── purge-guard: the safety-critical rule ──────────────────────────────────────
def test_flagged_edition_cannot_be_hard_deleted(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid, pub = _edition(conn)
    X.claim(conn, pub_id=pub, tool="buddhistllm")
    with pytest.raises(Exception, match="external-tool dependencies"):
        conn.execute("DELETE FROM edition WHERE id=?", (eid,))
    # still there, still flagged
    assert conn.execute("SELECT count(*) FROM edition WHERE id=?", (eid,)).fetchone()[0] == 1


def test_flagged_edition_can_still_be_tombstoned(tmp_path):
    # Tombstone is an UPDATE (deleted_at), not a DELETE — the guard must not block it.
    conn = init_db(tmp_path / "t.db")
    eid, pub = _edition(conn)
    X.claim(conn, pub_id=pub, tool="buddhistllm")
    conn.execute("UPDATE edition SET deleted_at='2026-07-06' WHERE id=?", (eid,))
    row = conn.execute("SELECT deleted_at, pub_id FROM edition WHERE id=?", (eid,)).fetchone()
    assert row[0] == "2026-07-06" and row[1] == pub   # id + pub_id frozen


def test_unflagged_edition_is_still_hard_deletable(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid, _ = _edition(conn)
    conn.execute("DELETE FROM edition WHERE id=?", (eid,))   # no dependency → allowed
    assert conn.execute("SELECT count(*) FROM edition WHERE id=?", (eid,)).fetchone()[0] == 0


def test_guard_survives_reopen(tmp_path):
    # schema.sql (executescript) recreates the guard on every open; an existing DB gets it too.
    p = tmp_path / "t.db"
    conn = init_db(p)
    eid, pub = _edition(conn)
    X.claim(conn, pub_id=pub, tool="buddhistllm")
    conn.commit(); conn.close()
    conn2 = init_db(p)
    with pytest.raises(Exception, match="external-tool dependencies"):
        conn2.execute("DELETE FROM edition WHERE id=?", (eid,))


# ── resolve / supersede (S2 forwarding) ─────────────────────────────────────────
def test_resolve_live_edition_is_its_own_canonical(tmp_path):
    conn = init_db(tmp_path / "t.db")
    _, pub = _edition(conn)
    r = X.resolve(conn, pub)
    assert r is not None and r.status == "active" and r.canonical == pub


def test_resolve_unminted_token_is_none(tmp_path):
    conn = init_db(tmp_path / "t.db")
    assert X.resolve(conn, "00000000-0000-4000-8000-000000000000") is None


def test_supersede_forwards_token_to_winner(tmp_path):
    conn = init_db(tmp_path / "t.db")
    _, a = _edition(conn, "loser")
    _, b = _edition(conn, "winner")
    X.supersede(conn, old_pub_id=a, new_pub_id=b)
    ra = X.resolve(conn, a)
    assert ra.status == "superseded" and ra.canonical == b
    assert X.resolve(conn, b).canonical == b            # winner stays its own canonical


def test_supersede_chains_terminate(tmp_path):
    conn = init_db(tmp_path / "t.db")
    _, a = _edition(conn, "a"); _, b = _edition(conn, "b"); _, c = _edition(conn, "c")
    X.supersede(conn, old_pub_id=a, new_pub_id=b)
    X.supersede(conn, old_pub_id=b, new_pub_id=c)
    assert X.resolve(conn, a).canonical == c            # a -> b -> c
    assert X.resolve(conn, b).canonical == c


def test_supersede_rejects_self_and_cycles(tmp_path):
    conn = init_db(tmp_path / "t.db")
    _, a = _edition(conn, "a"); _, b = _edition(conn, "b")
    with pytest.raises(ValueError, match="itself"):
        X.supersede(conn, old_pub_id=a, new_pub_id=a)
    X.supersede(conn, old_pub_id=a, new_pub_id=b)
    with pytest.raises(ValueError, match="cycle"):
        X.supersede(conn, old_pub_id=b, new_pub_id=a)   # b -> a would close the loop


# ── the milestone: the REAL store passes the shared S1–S2 conformance suite ──────
class CatalogueStabilityProvider:
    """Adapts a real catalogue connection to the StabilityProvider seam the shared suite drives."""
    def __init__(self, conn):
        self.conn = conn
        self._i = 0

    def mint(self) -> str:
        self._i += 1
        eid = self.conn.execute("INSERT INTO edition (title) VALUES (?)", (f"E{self._i}",)).lastrowid
        return self.conn.execute("SELECT pub_id FROM edition WHERE id=?", (eid,)).fetchone()[0]

    def resolve(self, token):
        return X.resolve(self.conn, token)

    def merge(self, src, dst):
        X.supersede(self.conn, old_pub_id=src, new_pub_id=dst)

    def tombstone(self, token):
        self.conn.execute("UPDATE edition SET deleted_at=datetime('now') WHERE pub_id=?", (token,))


def test_real_catalogue_store_upholds_the_stability_contract(tmp_path):
    conn = init_db(tmp_path / "t.db")
    run_stability_conformance(CatalogueStabilityProvider(conn))   # S1 + S2, must not raise


# ── production merge is forwarding-aware for a cited loser ───────────────────────
def _pub(conn, eid):
    return conn.execute("SELECT pub_id FROM edition WHERE id=?", (eid,)).fetchone()[0]


def test_merge_forwards_a_cited_loser_instead_of_deleting(cat_acc):
    w = cat_acc.editions.writes
    loser = w.create({"title": "Loser"}).target.id
    winner = w.create({"title": "Winner"}).target.id
    lpub, wpub = _pub(cat_acc.rw, loser), _pub(cat_acc.rw, winner)
    X.claim(cat_acc.rw, pub_id=lpub, tool="buddhistllm")     # cite the loser
    w.merge(loser, winner)                                   # must NOT hard-delete it
    assert cat_acc.rw.execute("SELECT count(*) FROM edition WHERE id=?", (loser,)).fetchone()[0] == 1
    r = X.resolve(cat_acc.rw, lpub)
    assert r.status == "superseded" and r.canonical == wpub  # the citation still resolves, to the winner


def test_merge_still_hard_deletes_a_non_cited_loser(cat_acc):
    w = cat_acc.editions.writes
    loser = w.create({"title": "Loser"}).target.id
    winner = w.create({"title": "Winner"}).target.id
    lpub = _pub(cat_acc.rw, loser)
    w.merge(loser, winner)                                   # no dependency → existing behavior
    assert cat_acc.rw.execute("SELECT count(*) FROM edition WHERE id=?", (loser,)).fetchone()[0] == 0
    assert X.resolve(cat_acc.rw, lpub) is None
