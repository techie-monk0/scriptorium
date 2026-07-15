"""Black-box tests for the "Ask" web surface — the grounded-Q&A page + its JSON contract.

The panel (LibraryUI.ask) talks ONLY to `/api/v1/ask` and `/api/v1/ask/models`, which proxy to
a configured `AskBackend`. These tests swap in a stub backend (the whole point of the interface),
so they exercise the route contract — shape, source-link seam, full-history forwarding, and the
soft-failure behaviour — without a live model server.
"""
from __future__ import annotations

from catalogue.webui.ask import AskBackend, AskUnavailable


class StubBackend(AskBackend):
    """In-memory AskBackend. Records the last call so tests can assert what got forwarded."""

    def __init__(self, *, models=None, reply=None, fail=False):
        self._models = models if models is not None else [{"id": "library-fast", "label": "Fast"}]
        self._reply = reply or {}
        self._fail = fail
        self.last = None

    def models(self):
        if self._fail:
            raise AskUnavailable("backend down")
        return self._models

    def ask(self, model, messages):
        if self._fail:
            raise AskUnavailable("backend down")
        self.last = {"model": model, "messages": messages}
        return {"model": model, "content": self._reply.get("content", ""),
                "sources": self._reply.get("sources", []), "timing": self._reply.get("timing", {})}


def test_ask_page_mounts_component(app_env):
    """The /ask page extends the shared shell and mounts the shared component on the web adapter."""
    c, _, _ = app_env
    r = c.get("/ask")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "LibraryUI.ask(" in body and 'id="ask-host"' in body
    assert "LibraryWeb.adapter()" in body
    assert 'id="nav-host"' in body                      # inherits the shared section nav


def test_ask_models_lists_backend_models(app_env):
    c, app, _ = app_env
    app.config["ASK_BACKEND"] = StubBackend(
        models=[{"id": "library-fast", "label": "Fast"}, {"id": "library-deep", "label": "Deep"}])
    doc = c.get("/api/v1/ask/models").get_json()
    assert doc["available"] is True
    assert [m["id"] for m in doc["models"]] == ["library-fast", "library-deep"]


def test_ask_turn_shape_and_source_seam(app_env):
    """One turn returns {available, model, content, sources, timing}. Each source keeps its durable
    edition_pub_id and carries an `eid` deep-link slot (None until ctx.edition_id_for_pub is wired)."""
    c, app, _ = app_env
    stub = StubBackend(reply={
        "content": "The three poisons are attachment, aversion, and ignorance [1].",
        "sources": [{"n": 1, "title": "Liberation in Our Hands", "edition_pub_id": "pub-xyz",
                     "location": "vol.1 · p.64", "file": "lioh.pdf"}],
        "timing": {"retrieve_ms": 10.0, "rerank_ms": 20.0, "generate_ms": 30.0,
                   "total_ms": 60.0, "cached": False}})
    app.config["ASK_BACKEND"] = stub
    doc = c.post("/api/v1/ask", json={
        "model": "library-fast",
        "messages": [{"role": "user", "content": "what are the three poisons"}]}).get_json()
    assert doc["available"] is True and doc["model"] == "library-fast"
    assert "three poisons" in doc["content"]
    assert doc["timing"]["total_ms"] == 60.0 and doc["timing"]["cached"] is False
    src = doc["sources"][0]
    assert src["title"] == "Liberation in Our Hands" and src["edition_pub_id"] == "pub-xyz"
    assert "eid" in src and src["eid"] is None            # deep-link seam present, resolver unwired


def test_ask_forwards_full_history_verbatim(app_env):
    """Full history must reach the backend unchanged — the backend's /source scoping lives in it."""
    c, app, _ = app_env
    stub = StubBackend(reply={"content": "ok"})
    app.config["ASK_BACKEND"] = stub
    history = [
        {"role": "user", "content": "/source author:Nagarjuna :: what is emptiness"},
        {"role": "assistant", "content": "Which work? <!--BDRAG_SCOPE eyJhIjoxfQ==-->"},
        {"role": "user", "content": "1"},
    ]
    c.post("/api/v1/ask", json={"model": "library-deep", "messages": history})
    assert stub.last["model"] == "library-deep"
    assert stub.last["messages"] == history               # verbatim, incl. the hidden scope marker


def test_ask_backend_down_is_soft_failure(app_env):
    """A backend outage is a soft 502 (available:false), never a 500 — the panel says 'offline'."""
    c, app, _ = app_env
    app.config["ASK_BACKEND"] = StubBackend(fail=True)
    r = c.post("/api/v1/ask", json={
        "model": "library-fast", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502 and r.get_json()["available"] is False
    m = c.get("/api/v1/ask/models").get_json()
    assert m["available"] is False and m["models"] == []


def test_ask_empty_question_is_400(app_env):
    c, app, _ = app_env
    app.config["ASK_BACKEND"] = StubBackend()
    r = c.post("/api/v1/ask", json={"messages": []})
    assert r.status_code == 400


def test_ask_unconfigured_is_503(app_env):
    """With no backend configured (CATALOGUE_ASK_URL=""), the feature is off but never errors."""
    c, app, _ = app_env
    app.config["ASK_BACKEND"] = None
    r = c.post("/api/v1/ask", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and r.get_json()["available"] is False
    assert c.get("/api/v1/ask/models").get_json() == {"available": False, "models": []}
