"""integrity.py STABILITY_CHECKS — the S1/S2 static invariants for edition identity, run inside
check_integrity → verified_commit. See docs/access/external_tool_dependency_contract.md §Stability.
"""
from __future__ import annotations

import pytest

from catalogue.db_store import init_db, integrity as I


def _edition(conn, title="Bk"):
    return conn.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


def _labels(rep):
    return " || ".join(e["check"] for e in rep["errors"])


def test_clean_db_passes_stability(tmp_path):
    conn = init_db(tmp_path / "t.db")
    _edition(conn); _edition(conn, "B")
    assert I.check_integrity(conn)["ok"]


def test_missing_pub_id_is_flagged(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid = _edition(conn)
    conn.execute("DROP TRIGGER edition_pub_id_immutable")   # so we can inject the violation
    conn.execute("UPDATE edition SET pub_id = NULL WHERE id = ?", (eid,))
    assert "pub_id missing" in _labels(I.check_integrity(conn))


def test_duplicate_pub_id_is_flagged(tmp_path):
    conn = init_db(tmp_path / "t.db")
    a, b = _edition(conn, "A"), _edition(conn, "B")
    pa = conn.execute("SELECT pub_id FROM edition WHERE id=?", (a,)).fetchone()[0]
    # bypass the unique index by injecting through a temp detach is overkill; drop the index instead
    conn.execute("DROP INDEX edition_pub_id_uq")
    conn.execute("DROP TRIGGER edition_pub_id_immutable")
    conn.execute("UPDATE edition SET pub_id = ? WHERE id = ?", (pa, b))
    assert "pub_id duplicated" in _labels(I.check_integrity(conn))


def test_dangling_superseded_by_is_flagged(tmp_path):
    conn = init_db(tmp_path / "t.db")
    a = _edition(conn)
    conn.commit()                              # PRAGMA foreign_keys is a no-op inside a transaction
    conn.execute("PRAGMA foreign_keys=OFF")    # the FK would otherwise block this injection
    conn.execute("UPDATE edition SET superseded_by = 99999 WHERE id = ?", (a,))
    assert "forwarding pointer must resolve" in _labels(I.check_integrity(conn))


def test_self_forward_is_flagged(tmp_path):
    conn = init_db(tmp_path / "t.db")
    a = _edition(conn)
    conn.execute("UPDATE edition SET superseded_by = id WHERE id = ?", (a,))
    assert "self-forward" in _labels(I.check_integrity(conn))


def test_forwarding_cycle_is_flagged(tmp_path):
    conn = init_db(tmp_path / "t.db")
    a, b = _edition(conn, "A"), _edition(conn, "B")
    conn.execute("UPDATE edition SET superseded_by = ? WHERE id = ?", (b, a))
    conn.execute("UPDATE edition SET superseded_by = ? WHERE id = ?", (a, b))   # A->B->A
    assert "cycle" in _labels(I.check_integrity(conn))


def test_verified_commit_rolls_back_a_stability_violation(tmp_path):
    conn = init_db(tmp_path / "t.db")
    a = _edition(conn)
    conn.commit()
    conn.execute("UPDATE edition SET superseded_by = id WHERE id = ?", (a,))   # self-forward (S2; FK-valid)
    with pytest.raises(I.IntegrityError):
        I.verified_commit(conn)
    # rolled back — the bad pointer is gone
    assert conn.execute("SELECT superseded_by FROM edition WHERE id=?", (a,)).fetchone()[0] is None
