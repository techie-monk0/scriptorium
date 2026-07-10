"""End-to-end tests for the Phase-4-converted maintenance CLIs — driven through `main(argv)` so the
whole entry point (arg parse → bind a system Access → access-API call → stdout/exit) is exercised, not
just the underlying functions. Each CLI was routed off raw SQL onto the access-API; these pin that the
operator-facing command still does the right thing against a real DB.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import init_db
from catalogue.cli import (books_by_subject, exclude_purge, sweep_dangling_refs,
                           sweep_orphan_covers, verify)


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "c.db"
    init_db(p).close()
    return p


def _conn(p):
    return init_db(p)


# ── exclude_purge ────────────────────────────────────────────────────────────────
def test_exclude_purge_cli_dry_run_then_apply(db_path, capsys):
    conn = _conn(db_path)
    eid = conn.execute("INSERT INTO edition (title) VALUES ('Ann')").lastrowid
    conn.execute("INSERT INTO holding (edition_id, form, file_path) "
                 "VALUES (?, 'electronic', '/lib/x ANNOTATED/a.pdf')", (eid,))
    conn.commit(); conn.close()

    # dry run: reports, touches nothing
    assert exclude_purge.main([str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "1 excluded holding(s)" in out and "dry-run" in out
    conn = _conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM holding").fetchone()[0] == 1
    conn.close()

    # apply: holding gone, edition tombstoned (soft-delete via the access-API)
    assert exclude_purge.main([str(db_path), "--apply"]) == 0
    assert "deleted 1 holding(s)" in capsys.readouterr().out
    conn = _conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM holding").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM v_live_edition WHERE id=?", (eid,)).fetchone()[0] == 0
    conn.close()


# ── sweep_dangling_refs ──────────────────────────────────────────────────────────
def test_sweep_dangling_refs_cli_apply_drops_orphan(db_path, capsys):
    conn = _conn(db_path)
    rid = conn.execute("INSERT INTO review_queue (item_type, payload_json) "
                       "VALUES ('work_canonical', ?)", (json.dumps({"work_id": 99999}),)).lastrowid
    conn.commit(); conn.close()

    sweep_dangling_refs.main(["--db", str(db_path)])             # dry run
    assert "would drop" in capsys.readouterr().out
    conn = _conn(db_path)
    assert conn.execute("SELECT 1 FROM review_queue WHERE id=?", (rid,)).fetchone()   # untouched
    conn.close()

    sweep_dangling_refs.main(["--db", str(db_path), "--apply"])  # apply
    assert "dropped" in capsys.readouterr().out
    conn = _conn(db_path)
    assert conn.execute("SELECT 1 FROM review_queue WHERE id=?", (rid,)).fetchone() is None
    conn.close()


# ── verify (full non-FK health sweep) ────────────────────────────────────────────
def test_verify_cli_reports_then_fixes(db_path, capsys):
    conn = _conn(db_path)
    conn.execute("INSERT INTO review_queue (item_type, payload_json) "
                 "VALUES ('work_canonical', ?)", (json.dumps({"work_id": 77777}),))
    conn.commit(); conn.close()

    # report mode → exit 1 (orphans found), nothing changed
    assert verify.main(["--db", str(db_path)]) == 1
    assert "review items: 1" in capsys.readouterr().out
    # apply → exit 0, orphan dropped
    assert verify.main(["--db", str(db_path), "--apply"]) == 0
    conn = _conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0
    conn.close()


def test_verify_cli_clean_db_exits_zero(db_path, capsys):
    assert verify.main(["--db", str(db_path)]) == 0
    assert "Clean" in capsys.readouterr().out


# ── sweep_orphan_covers ──────────────────────────────────────────────────────────
def test_books_by_subject_cli_routes_through_access_api(db_path, capsys):
    import json
    conn = _conn(db_path)
    s = conn.execute("INSERT INTO subject (name) VALUES ('Buddhism/Tantra')").lastrowid
    e = conn.execute("INSERT INTO edition (title) VALUES ('Kalachakra')").lastrowid
    conn.execute("INSERT INTO edition_subject (edition_id, subject_id) VALUES (?, ?)", (e, s))
    p = conn.execute("INSERT INTO person (primary_name) VALUES ('Tsongkhapa')").lastrowid
    conn.execute("INSERT INTO edition_author (edition_id, person_id, seq) VALUES (?, ?, 1)", (e, p))
    conn.execute("INSERT INTO holding (edition_id, form, file_path) "
                 "VALUES (?, 'electronic', '/lib/k.pdf')", (e,))
    conn.commit(); conn.close()

    assert books_by_subject.main([str(db_path), "--subject", "Buddhism", "--json"]) == 0
    recs = json.loads(capsys.readouterr().out)
    assert len(recs) == 1 and recs[0]["title"] == "Kalachakra"
    assert recs[0]["authors"] == ["Tsongkhapa"] and len(recs[0]["holdings"]) == 1
    # author filter + a no-match subject (exit 1, message on stderr)
    assert books_by_subject.main([str(db_path), "--subject", "Buddhism", "--author", "Tsongkhapa"]) == 0
    assert books_by_subject.main([str(db_path), "--subject", "Nope"]) == 1
    assert "No subject matches" in capsys.readouterr().err


def test_sweep_orphan_covers_cli_trashes_orphan(db_path, tmp_path, capsys):
    cache = tmp_path / "cover-cache"; cache.mkdir()
    (cache / "e4242.jpg").write_bytes(b"\xff\xd8\xff")          # no edition 4242 → orphan
    sweep_orphan_covers.main(["--db", str(db_path), "--cache", str(cache), "--apply"])
    assert "Trashed 1" in capsys.readouterr().out
    assert not (cache / "e4242.jpg").exists()                   # moved to .trash/
