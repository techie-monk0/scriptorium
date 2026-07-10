"""access-API gateway + Holding read path — the first end-to-end vertical slice (Phase 3).

System: bind → policy gate → read-only connection → Holding DTO, against a real init_db DB.
Unit-ish: the policy gate denies, reads declare READ mode, the read connection can't write.
See docs/access/entity_api_model.md §5/§8/§9.
"""
from __future__ import annotations

import sqlite3

import pytest

from catalogue.access_api import bind, system_access
from catalogue.contracts import AccessMode, Action, Denied, Holding, Policy, Principal
from catalogue.db_store import init_db


def _seed(tmp_path):
    p = tmp_path / "t.db"
    c = init_db(p)
    eid = c.execute("INSERT INTO edition (title) VALUES ('Bk')").lastrowid
    c.execute("INSERT INTO holding (edition_id, file_path, content_hash, text_status) "
              "VALUES (?, '/lib/a.pdf', 't:abc', 'ocr_good')", (eid,))
    c.execute("INSERT INTO holding (edition_id, file_path, text_status) "
              "VALUES (?, '/lib/b.pdf', 'ocr_good')", (eid,))
    c.commit()
    c.close()
    return p, eid


def test_read_through_gateway_returns_dtos(tmp_path):
    p, eid = _seed(tmp_path)
    with system_access(p) as acc:
        hs = acc.holdings.reads.by_edition(eid)
        assert len(hs) == 2 and all(isinstance(h, Holding) for h in hs)
        one = acc.holdings.reads.get(hs[0].id)
        assert one.edition_id == eid and one.file_path == "/lib/a.pdf"
        assert one.ref().kind == "holding" and one.ref().fingerprint == "t:abc"
        assert acc.holdings.reads.get(99999) is None


def test_reads_use_a_readonly_connection(tmp_path):
    p, _ = _seed(tmp_path)
    with system_access(p) as acc:
        acc.holdings.reads.get(1)                      # opens the RO connection
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            acc.ro.execute("INSERT INTO edition (title) VALUES ('X')")


def test_policy_gate_denies_an_unauthorized_read(tmp_path):
    p, eid = _seed(tmp_path)

    class NoReads(Policy):
        def allows(self, principal, action):
            return False

    with bind(Principal(id="viewer"), NoReads(), p) as acc:
        with pytest.raises(Denied):
            acc.holdings.reads.by_edition(eid)


def test_reads_declare_read_mode_to_the_policy(tmp_path):
    p, eid = _seed(tmp_path)
    seen = []

    class Recorder(Policy):
        def allows(self, principal, action):
            seen.append(action)
            return True

    with bind(Principal(id="u"), Recorder(), p) as acc:
        acc.holdings.reads.by_edition(eid)
    assert seen and seen[0].resource == "holding" and seen[0].mode is AccessMode.READ
