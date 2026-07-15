"""The "Ask" backend seam — the grounded-Q&A service the web/PWA "Ask" panel calls.

`AskBackend` is the abstract interface the `/api/v1/ask` route talks to; the concrete
`OpenAIProxyBackend` below it speaks OpenAI chat-completions to the BuddhistLLM RAG proxy.
Keeping this behind an interface means the catalogue never hard-codes the model host — swap
the implementation (a different RAG service, or a stub in tests) without touching the route
or the frontend.

The backend calls a SEPARATE process server-side (loopback / Tailscale), so the browser only
ever talks same-origin to `/api/v1/ask`. The model host therefore needs no CORS and is never
exposed publicly.

Technical details
-----------------
The proxy answers OpenAI `POST /v1/chat/completions`. The answer text is the standard
`choices[0].message.content` (markdown). Citations and per-stage timing arrive as two
ADDITIVE, non-standard top-level fields the proxy adds — `sources` (a list of
`{n, title, edition_pub_id, location, file}`) and `timing` — which a plain OpenAI client
simply ignores. `messages` is forwarded verbatim (full history) because the proxy's own
multi-turn state (its `/source` scoping) lives in the chat history, not on the server.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod


class AskUnavailable(RuntimeError):
    """The backend is unreachable or erroring. The route turns this into a soft failure so the
    UI can say "Ask is offline" rather than 500-ing."""


class AskBackend(ABC):
    """A grounded-answer service. `messages` is the OpenAI chat array — pass the FULL history
    so the backend's own multi-turn state (e.g. BuddhistLLM's `/source` scoping) survives."""

    @abstractmethod
    def models(self) -> list[dict]:
        """Advertised models as `[{id, label}]`. Raises `AskUnavailable` if unreachable."""
        raise NotImplementedError

    @abstractmethod
    def ask(self, model: str, messages: list[dict]) -> dict:
        """One grounded turn → `{model, content, sources, timing}`:
          - `content` — markdown answer,
          - `sources` — `[{n, title, edition_pub_id, location, file}]` (may be `[]`),
          - `timing`  — `{retrieve_ms, rerank_ms, generate_ms, total_ms, cached}` (may be `{}`).
        Raises `AskUnavailable` on any transport/backend failure."""
        raise NotImplementedError


# ── implementation ──────────────────────────────────────────────────────────────
class OpenAIProxyBackend(AskBackend):
    """Talks OpenAI `/chat/completions` to the BuddhistLLM RAG proxy. Reads the standard
    `choices[0].message.content` for the answer plus the proxy's additive `sources`/`timing`
    fields (absent → treated as empty)."""

    def __init__(self, base_url: str, *, timeout: float = 1800.0):
        self.base = base_url.rstrip("/")           # e.g. http://127.0.0.1:7070/v1
        self.timeout = timeout

    def _get(self, path: str):
        req = urllib.request.Request(self.base + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AskUnavailable(f"ask backend unreachable: {e}") from e

    def _post(self, path: str, body: dict):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AskUnavailable(f"ask backend error: {e}") from e

    def models(self) -> list[dict]:
        doc = self._get("/models")
        return [{"id": m.get("id"), "label": m.get("id")} for m in doc.get("data", [])]

    def ask(self, model: str, messages: list[dict]) -> dict:
        doc = self._post("/chat/completions",
                         {"model": model, "messages": messages, "stream": False})
        choice = (doc.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content", "")
        return {"model": doc.get("model", model), "content": content,
                "sources": doc.get("sources", []), "timing": doc.get("timing", {})}
