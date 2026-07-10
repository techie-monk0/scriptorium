"""Sandbox fork/promote/discard + freeze-check."""
import os

import pytest

from catalogue.services import sandbox
from catalogue.db_store import connect, init_db


def _make_db(path):
    init_db(path)
    _add_person(path)


def _add_person(path):
    conn = connect(path)
    conn.execute("INSERT INTO person (primary_name) VALUES ('X')")
    conn.commit()
    conn.close()


def _work_count(path):
    conn = connect(path)
    try:
        return conn.execute("SELECT count(*) FROM person").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def live(tmp_path):
    p = str(tmp_path / "catalogue.db")
    _make_db(p)
    return p


def test_fork_creates_independent_copy(live):
    res = sandbox.fork(live)
    sb = res["sandbox"]
    assert os.path.exists(sb)
    assert _work_count(sb) == _work_count(live) == 1

    # A write to the sandbox does not touch live.
    conn = connect(sb)
    conn.execute("INSERT INTO person (primary_name) VALUES ('Y')")
    conn.commit()
    conn.close()
    assert _work_count(sb) == 2
    assert _work_count(live) == 1


def test_fork_refuses_existing_then_force(live):
    sandbox.fork(live)
    with pytest.raises(sandbox.SandboxError):
        sandbox.fork(live)
    # --force discards the old one and re-forks cleanly.
    sandbox.fork(live, force=True)
    assert os.path.exists(sandbox.sandbox_path(live))


def test_promote_swaps_sandbox_in_and_backs_up(live):
    sandbox.fork(live)
    sb = sandbox.sandbox_path(live)
    conn = connect(sb)
    conn.execute("INSERT INTO person (primary_name) VALUES ('Y')")
    conn.commit()
    conn.close()

    res = sandbox.promote(live)
    assert _work_count(live) == 2            # sandbox content is now live
    assert not os.path.exists(sb)            # sandbox consumed by the swap
    assert os.path.exists(res["backup"])     # pre-swap backup kept
    assert _work_count(res["backup"]) == 1   # backup is the old live


def test_promote_freeze_check_refuses_on_live_drift(live):
    sandbox.fork(live)
    # Mutate LIVE after forking — promote must refuse (would lose this).
    conn = connect(live)
    conn.execute("INSERT INTO person (primary_name) VALUES ('Y')")
    conn.commit()
    conn.close()

    assert sandbox.status(live)["live_drifted"] is True
    with pytest.raises(sandbox.SandboxError):
        sandbox.promote(live)
    # --force overrides.
    sandbox.promote(live, force=True)
    assert os.path.exists(live)


def test_discard_removes_sandbox(live):
    sandbox.fork(live)
    assert os.path.exists(sandbox.sandbox_path(live))
    sandbox.discard(live)
    assert not os.path.exists(sandbox.sandbox_path(live))
    assert not os.path.exists(sandbox.meta_path(live))


def test_status_without_sandbox(live):
    st = sandbox.status(live)
    assert st["sandbox_exists"] is False
    assert st["live_drifted"] is None
