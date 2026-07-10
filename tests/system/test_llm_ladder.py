"""§4.9 LLM ladder — system-level checks for fixes 1-3.

1. Anthropic transport sends `Authorization: Bearer …` for the
   OpenAI-compat `/chat/completions` endpoint — NOT the native-Messages
   `x-api-key` / `anthropic-version` pair, which would 401.
2. Haiku 4.5 rates are the published $1 in / $5 out per MTok, so the
   $20 cap math is correct on the raw-API-key billing path.
3. Default ladder's Haiku rung uses the dated model ID
   `claude-haiku-4-5-20251001` (Anthropic returns 404 for the bare
   `claude-haiku-4-5`).

These exercise the public surface (`default_ladder`, `classify_entry`,
`_default_transport`) without hitting the network — the urllib opener
is monkey-patched so we can read the outgoing headers.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import urllib.request
from contextlib import contextmanager

from catalogue.services.classify import default_ladder, classify_entry, Rung
from catalogue.db_store import init_db
from catalogue.services.llm import BudgetTracker, LLMClient, _auth_headers, _default_transport


# ── 1. Anthropic auth header ─────────────────────────────────────────────────

def test_anthropic_url_uses_bearer_not_xapikey():
    """OpenAI-compat endpoint accepts Bearer; native headers 401."""
    h = _auth_headers(
        "https://api.anthropic.com/v1/chat/completions",
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    assert h == {"Authorization": "Bearer sk-test"}
    # Critical regression guards — these were the bug.
    assert "x-api-key" not in h
    assert "anthropic-version" not in h


def test_non_anthropic_url_sends_no_auth():
    """Ollama and other local backends never get an Authorization header."""
    h = _auth_headers(
        "http://localhost:11434/v1/chat/completions",
        env={"ANTHROPIC_API_KEY": "sk-test"},  # set but irrelevant
    )
    assert h == {}


def test_default_transport_attaches_bearer_when_hitting_anthropic(monkeypatch):
    """End-to-end through `_default_transport`: capture the real urllib
    Request and assert what would go on the wire."""
    captured = {}

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"choices":[{"message":{"content":"{}"}}],"usage":{}}'

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return _FakeResp()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live-test")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    _default_transport(
        "https://api.anthropic.com/v1/chat/completions",
        {"model": "claude-haiku-4-5-20251001", "messages": []},
        timeout=5.0,
    )

    # urllib normalizes header capitalization to Title-Case.
    assert captured["headers"].get("Authorization") == "Bearer sk-live-test"
    assert "X-api-key" not in captured["headers"]
    assert "Anthropic-version" not in captured["headers"]


# ── 2. Haiku rate accuracy ───────────────────────────────────────────────────

def test_haiku_rate_matches_published_pricing():
    """$1/MTok in, $5/MTok out → 0.001 / 0.005 per 1K. A 1M-in / 200K-out
    call should cost $2.00 — anything else means the cap math is wrong."""
    b = BudgetTracker(billing_path="api_key", cap_usd=100.0)
    cost = b.estimate("claude-haiku-4-5-20251001", 1_000_000, 200_000)
    assert abs(cost - 2.00) < 1e-9


def test_budget_cap_enforced_with_corrected_rate():
    """Under the new rate, ~20M-in tokens should exceed a $20 cap."""
    b = BudgetTracker(billing_path="api_key", cap_usd=20.0)
    # 19M in / 0 out = $19.00 — under cap, should record.
    b.record("claude-haiku-4-5-20251001", 19_000_000, 0)
    assert abs(b.spent_usd - 19.0) < 1e-9
    # Another 2M in pushes to $21 — must raise.
    import pytest
    from catalogue.services.llm import BudgetExceeded
    with pytest.raises(BudgetExceeded):
        b.record("claude-haiku-4-5-20251001", 2_000_000, 0)


# ── 3. Dated model ID in the default ladder ──────────────────────────────────

def test_default_ladder_uses_dated_haiku_id():
    rungs = default_ladder()
    names = [r.name for r in rungs]
    # Default local model is gemma3:12b (qwen3 & Gemma-4 dropped — reasoning models
    # return empty content over Ollama /v1, see default_ladder docstring §4.9).
    assert names == ["gemma3:12b", "claude-haiku-4-5-20251001"]
    # The model the client posts must match the rung name (so the rate
    # table lookup hits and Anthropic doesn't 404).
    assert rungs[-1].client.model == "claude-haiku-4-5-20251001"


def test_haiku_rung_skipped_when_no_api_key(monkeypatch):
    """`available()` gates the Haiku rung on ANTHROPIC_API_KEY presence —
    without a key, we never attempt the call (no 401 noise, no cost)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rungs = default_ladder()
    assert rungs[-1].available() is False


# ── End-to-end: ladder climb records the corrected rate ──────────────────────

def test_ladder_climb_to_haiku_records_real_cost(tmp_path, monkeypatch):
    """If 8B and 14B return low confidence, the run escalates to Haiku
    and bills under the corrected rate — not the old placeholder."""
    db_path = tmp_path / "ladder.db"
    init_db(db_path).close()
    db = sqlite3.connect(db_path)

    def _resp(content: str, *, tokens_in: int, tokens_out: int) -> dict:
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
        }

    low_conf = json.dumps({"kind": "other", "confidence": 0.1})
    high_conf = json.dumps({"kind": "root", "confidence": 0.95})

    calls = []

    def _make_transport(content):
        def _t(url, body, timeout):
            calls.append(body["model"])
            return _resp(content, tokens_in=1000, tokens_out=200)
        return _t

    budget = BudgetTracker(billing_path="api_key", cap_usd=20.0)
    rungs = [
        Rung("qwen3:8b",
             LLMClient(model="qwen3:8b", budget=budget,
                       transport=_make_transport(low_conf))),
        Rung("qwen3:14b",
             LLMClient(model="qwen3:14b", budget=budget,
                       transport=_make_transport(low_conf))),
        Rung("claude-haiku-4-5-20251001",
             LLMClient(model="claude-haiku-4-5-20251001", budget=budget,
                       transport=_make_transport(high_conf))),
    ]

    result = classify_entry(db, "An Ambiguous Heading", ladder=rungs)
    assert result.rung == "claude-haiku-4-5-20251001"
    assert result.confidence == 0.95
    assert calls == ["qwen3:8b", "qwen3:14b", "claude-haiku-4-5-20251001"]
    # Local rungs $0; Haiku 1000-in + 200-out = .001 + .001 = $0.002.
    assert abs(budget.spent_usd - 0.002) < 1e-9
    db.close()
