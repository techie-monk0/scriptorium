"""System tests — extraction cascade + LLM escalation (§4.7, §4.9).

The Step-4 orchestrator's public Python entry is
`catalogue.process.process_holding`. Assertions observe via the returned
`ProcessReport` and via the review-queue HTTP surface.

Plan invariants:
  - "Extraction cascade: 1) Structured outline; 2) Vision-LLM on TOC
    images; 3) No/unreadable text layer → metadata-only; queued for
    digitization." (§4.7)
  - "Per-entry escalation ladder qwen3:8b → qwen3:14b → Claude Haiku.
    Each rung's result is cached; the escalation logic checks the cache
    first so settled entries never re-climb the ladder and never re-bill."
    (§4.9)
  - "Haiku is the top rung (not a larger Claude — the task doesn't need
    it). $20 cap applies only to a raw API key." (§4.9)
"""
from __future__ import annotations

from pathlib import Path

from catalogue.services.classify import Rung
from catalogue.db_store import init_db
from catalogue.services.llm import BudgetTracker, LLMClient
from catalogue.services.process import ProcessConfig, process_holding
from catalogue.services.sweep import SweepConfig, sweep

from .conftest import make_epub


# ── Helpers via the public sweep entry point (no SELECT) ─────────────────
def _build_holding_via_sweep(tmp_path: Path, *, body_html: str) -> tuple:
    mount = tmp_path / "mount"
    mount.mkdir()
    make_epub(mount / "book.epub", [body_html])
    conn = init_db(tmp_path / "cat.db")
    sweep(conn, SweepConfig(mount_root=mount))
    return conn, mount


def _fake_transport(content: str):
    def _t(url, body, timeout):
        return {"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    return _t


def _ladder_returning(content: str, *, budget: BudgetTracker) -> list[Rung]:
    return [Rung("qwen3:8b",
                 LLMClient(model="qwen3:8b", budget=budget,
                           transport=_fake_transport(content)))]


# ── §4.7 cascade ─────────────────────────────────────────────────────────
def test_holding_with_outline_runs_full_cascade(tmp_path):
    conn, _ = _build_holding_via_sweep(
        tmp_path,
        body_html="<h1>The Way of the Bodhisattva</h1>"
                  "<h1>Translator's Introduction</h1>"
                  "<h1>Commentary by Patrul Rinpoche</h1>"
                  "<h1>Appendix A</h1>",
    )
    budget = BudgetTracker(billing_path="local")
    try:
        report = process_holding(
            conn, 1,
            ProcessConfig(
                ladder=_ladder_returning(
                    '{"kind":"root","confidence":0.9}', budget=budget,
                ),
            ),
        )
        assert report.extracted_entries == 4
        assert report.queued_for_digitization is False
        assert len(report.classifications) == 4
    finally:
        conn.close()


def test_holding_without_outline_queues_for_digitization(tmp_path, app_env):
    """§4.7 step 3: no/unreadable text layer → queue. Observable via
    /review."""
    c, app, _ = app_env
    mount = tmp_path / "mount"
    mount.mkdir()
    make_epub(mount / "bookless.epub", ["<p>prose only, no headings</p>"])

    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    try:
        sweep(conn, SweepConfig(mount_root=mount))
        process_holding(conn, 1, ProcessConfig())
    finally:
        conn.close()

    review = c.get("/review-queue")
    assert b"low_confidence_extraction" in review.data


# ── §4.9 escalation: cache-first, no re-bill on re-run ───────────────────
def test_settled_entries_do_not_re_bill_on_reprocess(tmp_path):
    """§4.9: 'settled entries never re-climb the ladder and never re-bill.'
    Observable as: budget.spent stays flat across re-runs."""
    conn, _ = _build_holding_via_sweep(
        tmp_path,
        body_html="<h1>A</h1><h1>B</h1><h1>C</h1>",
    )
    budget = BudgetTracker(billing_path="api_key", cap_usd=100.0)
    ladder_run_1 = [Rung(
        "claude-haiku-4-5-20251001",
        LLMClient(model="claude-haiku-4-5-20251001", budget=budget,
                  transport=_fake_transport('{"kind":"root","confidence":0.9}')),
    )]
    try:
        process_holding(conn, 1, ProcessConfig(ladder=ladder_run_1))
        spent_after_first = budget.spent_usd
        assert spent_after_first > 0

        # Re-run with a transport that would crash on ANY call.
        def boom(url, body, timeout):
            raise AssertionError("LLM must not be called on cached re-run")

        ladder_run_2 = [Rung(
            "claude-haiku-4-5-20251001",
            LLMClient(model="claude-haiku-4-5-20251001", budget=budget,
                      transport=boom),
        )]
        process_holding(conn, 1, ProcessConfig(ladder=ladder_run_2))
        assert budget.spent_usd == spent_after_first
    finally:
        conn.close()


# ── §4.9 billing: $20 cap applies ONLY to raw API key ────────────────────
def test_local_billing_does_not_enforce_cap(tmp_path):
    """§4.9: local Qwen is $0, Max-5x credit is tracked elsewhere, and
    the $20 cap is for the raw-API-key path only."""
    conn, _ = _build_holding_via_sweep(
        tmp_path, body_html="<h1>A</h1><h1>B</h1><h1>C</h1>",
    )
    # cap=0 would block immediately under api_key, but we're 'local'.
    budget = BudgetTracker(billing_path="local", cap_usd=0.0)
    try:
        report = process_holding(
            conn, 1,
            ProcessConfig(
                ladder=_ladder_returning(
                    '{"kind":"root","confidence":0.9}', budget=budget,
                ),
            ),
        )
        assert len(report.classifications) == 3
    finally:
        conn.close()


def test_api_key_billing_path_halts_at_cap(tmp_path):
    """§4.9: 'a running cost counter halts LLM calls before exceeding $20.'
    Observable as: process_holding raises BudgetExceeded on the api_key path."""
    import pytest
    from catalogue.services.llm import BudgetExceeded

    conn, _ = _build_holding_via_sweep(
        tmp_path, body_html="<h1>A</h1><h1>B</h1><h1>C</h1>",
    )
    budget = BudgetTracker(billing_path="api_key", cap_usd=0.000001)

    def big_transport(url, body, timeout):
        return {
            "choices": [{"message": {"content": '{"kind":"root","confidence":0.9}'}}],
            "usage": {"prompt_tokens": 10_000, "completion_tokens": 10_000},
        }

    ladder = [Rung("claude-haiku-4-5-20251001",
                   LLMClient(model="claude-haiku-4-5-20251001", budget=budget,
                             transport=big_transport))]
    try:
        with pytest.raises(BudgetExceeded):
            process_holding(conn, 1, ProcessConfig(ladder=ladder))
    finally:
        conn.close()
