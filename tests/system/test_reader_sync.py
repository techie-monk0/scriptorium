"""End-to-end tests for the offline-first reader-state sync (reader_module_plan.md Phase 2).

Exercises /sync/reader through the Flask test client: the pull/push protocol, the monotonic
rev cursor, last-write-wins conflict resolution, tombstone propagation, the holding CASCADE,
and the editor-only write gate. This is the foundation the bookmarks UI (Phase 3) and later
annotations ride on, so its merge semantics are pinned here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalogue.webui.web import create_app


def _holding(seed) -> int:
    eid = seed("INSERT INTO edition (title) VALUES ('Book')").lastrowid
    return seed(
        "INSERT INTO holding (edition_id, form, file_path, text_status) "
        "VALUES (?, 'electronic', '/x/book.pdf', 'ocr_good')", (eid,)).lastrowid


def _bm(c, **fields):
    """POST one bookmark op; return the JSON response."""
    op = {"type": "bookmark"}
    op.update(fields)
    return c.post("/sync/reader", json={"ops": [op]}).get_json()


# ── pull/push round-trip + cursor ────────────────────────────────────────────
def test_push_then_pull_returns_the_bookmark(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    res = _bm(c, id="bm-1", holding_id=hid, locator="42", fraction=0.5,
              label="A spot", created_at="2026-06-26T10:00:00Z", updated_at="2026-06-26T10:00:00Z")
    assert res["applied"] == [{"id": "bm-1", "rev": 1}]

    pull = c.get("/sync/reader?since=0").get_json()
    assert pull["rev"] == 1
    assert len(pull["bookmarks"]) == 1
    bm = pull["bookmarks"][0]
    assert bm["id"] == "bm-1" and bm["holding_id"] == hid
    assert bm["locator"] == "42" and bm["fraction"] == 0.5 and bm["label"] == "A spot"
    assert bm["deleted_at"] is None and bm["rev"] == 1


def test_cursor_advances_so_a_synced_client_gets_nothing_new(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    _bm(c, id="bm-1", holding_id=hid, locator="1", updated_at="2026-06-26T10:00:00Z")
    cur = c.get("/sync/reader?since=0").get_json()["rev"]
    # A client already at the high-water mark pulls nothing.
    again = c.get(f"/sync/reader?since={cur}").get_json()
    assert again["bookmarks"] == [] and again["rev"] == cur


def test_second_device_pulls_full_set_from_zero(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    _bm(c, id="bm-1", holding_id=hid, locator="1", updated_at="2026-06-26T10:00:00Z")
    _bm(c, id="bm-2", holding_id=hid, locator="2", updated_at="2026-06-26T10:01:00Z")
    pull = c.get("/sync/reader?since=0").get_json()
    assert {b["id"] for b in pull["bookmarks"]} == {"bm-1", "bm-2"}
    # rev is monotonic and ordered.
    revs = [b["rev"] for b in pull["bookmarks"]]
    assert revs == sorted(revs)


def test_only_rows_after_cursor_are_returned(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    _bm(c, id="bm-1", holding_id=hid, locator="1", updated_at="2026-06-26T10:00:00Z")
    mid = c.get("/sync/reader?since=0").get_json()["rev"]
    _bm(c, id="bm-2", holding_id=hid, locator="2", updated_at="2026-06-26T10:01:00Z")
    pull = c.get(f"/sync/reader?since={mid}").get_json()
    assert [b["id"] for b in pull["bookmarks"]] == ["bm-2"]


# ── last-write-wins conflict resolution ──────────────────────────────────────
def test_newer_edit_wins(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    _bm(c, id="bm-1", holding_id=hid, label="first", updated_at="2026-06-26T10:00:00Z")
    res = _bm(c, id="bm-1", holding_id=hid, label="second", updated_at="2026-06-26T11:00:00Z")
    assert res["applied"][0]["id"] == "bm-1" and "rev" in res["applied"][0]
    bms = c.get("/sync/reader?since=0").get_json()["bookmarks"]
    assert len(bms) == 1 and bms[0]["label"] == "second"


def test_older_edit_is_skipped(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    _bm(c, id="bm-1", holding_id=hid, label="newer", updated_at="2026-06-26T12:00:00Z")
    res = _bm(c, id="bm-1", holding_id=hid, label="stale", updated_at="2026-06-26T09:00:00Z")
    assert res["applied"] == [{"id": "bm-1", "skipped": True}]
    bms = c.get("/sync/reader?since=0").get_json()["bookmarks"]
    assert bms[0]["label"] == "newer"          # the stale offline edit did not clobber it


def test_reapplying_same_op_is_idempotent(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    op = dict(id="bm-1", holding_id=hid, locator="5", updated_at="2026-06-26T10:00:00Z")
    _bm(c, **op)
    _bm(c, **op)                                # client re-sends an unacked op
    bms = c.get("/sync/reader?since=0").get_json()["bookmarks"]
    assert len(bms) == 1                        # no duplicate row


# ── tombstones ───────────────────────────────────────────────────────────────
def test_delete_tombstone_propagates(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    _bm(c, id="bm-1", holding_id=hid, locator="1", updated_at="2026-06-26T10:00:00Z")
    # A delete is just an edit that sets deleted_at (and bumps updated_at).
    _bm(c, id="bm-1", holding_id=hid, locator="1", deleted_at="2026-06-26T10:05:00Z",
        updated_at="2026-06-26T10:05:00Z")
    bms = c.get("/sync/reader?since=0").get_json()["bookmarks"]
    assert len(bms) == 1                        # the tombstone is RETURNED, not hidden…
    assert bms[0]["deleted_at"] == "2026-06-26T10:05:00Z"   # …so other devices learn of it


# ── referential integrity + auth ─────────────────────────────────────────────
def test_marks_survive_holding_delete_as_orphans(app_env, seed):
    # Under "survive and re-attach" (reader plan N0) a hard holding delete does NOT cascade-delete
    # its marks: holding_id is SET NULL and the row persists (keyed by content_hash) to re-link on
    # re-import. (Previously this cascade-deleted the marks.)
    c, _, _ = app_env
    hid = _holding(seed)
    _bm(c, id="bm-1", holding_id=hid, locator="1", updated_at="2026-06-26T10:00:00Z")
    seed("DELETE FROM holding WHERE id = ?", (hid,))
    bms = c.get("/sync/reader?since=0").get_json()["bookmarks"]
    assert [b["id"] for b in bms] == ["bm-1"]      # survived the delete…
    assert bms[0]["holding_id"] is None            # …orphaned (SET NULL), not cascade-deleted


def test_unknown_op_type_is_ignored(app_env, seed):
    c, _, _ = app_env
    res = c.post("/sync/reader", json={"ops": [{"type": "wat", "id": "z"}]}).get_json()
    assert res["applied"] == []


# ── annotations ride the same endpoint ───────────────────────────────────────
def _ann(c, **fields):
    op = {"type": "annotation"}
    op.update(fields)
    return c.post("/sync/reader", json={"ops": [op]}).get_json()


def test_annotation_push_then_pull(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    res = _ann(c, id="an-1", holding_id=hid, kind="highlight",
               cfi_range="epubcfi(/6/4!/4)", color="yellow", note_text="key",
               updated_at="2026-06-26T10:00:00Z")
    assert res["applied"][0]["id"] == "an-1"
    pull = c.get("/sync/reader?since=0").get_json()
    assert len(pull["annotations"]) == 1
    a = pull["annotations"][0]
    assert a["kind"] == "highlight" and a["color"] == "yellow"
    assert a["cfi_range"] == "epubcfi(/6/4!/4)" and a["note_text"] == "key"


def test_bookmarks_and_annotations_pull_together(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    _bm(c, id="b1", holding_id=hid, locator="1", updated_at="2026-06-26T10:00:00Z")
    _ann(c, id="a1", holding_id=hid, kind="highlight", updated_at="2026-06-26T10:01:00Z")
    pull = c.get("/sync/reader?since=0").get_json()
    assert {b["id"] for b in pull["bookmarks"]} == {"b1"}
    assert {a["id"] for a in pull["annotations"]} == {"a1"}


def test_annotation_tombstone_propagates(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    _ann(c, id="a1", holding_id=hid, kind="highlight", updated_at="2026-06-26T10:00:00Z")
    _ann(c, id="a1", holding_id=hid, kind="highlight",
         deleted_at="2026-06-26T10:05:00Z", updated_at="2026-06-26T10:05:00Z")
    anns = c.get("/sync/reader?since=0").get_json()["annotations"]
    assert len(anns) == 1 and anns[0]["deleted_at"] == "2026-06-26T10:05:00Z"


def test_viewer_is_denied_reader_sync(tmp_path, monkeypatch):
    """Bookmarks are the library OWNER's. A read-only viewer (guest) is shut out of
    /sync/reader entirely: the push is rejected (the write gate + the handler's can_edit
    check), and the pull is default-denied because the endpoint isn't in the viewer GET
    allowlist — so a guest never sees the owner's private bookmarks."""
    monkeypatch.setenv("CATALOGUE_AUTH_USER", "owner")
    monkeypatch.setenv("CATALOGUE_AUTH_PASS", "owner-pass")
    monkeypatch.setenv("CATALOGUE_VIEWER_USER", "friend")
    monkeypatch.setenv("CATALOGUE_VIEWER_PASS", "friend-pass")
    app = create_app(tmp_path / "v.db")
    app.testing = True
    with app.test_client() as c:
        c.post("/login", data={"username": "friend", "password": "friend-pass", "next": "/"})
        push = c.post("/sync/reader", json={"ops": [{"type": "bookmark", "id": "x", "holding_id": 1}]})
        assert push.status_code == 403
        assert c.get("/sync/reader?since=0").status_code == 403

    # The editor (open-access default in the other tests) is the one who syncs bookmarks.
    with app.test_client() as c:
        c.post("/login", data={"username": "owner", "password": "owner-pass", "next": "/"})
        assert c.get("/sync/reader?since=0").status_code == 200


