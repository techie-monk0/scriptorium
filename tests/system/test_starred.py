"""Starred-editions API (/api/v1/starred) — server side.

Exercises GET/POST/DELETE across the HTTP surface (the same target web/PWA/iOS hit), the ETag/304
cache, idempotent re-star, the NotFound guard on a missing edition, and that each write returns the
fresh list. Seeds editions via direct SQL (Arrange); Act/Assert go through HTTP.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def seeded(app_env, seed):
    """app_env with two live editions to star."""
    seed("INSERT INTO edition (id, title) VALUES (1, 'One'), (2, 'Two')")
    return app_env


def test_star_then_list(seeded):
    c, _, _ = seeded
    r = c.post("/api/v1/starred", json={"edition_id": 2})
    assert r.status_code == 201
    assert r.get_json()["editions"] == [2]
    assert c.get("/api/v1/starred").get_json()["editions"] == [2]


def test_star_is_idempotent(seeded):
    c, _, _ = seeded
    c.post("/api/v1/starred", json={"edition_id": 1})
    r = c.post("/api/v1/starred", json={"edition_id": 1})       # re-star — still one entry
    assert r.status_code == 201
    assert r.get_json()["editions"] == [1]


def test_unstar(seeded):
    c, _, _ = seeded
    c.post("/api/v1/starred", json={"edition_id": 1})
    r = c.delete("/api/v1/starred/1")
    assert r.status_code == 200
    assert r.get_json()["editions"] == []


def test_star_missing_edition_is_404(seeded):
    c, _, _ = seeded
    r = c.post("/api/v1/starred", json={"edition_id": 999})
    assert r.status_code == 404


def test_bad_body_is_422(seeded):
    c, _, _ = seeded
    assert c.post("/api/v1/starred", json={}).status_code == 422
    assert c.post("/api/v1/starred", json={"edition_id": "x"}).status_code == 422


def test_etag_304(seeded):
    c, _, _ = seeded
    c.post("/api/v1/starred", json={"edition_id": 1})
    r1 = c.get("/api/v1/starred")
    etag = r1.headers["ETag"]
    r2 = c.get("/api/v1/starred", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    # A new star changes the fingerprint → the old ETag no longer matches.
    c.post("/api/v1/starred", json={"edition_id": 2})
    assert c.get("/api/v1/starred", headers={"If-None-Match": etag}).status_code == 200
