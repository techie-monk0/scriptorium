"""System tests — review-queue workflow (§3, §4.8d, §7.3).

Plan invariants verified through HTTP:
  - "OCR handling: existing OCR mostly good → use as-is; score quality;
    selective re-OCR of the low-quality minority." (§3, §4.8d)
  - "Failures → review queue. ... low-quality OCR — payload_json."
    (§5, §6)
  - The OCR-quality override available from the review UI is the user's
    explicit escape hatch: an entry the heuristic flagged as `ocr_poor`
    can be marked `ocr_good` and removed from the needs-work list.
"""
from __future__ import annotations

import json


def _enqueue(seed, item_type: str, payload: dict) -> int:
    cur = seed(
        "INSERT INTO review_queue (item_type, payload_json) VALUES (?, ?)",
        (item_type, json.dumps(payload)),
    )
    return cur.lastrowid


def test_pending_review_items_appear_on_dashboard_and_list(app_env, seed):
    c, _, _ = app_env
    _enqueue(seed, "low_quality_ocr", {"score": 0.2})
    _enqueue(seed, "alias_merge", {"a": "x"})

    home = c.get("/")
    assert home.status_code == 200
    # The nav is manifest-driven (labels applied client-side from LibraryCore.APP_SECTIONS); the
    # Review entry point's href is what's server-rendered.
    assert b"/review" in home.data           # the hub surfaces a Review entry point

    listing = c.get("/review-queue")
    assert b"low_quality_ocr" in listing.data
    assert b"alias_merge" in listing.data


def test_resolve_removes_item_from_pending_list(app_env, seed):
    c, _, _ = app_env
    iid = _enqueue(seed, "alias_merge", {})

    pending_before = c.get("/review-queue").data
    assert f'href="/review-queue/{iid}"'.encode() in pending_before

    c.post(f"/review-queue/{iid}/resolve", data={"action": "resolve"})

    pending_after = c.get("/review-queue").data
    assert f'href="/review-queue/{iid}"'.encode() not in pending_after

    # And it shows up under the "resolved" filter.
    resolved = c.get("/review-queue?status=resolved").data
    assert f'href="/review-queue/{iid}"'.encode() in resolved


def test_ocr_override_flips_holding_text_status(app_env, seed):
    """The end-to-end goal: a holding flagged as ocr_poor, when overridden via
    the review UI, is promoted to ocr_good. (The /holdings + /needs-work UI
    surfaces were removed in the dashboard redesign — see DELETIONS.md — so the
    effect is observed on the holding row the override writes.)"""
    import sqlite3
    c, _, _ = app_env
    # A holding the sweep would have marked ocr_poor with a known hash.
    seed("INSERT INTO edition (id, title) VALUES (1, 'Marginal Quality')")
    seed(
        "INSERT INTO holding (edition_id, form, file_hash, text_status, "
        "ocr_quality_score) VALUES (1, 'electronic', 'abc', 'ocr_poor', 0.35)"
    )
    iid = _enqueue(seed, "low_quality_ocr",
                   {"file_hash": "abc", "score": 0.35})

    def status():
        conn = sqlite3.connect(c.application.config["DB_PATH"])
        try:
            return conn.execute(
                "SELECT text_status FROM holding WHERE file_hash='abc'").fetchone()[0]
        finally:
            conn.close()

    assert status() == "ocr_poor"
    c.post(f"/review-queue/{iid}/resolve", data={"action": "ocr_override"})
    assert status() == "ocr_good"


def test_double_resolve_does_not_break_the_flow(app_env, seed):
    """Plan: actions should be idempotent. A second resolve click must
    not 500."""
    c, _, _ = app_env
    iid = _enqueue(seed, "alias_merge", {})
    r1 = c.post(f"/review-queue/{iid}/resolve", data={"action": "resolve"})
    r2 = c.post(f"/review-queue/{iid}/resolve", data={"action": "resolve"})
    assert r1.status_code in (200, 302, 303)
    assert r2.status_code in (200, 302, 303)