def test_holding_scoped_delta_includes_tombstones(app_env, seed):
    # `?holding&since` is the native/PWA per-book offline pull: only this holding's rows, with
    # rev > since, INCLUDING tombstones (so a deletion on another device propagates). Bare
    # `?holding` stays live-only (the web reader's paint).
    c, _, _ = app_env
    hid = _holding(seed)
    _ann(c, id="an-live", holding_id=hid, kind="highlight", updated_at="2026-06-26T10:00:00Z")
    _ann(c, id="an-gone", holding_id=hid, kind="ink", updated_at="2026-06-26T10:01:00Z")
    # tombstone the second one
    _ann(c, id="an-gone", holding_id=hid, deleted_at="2026-06-26T10:02:00Z",
         updated_at="2026-06-26T10:02:00Z")

    delta = c.get(f"/sync/reader?holding={hid}&since=0").get_json()["annotations"]
    by_id = {a["id"]: a for a in delta}
    assert set(by_id) == {"an-live", "an-gone"}          # tombstone INCLUDED in the delta
    assert by_id["an-gone"]["deleted_at"] == "2026-06-26T10:02:00Z"

    live = c.get(f"/sync/reader?holding={hid}").get_json()["annotations"]   # bare = live only
    assert [a["id"] for a in live] == ["an-live"]


