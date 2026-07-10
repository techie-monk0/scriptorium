"""Regressions for the C1–C4 / H1–H5 / M2 / M7 fixes from the plan code
review. Each test is named for the finding it locks in."""
from __future__ import annotations

import sqlite3
import threading

import pytest


# ── C1: contract status code is 'raw', not 'pending' ──────────────────────
def test_c1_capture_inserts_status_raw(app_env):
    """§14.5: open capture rows use status='raw'. Older DBs that already
    contained 'pending' are migrated by db._migrate (covered separately)."""
    c, app, _ = app_env
    c.post("/capture", json={"isbn": "9780861711765", "source": "ios"})

    conn = sqlite3.connect(app.config["DB_PATH"])
    rows = conn.execute(
        "SELECT status FROM capture_staging WHERE raw_isbn = ?",
        ("9780861711765",),
    ).fetchall()
    conn.close()
    assert rows == [("raw",)]


# ── C2: concurrent POST /capture must not duplicate ───────────────────────
def test_c2_concurrent_capture_dedups_to_one_row(app_env):
    """Two threads POSTing the same ISBN race the SELECT-then-INSERT
    window. The partial unique index `capture_staging_raw_isbn_uq`
    serializes them; exactly one row survives."""
    c, app, _ = app_env
    isbn = "9780861711765"

    # Flask's test client is not thread-safe; instead drive the underlying
    # WSGI app from threads to provoke the race deterministically.
    results = []

    def hit():
        with app.test_client() as client:
            r = client.post("/capture", json={"isbn": isbn, "source": "ios"})
            results.append(r.get_json())

    threads = [threading.Thread(target=hit) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conn = sqlite3.connect(app.config["DB_PATH"])
    (n,) = conn.execute(
        "SELECT count(*) FROM capture_staging WHERE raw_isbn = ?", (isbn,)
    ).fetchone()
    conn.close()
    assert n == 1
    # All staging_ids returned must point at the same row.
    sids = {r["staging_id"] for r in results}
    assert len(sids) == 1


# ── C3: CSV invalid ISBN counted once, not staged ─────────────────────────
def test_c3_csv_invalid_not_double_counted(app_env):
    c, app, _ = app_env
    app.config["ISBN_LOOKUP"] = lambda _i: None
    r = c.post(
        "/capture/import",
        data={"lines": "9780861711765\n9780205309022\n"},  # one valid, one bad
        headers={"X-Requested-With": "shortcut"},
    )
    body = r.get_json()
    assert body["scanned"] == 2
    assert body["imported"] == 1
    assert body["invalid"] == 1   # NOT 2 (the bug double-counted)

    conn = sqlite3.connect(app.config["DB_PATH"])
    (n,) = conn.execute("SELECT count(*) FROM capture_staging").fetchone()
    conn.close()
    assert n == 1                  # the bad row must NOT have been staged


# ── C4: batch endpoint returns 422 when every item is invalid ─────────────
def test_c4_batch_all_invalid_returns_422(app_env):
    c, _, _ = app_env
    r = c.post(
        "/capture/batch",
        json={"scans": [
            {"isbn": "9780205309022", "source": "ios"},  # bad checksum
            {"isbn": "ABC", "source": "ios"},            # bad format
        ]},
    )
    assert r.status_code == 422
    results = r.get_json()["results"]
    assert all(x["status"] == "invalid" for x in results)


def test_c4_batch_mixed_still_201(app_env):
    """Regression guard: existing per-item-results-in-order behaviour
    must keep 201 when at least one scan succeeds."""
    c, _, _ = app_env
    r = c.post(
        "/capture/batch",
        json={"scans": [
            {"isbn": "9780861711765", "source": "ios"},
            {"isbn": "9780205309022", "source": "ios"},   # bad
        ]},
    )
    assert r.status_code == 201


# ── H2: FTS5 query syntax in user input must not 500 ──────────────────────
@pytest.mark.parametrize("q", ['"', 'foo AND bar', '(', '*', 'a:b', '"unterminated'])
def test_h2_fts_query_syntax_does_not_500(app_env, q):
    """User input is wrapped as an FTS5 phrase before MATCH; the parser
    never sees raw operator characters."""
    c, _, _ = app_env
    # The FTS5 MATCH now runs behind /api/v1/content (the converged content search).
    # Operator characters in user input must never 500 — they're phrase-wrapped.
    r = c.get("/api/v1/content", query_string={"q": q})
    assert r.status_code == 200
    assert c.get("/search", query_string={"q": q}).status_code == 200   # shell still loads


# ── H3: Hit.score is higher=better ────────────────────────────────────────
def test_h3_search_hit_score_higher_is_better(app_env):
    """Insert two editions; the term-matching one must have the larger
    `Hit.score`. Bare `bm25()` is smaller=better — we negate so callers
    sorting `score` descending stay correct."""
    c, app, _ = app_env
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO edition (id, title) VALUES (1, 'A')")
    conn.execute("INSERT INTO edition (id, title) VALUES (2, 'B')")
    conn.execute(
        "INSERT INTO edition_text (edition_id, page, content) "
        "VALUES (1, 1, 'tathāgatagarbha doctrine in the Uttaratantra')"
    )
    conn.execute(
        "INSERT INTO edition_text (edition_id, page, content) "
        "VALUES (2, 1, 'unrelated text about something else entirely')"
    )
    conn.commit()
    conn.close()

    from catalogue.services.search import SearchService
    from catalogue.db_store import connect as db_connect
    svc = SearchService()
    db = db_connect(app.config["DB_PATH"])
    try:
        hits = svc.search(db, "tathagatagarbha")
    finally:
        db.close()
    assert hits
    assert hits[0].edition_id == 1
    # And the score is positive (negated bm25 of a hit).
    assert hits[0].score > 0


# ── M2: budget gate is pre-call, not post-call ────────────────────────────
def test_m2_budget_halts_before_network_call():
    """§13: USD-20 cap halts BEFORE the LLM call — the very call that
    would overrun must not reach the transport."""
    from catalogue.services.llm import BudgetExceeded, BudgetTracker, LLMClient

    calls: list = []

    def fake_transport(url, body, timeout):
        calls.append(url)
        return {"choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    # Force the api_key billing path with an absurdly low cap so even one
    # call's worst-case estimate exceeds it.
    bt = BudgetTracker(cap_usd=0.0000001, billing_path="api_key")
    client = LLMClient(
        model="claude-haiku-4-5-20251001",
        base_url="https://api.anthropic.com/v1",
        budget=bt,
        transport=fake_transport,
    )
    with pytest.raises(BudgetExceeded):
        client.chat([{"role": "user", "content": "x" * 200}], max_tokens=512)
    assert calls == []   # transport never invoked


def test_m2_budget_local_path_does_not_gate():
    """Local Qwen rung must not be capped — it's $0."""
    from catalogue.services.llm import BudgetTracker, LLMClient

    def fake_transport(url, body, timeout):
        return {"choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    bt = BudgetTracker(cap_usd=0.0, billing_path="local")
    client = LLMClient(budget=bt, transport=fake_transport)
    out = client.chat([{"role": "user", "content": "hi"}], max_tokens=8)
    assert out["model"] == "qwen3:8b"


# ── M7: ON DELETE CASCADE on join tables ──────────────────────────────────
def test_m7_deleting_work_cascades_to_join_tables(app_env, seed):
    """Pruning a Work used to leave orphan rows in work_alias /
    relationship / work_subject etc. ON DELETE CASCADE handles it now."""
    _, app, _ = app_env

    # Seed: one Work with an alias, a subject link, and a self-relationship.
    seed("INSERT INTO work (id) VALUES (1)")
    seed("INSERT INTO work (id) VALUES (2)")
    seed(
        "INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
        "VALUES (1, 'Bodhicaryāvatāra', 'iast', 'bodicaryavatara')"
    )
    seed("INSERT INTO subject (id, name) VALUES (1, 'Madhyamaka')")
    seed("INSERT INTO work_subject (work_id, subject_id) VALUES (1, 1)")
    seed(
        "INSERT INTO relationship (from_work_id, relation, to_work_id) "
        "VALUES (1, 'comments_on', 2)"
    )

    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM work WHERE id = 1")
    conn.commit()

    rows = lambda sql: conn.execute(sql).fetchall()
    assert rows("SELECT id FROM work_alias WHERE work_id = 1") == []
    assert rows("SELECT * FROM work_subject WHERE work_id = 1") == []
    # relationship cascades for BOTH from_work_id and to_work_id.
    assert rows("SELECT id FROM relationship WHERE from_work_id = 1 "
                "OR to_work_id = 1") == []
    # The PRAGMA foreign_key_check should be clean.
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_m7_deleting_edition_cascades_to_holding_and_edition_text(app_env, seed):
    _, app, _ = app_env
    seed("INSERT INTO edition (id, title) VALUES (1, 't')")
    seed("INSERT INTO holding (edition_id, form) VALUES (1, 'electronic')")
    seed(
        "INSERT INTO edition_text (edition_id, page, content) "
        "VALUES (1, 1, 'text')"
    )

    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM edition WHERE id = 1")
    conn.commit()
    (h,) = conn.execute("SELECT count(*) FROM holding").fetchone()
    (t,) = conn.execute("SELECT count(*) FROM edition_text").fetchone()
    conn.close()
    assert h == 0 and t == 0
