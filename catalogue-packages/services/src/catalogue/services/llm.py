"""OpenAI-compatible LLM client + USD-20 budget tracker (§4.9, §6, §13).

All LLM calls go through `LLMClient.chat()` which speaks the OpenAI
`/v1/chat/completions` schema. Switching backends = a `base_url` swap, never
a code change. The transport is injectable so tests don't need Ollama.

The budget tracker only enforces the cap when configured for the raw
`ANTHROPIC_API_KEY` billing path; local Qwen via Ollama costs $0 and the
Max-5x programmatic credit is tracked separately (§4.9 — three buckets).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# Local (Ollama) model + endpoint, config-driven (vocab.json `_local_llm`).
_LOCAL_LLM_DEFAULT = {"model": "gemma3:12b", "base_url": "http://localhost:11434/v1"}


def local_llm_config() -> dict:
    """`{'model':…, 'base_url':…}` for the local model — from vocab.json `_local_llm`,
    falling back to the default. Lets the operator swap the local model/endpoint
    without a code change (§12.7)."""
    cfg = dict(_LOCAL_LLM_DEFAULT)
    try:
        from catalogue.db_store.db import VOCAB_PATH
        data = json.loads(VOCAB_PATH.read_text("utf-8"))
        for k, v in (data.get("_local_llm") or {}).items():
            if k in cfg and v:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def local_llm_client(**overrides):
    """An `LLMClient` for the configured local model (override `model`/`base_url`)."""
    cfg = {**local_llm_config(), **overrides}
    return LLMClient(model=cfg["model"], base_url=cfg["base_url"])


# External (cloud) model, config-driven (vocab.json `_external_llm`). Switch
# `provider` to pick Claude vs Gemini; both endpoints are OpenAI-compatible.
_EXTERNAL_PROVIDERS = {
    "claude": {"model": "claude-sonnet-4-6", "base_url": "https://api.anthropic.com/v1",
               "key_env": "ANTHROPIC_API_KEY"},
    "gemini": {"model": "gemini-2.0-flash",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_env": "GOOGLE_API_KEY"},
}


def external_llm_config() -> dict:
    """`{'provider', 'model', 'base_url', 'key_env'}` for the chosen cloud model —
    from vocab.json `_external_llm` (a `provider` selector + optional per-provider
    `model`/`base_url` overrides), defaulting to Claude."""
    provider = "claude"
    providers = {k: dict(v) for k, v in _EXTERNAL_PROVIDERS.items()}
    try:
        from catalogue.db_store.db import VOCAB_PATH
        data = json.loads(VOCAB_PATH.read_text("utf-8")).get("_external_llm") or {}
        provider = (data.get("provider") or provider).lower()
        for name, p in (data.get("providers") or {}).items():
            providers.setdefault(name.lower(), {}).update(p or {})
    except Exception:
        pass
    p = providers.get(provider) or _EXTERNAL_PROVIDERS["claude"]
    return {"provider": provider, "model": p.get("model"),
            "base_url": p.get("base_url"),
            "key_env": p.get("key_env", "ANTHROPIC_API_KEY")}


# ── Per-model rates (USD per 1K tokens; in/out). ──────────────────────────
# Used only for the raw-API-key billing path. Local rungs MUST be 0.
_RATES = {
    "qwen3:8b":                  (0.0,    0.0),
    "qwen3:14b":                 (0.0,    0.0),
    # Claude Haiku 4.5: $1 / MTok in, $5 / MTok out → per-1K below.
    "claude-haiku-4-5-20251001": (0.001,  0.005),
}


class BudgetExceeded(RuntimeError):
    """Raised when an LLM call would push spend past the configured cap.
    Per §4.9 / §13, this cap applies ONLY to the raw-API-key path."""


@dataclass
class BudgetTracker:
    """Running spend counter. Cheap to construct; share one per app run."""
    cap_usd: float = 20.0
    billing_path: str = "local"     # 'local' | 'max_credit' | 'api_key'
    spent_usd: float = 0.0

    def estimate(self, model: str, tokens_in: int, tokens_out: int) -> float:
        rate_in, rate_out = _RATES.get(model, (0.0, 0.0))
        return (tokens_in / 1000.0) * rate_in + (tokens_out / 1000.0) * rate_out

    def reserve(self, model: str, tokens_in: int, tokens_out: int) -> None:
        """Pre-call gate (§13: cap 'halts BEFORE overrun'). Raises
        `BudgetExceeded` if the worst-case cost would push spend past the
        cap. No-op on local + max-credit paths.

        `tokens_in` is the prompt-token estimate; `tokens_out` should be
        `max_tokens` from the upcoming chat call so we charge the
        worst-case before the network round-trip rather than after.
        """
        if self.billing_path != "api_key":
            return
        cost = self.estimate(model, tokens_in, tokens_out)
        if self.spent_usd + cost > self.cap_usd:
            raise BudgetExceeded(
                f"would spend {self.spent_usd + cost:.4f} > cap {self.cap_usd}"
            )

    def record(self, model: str, tokens_in: int, tokens_out: int) -> float:
        cost = self.estimate(model, tokens_in, tokens_out)
        # Defensive backstop. `reserve()` is the primary gate; this
        # catches callers that skipped it.
        if self.billing_path == "api_key" and self.spent_usd + cost > self.cap_usd:
            raise BudgetExceeded(
                f"would spend {self.spent_usd + cost:.4f} > cap {self.cap_usd}"
            )
        self.spent_usd += cost
        return cost


# ── Transport: a callable that POSTs JSON, returns parsed dict ────────────
TransportFn = Callable[[str, dict, float], dict]   # (url, body, timeout) → JSON


def _auth_headers(url: str, env: dict | None = None) -> dict[str, str]:
    """Pick the right auth headers for `url`. Extracted so a test can
    verify we send Anthropic's OpenAI-compat `Authorization: Bearer …`
    rather than the native-Messages `x-api-key` / `anthropic-version`
    pair, which 401s against `/v1/chat/completions`.

    Keys come from env, never hardcoded (§13)."""
    env = env if env is not None else os.environ
    if "anthropic" in url:
        key = env.get("ANTHROPIC_API_KEY")
        if key:
            # Anthropic exposes an OpenAI-compatible endpoint at
            # `https://api.anthropic.com/v1/chat/completions` that accepts
            # Bearer auth. The native `/v1/messages` headers
            # (`x-api-key`, `anthropic-version`) do NOT work here.
            return {"Authorization": f"Bearer {key}"}
    if "googleapis" in url or "generativelanguage" in url:
        # Gemini's OpenAI-compat endpoint
        # (`generativelanguage.googleapis.com/v1beta/openai/chat/completions`)
        # takes Bearer auth with the Google AI Studio key.
        key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
        if key:
            return {"Authorization": f"Bearer {key}"}
    return {}


def _default_transport(url: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", **_auth_headers(url)}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


# ── Local-server lifecycle (Ollama) ───────────────────────────────────────
def _is_local(base_url: str) -> bool:
    return "localhost" in base_url or "127.0.0.1" in base_url


def _server_healthy(base_url: str, timeout: float = 2.0) -> bool:
    """True if an OpenAI-compatible server answers at base_url. Uses the
    cheap `/models` listing as a liveness probe."""
    url = f"{base_url.rstrip('/')}/models"
    try:
        urllib.request.urlopen(url, timeout=timeout)   # noqa: S310
        return True
    except Exception:
        return False


def ensure_ollama(base_url: str = "http://localhost:11434/v1", *,
                  keep_alive: str = "24h",
                  warm_model: Optional[str] = None,
                  start_timeout: float = 60.0,
                  log: Callable[[str], None] = print) -> bool:
    """Best-effort: make a local Ollama reachable at `base_url`, starting
    `ollama serve` if needed, then optionally warm `warm_model` so the first
    real call doesn't pay the cold-load (~75–90s for a fresh model on the M4).

    Call this ONCE at startup (before forking workers), not per request — the
    spawned daemon is detached so it outlives this process and keeps the model
    resident. No-op for a non-local `base_url`. Returns True if the server is
    reachable at the end.
    """
    if _server_healthy(base_url):
        log("ollama: already running")
    elif not _is_local(base_url):
        log(f"ollama: {base_url} unreachable and not local — start it yourself")
        return False
    else:
        exe = shutil.which("ollama")
        if not exe:
            log("ollama: 'ollama' not on PATH — install it or start the app")
            return False
        log(f"ollama: not running — starting `ollama serve` "
            f"(OLLAMA_KEEP_ALIVE={keep_alive}, OLLAMA_NUM_PARALLEL=1)")
        # Detached (own session) so the daemon survives this process exiting
        # and stays warm for later runs / the web app. NUM_PARALLEL=1 forces
        # the server to *serialize* requests: on a 24 GB M4 a ~8 GB model can't
        # hold several concurrent contexts, and trying to (e.g. parallel resolve
        # workers all hitting it) yields empty/truncated responses and is slower
        # overall. Serialized requests block briefly but come back correct.
        subprocess.Popen(
            [exe, "serve"],
            env={**os.environ, "OLLAMA_KEEP_ALIVE": keep_alive,
                 "OLLAMA_NUM_PARALLEL": "1"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        t0 = time.time()
        while time.time() - t0 < start_timeout:
            if _server_healthy(base_url):
                break
            time.sleep(1.0)
        else:
            log(f"ollama: serve did not come up within {start_timeout:.0f}s")
            return False
        log("ollama: server is up")

    if warm_model:
        log(f"ollama: warming {warm_model} (first load can take ~60–90s)…")
        t0 = time.time()
        try:
            LLMClient(model=warm_model, base_url=base_url).chat(
                [{"role": "user", "content": "ok"}],
                max_tokens=1, json_only=False)
            log(f"ollama: {warm_model} warm ({time.time() - t0:.0f}s)")
        except Exception as e:  # noqa: BLE001 — warmup is best-effort
            log(f"ollama: warmup failed ({e}) — continuing")
    return True


# ── Client ────────────────────────────────────────────────────────────────
@dataclass
class LLMClient:
    """One client per (base_url, model). Backend swap is a `base_url`
    change only (§4.9). Default points at local Ollama."""
    model: str = "qwen3:8b"
    base_url: str = "http://localhost:11434/v1"
    # A cold qwen3:8b on the 24 GB M4 needs ~75–90s for its first reply
    # (model load + generation), so the old 60s default timed out *every*
    # call → "no rungs ran". Generous default; tune via env. Subsequent
    # calls are fast once Ollama keeps the model warm.
    timeout: float = field(
        default_factory=lambda: float(os.environ.get("CATALOGUE_LLM_TIMEOUT", "180"))
    )
    budget: BudgetTracker = field(default_factory=BudgetTracker)
    transport: TransportFn = field(default=_default_transport)

    def chat(self, messages: list[dict], *,
             max_tokens: int = 512,
             json_only: bool = True) -> dict[str, Any]:
        """Returns `{content, tokens_in, tokens_out, model}`. Raises
        `BudgetExceeded` only when the configured billing path enforces it.
        Network/parse errors raise; callers decide whether to fall back."""
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,        # deterministic for caching to be useful
        }
        if json_only:
            body["response_format"] = {"type": "json_object"}
        url = f"{self.base_url.rstrip('/')}/chat/completions"

        # §13: USD-20 cap halts BEFORE the call, not after — otherwise the
        # very call that overruns is already billed. Worst-case estimate:
        # the full prompt char count / 4 chars-per-token plus `max_tokens`
        # for output. Cheap heuristic; only the raw-api-key path enforces.
        prompt_chars = sum(
            len(m.get("content", "")) for m in messages if isinstance(m, dict)
        )
        est_in = max(1, prompt_chars // 4)
        self.budget.reserve(self.model, est_in, max_tokens)

        try:
            resp = self.transport(url, body, self.timeout)
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM transport failed: {e}") from e

        # OpenAI-compatible response shape — Ollama, OpenAI, and the
        # Claude Anthropic-compat endpoint all return this.
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        usage = resp.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        self.budget.record(self.model, tokens_in, tokens_out)
        return {
            "content": content,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model": self.model,
        }