# ── e2e: the iOS ReaderSync wire ⇄ the real /sync/reader route ────────────────
# Mirrors EXACTLY the legacy op shape `ReaderSync.LegacyOp` serialises (snake_case; rect + ink as
# JSON strings; holding:<id> → holding_id), pushes it through the live Flask app + SQLite store, and
# proves the mark round-trips AND is visible to the web reader's own pull — the cross-binding "a mark
# made on iOS appears in the web reader" contract (N0b), end to end minus the Swift client (Mac-run).
def test_e2e_ios_wire_roundtrips_and_is_visible_to_web(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    ink = '{"strokes":[{"color":"#ff0000","mode":"draw","points":[[0.1,0.2,0.5,0]],"width":4}]}'
    op = {
        "type": "annotation",
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "holding_id": hid,                       # ReaderSync parsed this from "holding:<id>"
        "kind": "highlight",
        "cfi_range": "epubcfi(/6/4!/4)",
        "page": 3,
        "rect": "[0.1,0.2,0.3,0.05]",            # region → JSON string
        "color": "#ffd54a",
        "note_text": "key passage",
        "ink": ink,                               # Ink struct → canonical JSON string
        "created_at": "2026-06-29T10:00:00Z",
        "updated_at": "2026-06-29T10:00:00Z",
    }
    applied = c.post("/sync/reader", json={"ops": [op]}).get_json()["applied"]
    assert applied[0]["id"] == op["id"] and "rev" in applied[0]   # accepted, not skipped

    # iOS pull (holding-scoped delta) — every field survives the round-trip losslessly
    a = c.get(f"/sync/reader?holding={hid}&since=0").get_json()["annotations"][0]
    assert a["kind"] == "highlight"
    assert a["page"] == 3
    assert a["cfi_range"] == "epubcfi(/6/4!/4)"
    assert a["rect"] == "[0.1,0.2,0.3,0.05]"      # region JSON-string preserved
    assert a["ink"] == ink                         # ink JSON-string preserved (byte-for-byte)
    assert a["color"] == "#ffd54a" and a["note_text"] == "key passage"

    # …and the SAME mark is visible to the web reader's live pull (different surface, one store)
    web = c.get(f"/sync/reader?holding={hid}").get_json()["annotations"]
    assert [x["id"] for x in web] == [op["id"]]


def test_e2e_ios_tombstone_propagates_to_other_device(app_env, seed):
    # A delete on "device A" (tombstone op) must reach "device B" via the holding delta pull.
    c, _, _ = app_env
    hid = _holding(seed)
    _ann(c, id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", holding_id=hid, kind="ink",
         updated_at="2026-06-29T10:00:00Z")
    _ann(c, id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", holding_id=hid,
         deleted_at="2026-06-29T10:05:00Z", updated_at="2026-06-29T10:05:00Z")
    delta = c.get(f"/sync/reader?holding={hid}&since=0").get_json()["annotations"]
    assert delta[0]["deleted_at"] == "2026-06-29T10:05:00Z"   # device B learns of the deletion


# ── authored PDF outlines ride the same endpoint (Shape B, wholesale LWW) ─────
def _outline(c, **fields):
    op = {"type": "outline"}
    op.update(fields)
    return c.post("/sync/reader", json={"ops": [op]}).get_json()


def _entries(*rows):
    return json.dumps([{"level": lvl, "title": t, "page": p} for lvl, t, p in rows])


def test_outline_push_then_pull(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    body = _entries((1, "Chapter One", 1), (2, "Section 1.1", 2))
    res = _outline(c, id=f"outline:holding:{hid}", holding_id=hid, entries=body,
                   updated_at="2026-06-29T10:00:00Z")
    assert res["applied"][0]["id"] == f"outline:holding:{hid}" and "rev" in res["applied"][0]

    pull = c.get("/sync/reader?since=0").get_json()
    assert len(pull["outlines"]) == 1
    o = pull["outlines"][0]
    assert o["holding_id"] == hid
    assert json.loads(o["entries"]) == [{"level": 1, "title": "Chapter One", "page": 1},
                                        {"level": 2, "title": "Section 1.1", "page": 2}]


def test_outline_is_wholesale_lww_by_stable_id(app_env, seed):
    """Two devices editing the same copy's outline use the SAME stable id → the newer edit replaces
    the whole outline (one row), not a merge of entries."""
    c, _, _ = app_env
    hid = _holding(seed)
    oid = f"outline:holding:{hid}"
    _outline(c, id=oid, holding_id=hid, entries=_entries((1, "Old", 1)),
             updated_at="2026-06-29T10:00:00Z")
    _outline(c, id=oid, holding_id=hid, entries=_entries((1, "New A", 1), (1, "New B", 5)),
             updated_at="2026-06-29T11:00:00Z")
    outs = c.get("/sync/reader?since=0").get_json()["outlines"]
    assert len(outs) == 1                                   # one outline per copy
    assert [e["title"] for e in json.loads(outs[0]["entries"])] == ["New A", "New B"]


def test_outline_older_edit_skipped(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    oid = f"outline:holding:{hid}"
    _outline(c, id=oid, holding_id=hid, entries=_entries((1, "Newer", 1)),
             updated_at="2026-06-29T12:00:00Z")
    res = _outline(c, id=oid, holding_id=hid, entries=_entries((1, "Stale", 1)),
                   updated_at="2026-06-29T09:00:00Z")
    assert res["applied"] == [{"id": oid, "skipped": True}]
    outs = c.get("/sync/reader?since=0").get_json()["outlines"]
    assert json.loads(outs[0]["entries"])[0]["title"] == "Newer"


# ── the cheap change-probe: GET /sync/reader/rev ─────────────────────────────
def test_reader_sync_rev_probe_reports_per_resource_max(app_env, seed):
    """The reader asks this before pulling: max rev per resource for one copy, so it can skip the
    full pull when nothing changed and only fetch when it did."""
    c, _, _ = app_env
    hid = _holding(seed)

    # Nothing yet → all zero.
    revs = c.get(f"/sync/reader/rev?holding={hid}").get_json()
    assert (revs["bookmarks_rev"], revs["annotations_rev"], revs["outlines_rev"]) == (0, 0, 0)

    # A bookmark, an annotation, an outline each advance their own probe (shared monotonic rev).
    _bm(c, id="bm-1", holding_id=hid, updated_at="2026-06-29T10:00:00Z")
    _ann(c, id="an-1", holding_id=hid, kind="highlight", updated_at="2026-06-29T10:01:00Z")
    _outline(c, id=f"outline:holding:{hid}", holding_id=hid, entries=_entries((1, "X", 1)),
             updated_at="2026-06-29T10:02:00Z")
    revs = c.get(f"/sync/reader/rev?holding={hid}").get_json()
    assert (revs["bookmarks_rev"], revs["annotations_rev"], revs["outlines_rev"]) == (1, 2, 3)

    # A new write to ONE resource moves only that probe — the reader learns exactly what to re-pull.
    before = c.get(f"/sync/reader/rev?holding={hid}").get_json()
    _bm(c, id="bm-2", holding_id=hid, updated_at="2026-06-29T10:03:00Z")
    after = c.get(f"/sync/reader/rev?holding={hid}").get_json()
    assert after["bookmarks_rev"] > before["bookmarks_rev"]        # change detected
    assert after["annotations_rev"] == before["annotations_rev"]   # unrelated resources unchanged
    assert after["outlines_rev"] == before["outlines_rev"]


def test_reader_sync_rev_requires_holding(app_env, seed):
    c, _, _ = app_env
    assert c.get("/sync/reader/rev").status_code == 400


def test_outline_holding_scoped_live_and_tombstone(app_env, seed):
    c, _, _ = app_env
    hid = _holding(seed)
    oid = f"outline:holding:{hid}"
    _outline(c, id=oid, holding_id=hid, entries=_entries((1, "Live", 1)),
             updated_at="2026-06-29T10:00:00Z")
    # bare ?holding = live paint: one outline
    live = c.get(f"/sync/reader?holding={hid}").get_json()["outlines"]
    assert len(live) == 1 and json.loads(live[0]["entries"])[0]["title"] == "Live"
    # tombstone → live paint drops it, delta pull still carries it
    _outline(c, id=oid, holding_id=hid, deleted_at="2026-06-29T10:05:00Z",
             updated_at="2026-06-29T10:05:00Z")
    assert c.get(f"/sync/reader?holding={hid}").get_json()["outlines"] == []
    delta = c.get(f"/sync/reader?holding={hid}&since=0").get_json()["outlines"]
    assert delta[-1]["deleted_at"] == "2026-06-29T10:05:00Z"


def test_e2e_ios_bookmark_wire_roundtrips(app_env, seed):
    # Mirrors the iOS `BookmarkSync` wire: a {type:"bookmark", ...} op with the opaque `locator`
    # string (a page number here), pulled back via the holding-scoped delta — the bookmark-sync
    # round-trip end to end (server already owns bookmarks).
    c, _, _ = app_env
    hid = _holding(seed)
    _bm(c, id="bk-1", holding_id=hid, locator="42", fraction=0.5, label="spot",
        created_at="2026-06-29T10:00:00Z", updated_at="2026-06-29T10:00:00Z")
    bms = c.get(f"/sync/reader?holding={hid}&since=0").get_json()["bookmarks"]
    assert [b["id"] for b in bms] == ["bk-1"]
    assert bms[0]["locator"] == "42"
    assert bms[0]["label"] == "spot"
    assert bms[0]["fraction"] == 0.5
