"""bind_conn — an Access over an EXISTING caller-owned connection (in-process composition).

The engine stages its mutations onto the caller's connection; acc.commit()/rollback() are no-ops, so
the CALLER owns the transaction. This makes `caller-snapshot → acc.<write> → caller commit` atomic —
the unblock for routing transactional services (dedup, snapshot-undo) through the engine.
See entity_api_model.md §5.
"""
from __future__ import annotations

import sqlite3

from catalogue.access_api import system_conn
from catalogue.contracts import Ref
from catalogue.db_store import init_db


def _two_people(conn):
    win = conn.execute("INSERT INTO person (primary_name) VALUES ('Winner')").lastrowid
    los = conn.execute("INSERT INTO person (primary_name) VALUES ('Loser')").lastrowid
    w = conn.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    conn.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w, los))
    conn.commit()
    return win, los


def test_engine_write_stages_onto_caller_conn_caller_commits(tmp_path):
    db = tmp_path / "bc.db"
    conn = init_db(db)
    win, los = _two_people(conn)

    acc = system_conn(conn)                       # share the caller's connection
    acc.persons.writes.merge(Ref("person", los), Ref("person", win))
    # staged on the caller's txn (read-your-writes), but NOT committed
    assert acc.persons.reads.get(los) is None                     # gone in this txn (merge absorbs)
    other = sqlite3.connect(db)
    assert other.execute("SELECT COUNT(*) FROM person WHERE id=?", (los,)).fetchone()[0] == 1, \
        "a separate connection must still see the loser until the caller commits"

    conn.commit()                                 # the CALLER owns the commit
    other2 = sqlite3.connect(db)
    assert other2.execute("SELECT COUNT(*) FROM person WHERE id=?", (los,)).fetchone()[0] == 0  # gone
    assert other2.execute("SELECT COUNT(*) FROM work_author WHERE person_id=?",
                          (los,)).fetchone()[0] == 0       # edge repointed, durable
    conn.close(); other.close(); other2.close()


def test_caller_rollback_undoes_a_staged_engine_write(tmp_path):
    db = tmp_path / "bc2.db"
    conn = init_db(db)
    win, los = _two_people(conn)

    acc = system_conn(conn)
    acc.persons.writes.merge(Ref("person", los), Ref("person", win))
    conn.rollback()                               # caller aborts the whole transaction

    # the merge is gone — loser live again, edge intact
    assert conn.execute("SELECT deleted_at FROM person WHERE id=?", (los,)).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM work_author WHERE person_id=?",
                        (los,)).fetchone()[0] == 1
    conn.close()


def test_acc_commit_close_are_noops_in_bind_conn(tmp_path):
    db = tmp_path / "bc3.db"
    conn = init_db(db)
    pid = conn.execute("INSERT INTO person (primary_name) VALUES ('P')").lastrowid
    conn.commit()
    acc = system_conn(conn)
    acc.persons.writes.apply(acc.persons.writes.plan_update(Ref("person", pid), {"dates": "1900"}))
    acc.commit()                                  # no-op
    acc.close()                                   # no-op — must NOT close the caller's connection
    # connection still usable + the staged update is visible to the caller (read-your-writes)
    assert conn.execute("SELECT dates FROM person WHERE id=?", (pid,)).fetchone()[0] == "1900"
    conn.commit(); conn.close()
