"""Reader-state store (catalogue.db_store.reader_state) — the unit-level home for the bookmark +
annotation sync/merge semantics that /sync/reader rides on.

These drive the typed `ReaderStateStore` PORT directly, pinning the offline-first rules —
monotonic rev, last-write-wins, tombstones, and per-kind annotation anchoring — independently of
the HTTP transport. Every test runs against BOTH adapters (the SQLite one and the in-memory fake)
via the `store`/`hid` fixtures, proving the contract holds for any implementation.
"""
from __future__ import annotations

import json

import pytest

from catalogue.test_kit import seed_minimal
from catalogue.db_store import reader_state as rs


@pytest.fixture(params=["sqlite", "memory"])
def store_hid(request, cat_conn):
    """(store, holding_id) for each adapter. The SQLite store needs a real holding (FK); the
    in-memory fake has no FK, so any id works."""
    if request.param == "sqlite":
        seed_minimal(cat_conn)
        store = rs.SqliteReaderStateStore(cat_conn)
        store.ensure_schema()
        cat_conn.commit()
        hid = cat_conn.execute("SELECT id FROM holding ORDER BY id LIMIT 1").fetchone()[0]
        return store, hid
    store = rs.InMemoryReaderStateStore()
    store.ensure_schema()
    return store, 1


@pytest.fixture
def store(store_hid):
    return store_hid[0]


@pytest.fixture
def hid(store_hid):
    return store_hid[1]


def test_ensure_schema_is_idempotent(store):
    store.ensure_schema()              # second call must not raise
    assert store.cursor() == 0


# ── bookmarks ─────────────────────────────────────────────────────────────────
def test_apply_then_read_back(store, hid):
    bm = store.apply_bookmark(id="bm-1", holding_id=hid, locator="42",
                              fraction=0.5, label="spot", updated_at="2026-06-26T10:00:00Z")
    assert bm is not None and bm.rev == 1 and bm.locator == "42"
    got = store.bookmarks_since(0)
    assert [b.id for b in got] == ["bm-1"] and got[0].label == "spot"


def test_rev_is_monotonic_and_cursor_tracks_it(store, hid):
    store.apply_bookmark(id="a", holding_id=hid, updated_at="2026-06-26T10:00:00Z")
    store.apply_bookmark(id="b", holding_id=hid, updated_at="2026-06-26T10:01:00Z")
    assert store.cursor() == 2
    assert [b.id for b in store.bookmarks_since(1)] == ["b"]   # only after the cursor


def test_last_write_wins(store, hid):
    store.apply_bookmark(id="x", holding_id=hid, label="old", updated_at="2026-06-26T10:00:00Z")
    assert store.apply_bookmark(id="x", holding_id=hid, label="new",
                                updated_at="2026-06-26T11:00:00Z") is not None
    # a stale (older) edit is skipped, not applied
    assert store.apply_bookmark(id="x", holding_id=hid, label="stale",
                                updated_at="2026-06-26T09:00:00Z") is None
    only = store.bookmarks_since(0)
    assert len(only) == 1 and only[0].label == "new"


def test_tombstone_is_returned(store, hid):
    store.apply_bookmark(id="t", holding_id=hid, updated_at="2026-06-26T10:00:00Z")
    store.apply_bookmark(id="t", holding_id=hid, deleted_at="2026-06-26T10:05:00Z",
                         updated_at="2026-06-26T10:05:00Z")
    rows = store.bookmarks_since(0)
    assert len(rows) == 1 and rows[0].deleted_at == "2026-06-26T10:05:00Z"


def test_malformed_op_raises(store, hid):
    with pytest.raises(ValueError):
        store.apply_bookmark(id="", holding_id=hid)
    with pytest.raises(ValueError):
        store.apply_bookmark(id="ok", holding_id=None)


# ── annotations: every kind round-trips with its anchoring intact ─────────────
def test_highlight_round_trip(store, hid):
    a = store.apply_annotation(id="an-1", holding_id=hid, kind="highlight",
                               cfi_range="epubcfi(/6/4!/4,/2/1:0,/2/1:10)", color="yellow",
                               note_text="nice", updated_at="2026-06-26T10:00:00Z")
    assert a is not None and a.kind == "highlight" and a.color == "yellow" and a.rev == 1
    got = store.annotations_since(0)
    assert [x.id for x in got] == ["an-1"]
    assert got[0].note_text == "nice" and got[0].cfi_range.startswith("epubcfi(")


def test_underline_epub_cfi_and_pdf_rect(store, hid):
    # EPUB underline anchors to a CFI range; PDF underline to page + normalised rects.
    store.apply_annotation(id="ul-epub", holding_id=hid, kind="underline",
                           cfi_range="epubcfi(/6/4!/4,/2/1:0,/2/1:8)", color="#90caf9",
                           updated_at="2026-06-26T10:00:00Z")
    rects = json.dumps([[0.1, 0.2, 0.3, 0.02]])
    store.apply_annotation(id="ul-pdf", holding_id=hid, kind="underline", page=4, rect=rects,
                           color="#90caf9", updated_at="2026-06-26T10:01:00Z")
    by_id = {x.id: x for x in store.annotations_since(0)}
    assert by_id["ul-epub"].kind == "underline" and by_id["ul-epub"].cfi_range.startswith("epubcfi(")
    assert by_id["ul-pdf"].page == 4 and json.loads(by_id["ul-pdf"].rect) == [[0.1, 0.2, 0.3, 0.02]]


def test_strikeout_round_trip(store, hid):
    rects = json.dumps([[0.1, 0.5, 0.4, 0.02]])
    a = store.apply_annotation(id="st-1", holding_id=hid, kind="strikeout", page=2, rect=rects,
                               color="#f48fb1", updated_at="2026-06-26T10:00:00Z")
    assert a is not None and a.kind == "strikeout"
    assert store.annotations_since(0)[0].rect == rects


def test_standalone_note_round_trip(store, hid):
    a = store.apply_annotation(id="nt-1", holding_id=hid, kind="note", page=3,
                               rect=json.dumps([0.5, 0.5]), note_text="margin thought",
                               updated_at="2026-06-26T10:00:00Z")
    assert a is not None and a.kind == "note" and a.note_text == "margin thought"
    assert store.annotations_since(0)[0].page == 3


def test_ink_round_trips_as_raw_points(store, hid):
    # Ink is stored as RAW page-relative [x,y,pressure] points (not a rendered outline) so the
    # same record round-trips to PencilKit / a native renderer — the cross-platform contract.
    ink = json.dumps({"strokes": [{"points": [[0.1, 0.1, 0.5], [0.2, 0.15, 0.7]],
                                    "width": 3, "color": "#111"}]})
    a = store.apply_annotation(id="ink-1", holding_id=hid, kind="ink", page=5, ink=ink,
                               updated_at="2026-06-26T10:00:00Z")
    assert a is not None and a.kind == "ink"
    back = json.loads(store.annotations_since(0)[0].ink)
    assert back["strokes"][0]["points"][0] == [0.1, 0.1, 0.5] and back["strokes"][0]["width"] == 3


def test_annotation_lww_and_tombstone(store, hid):
    store.apply_annotation(id="a", holding_id=hid, kind="highlight", color="yellow",
                           updated_at="2026-06-26T10:00:00Z")
    assert store.apply_annotation(id="a", holding_id=hid, kind="highlight", color="pink",
                                  updated_at="2026-06-26T09:00:00Z") is None        # older skipped
    store.apply_annotation(id="a", holding_id=hid, kind="highlight", color="green",
                           updated_at="2026-06-26T11:00:00Z")                        # newer wins
    assert store.annotations_since(0)[0].color == "green"
    store.apply_annotation(id="a", holding_id=hid, kind="highlight",
                           deleted_at="2026-06-26T12:00:00Z", updated_at="2026-06-26T12:00:00Z")
    assert store.annotations_since(0)[0].deleted_at == "2026-06-26T12:00:00Z"  # tombstone kept


def test_annotation_malformed_op_raises(store, hid):
    with pytest.raises(ValueError):
        store.apply_annotation(id="", holding_id=hid, kind="ink")
    with pytest.raises(ValueError):
        store.apply_annotation(id="ok", holding_id=None, kind="ink")


def test_bookmarks_and_annotations_share_the_rev_counter(store, hid):
    store.apply_bookmark(id="b1", holding_id=hid, updated_at="2026-06-26T10:00:00Z")
    store.apply_annotation(id="a1", holding_id=hid, kind="highlight",
                           updated_at="2026-06-26T10:01:00Z")
    assert store.cursor() == 2
    # a client synced to rev 1 catches only what changed after — the annotation.
    assert [b.id for b in store.bookmarks_since(1)] == []
    assert [a.id for a in store.annotations_since(1)] == ["a1"]


# ── content identity: stamp / survive-delete / re-link (N0 "survive and re-attach") ──
# These exercise the real SQLite FK behaviour, so they don't use the memory-parametrized fixture.

_LEGACY_ANNOTATION_DDL = """
CREATE TABLE annotation (
  id TEXT PRIMARY KEY,
  holding_id INTEGER NOT NULL REFERENCES holding(id) ON DELETE CASCADE,
  kind TEXT, cfi_range TEXT, page INTEGER, rect TEXT, color TEXT,
  note_text TEXT, ink TEXT, created_at TEXT, updated_at TEXT, deleted_at TEXT,
  rev INTEGER NOT NULL DEFAULT 0
);
"""


def _sqlite_store(cat_conn, content_hash="ch-abc"):
    seed_minimal(cat_conn)
    cat_conn.execute("PRAGMA foreign_keys=ON")
    store = rs.SqliteReaderStateStore(cat_conn)
    store.ensure_schema()
    hid = cat_conn.execute("SELECT id FROM holding ORDER BY id LIMIT 1").fetchone()[0]
    cat_conn.execute("UPDATE holding SET content_hash = ? WHERE id = ?", (content_hash, hid))
    return store, hid


def test_content_hash_is_stamped_from_holding(cat_conn):
    store, hid = _sqlite_store(cat_conn)
    a = store.apply_annotation(id="a1", holding_id=hid, kind="ink",
                               updated_at="2026-06-29T10:00:00Z")
    assert a.content_hash == "ch-abc"
    b = store.apply_bookmark(id="b1", holding_id=hid, updated_at="2026-06-29T10:00:00Z")
    assert b.content_hash == "ch-abc"


def test_mark_survives_holding_delete_as_orphan(cat_conn):
    store, hid = _sqlite_store(cat_conn)
    store.apply_annotation(id="a1", holding_id=hid, kind="highlight",
                           updated_at="2026-06-29T10:00:00Z")
    cat_conn.execute("DELETE FROM holding WHERE id = ?", (hid,))   # hard delete (no cascade now)
    row = cat_conn.execute(
        "SELECT holding_id, content_hash FROM annotation WHERE id = 'a1'").fetchone()
    assert row == (None, "ch-abc")     # survived, orphaned, identity kept


def test_relink_orphans_reattaches_by_content_hash(cat_conn):
    store, hid = _sqlite_store(cat_conn)
    eid = cat_conn.execute("SELECT edition_id FROM holding WHERE id = ?", (hid,)).fetchone()[0]
    store.apply_annotation(id="a1", holding_id=hid, kind="highlight",
                           updated_at="2026-06-29T10:00:00Z")
    cat_conn.execute("DELETE FROM holding WHERE id = ?", (hid,))   # orphan it
    # re-import the same file -> a new holding row with the same content_hash
    cat_conn.execute(
        "INSERT INTO holding (edition_id, content_hash) VALUES (?, 'ch-abc')", (eid,))
    new_hid = cat_conn.execute(
        "SELECT id FROM holding WHERE content_hash = 'ch-abc'").fetchone()[0]
    n = store.relink_orphans(holding_id=new_hid, content_hash="ch-abc")
    assert n == 1
    got = cat_conn.execute("SELECT holding_id FROM annotation WHERE id = 'a1'").fetchone()[0]
    assert got == new_hid


def test_ensure_schema_migrates_legacy_cascade_table(cat_conn):
    seed_minimal(cat_conn)
    cat_conn.execute("PRAGMA foreign_keys=ON")
    hid = cat_conn.execute("SELECT id FROM holding ORDER BY id LIMIT 1").fetchone()[0]
    # stand up the OLD-shape table (NOT NULL + CASCADE, no content_hash) with a row
    cat_conn.executescript(_LEGACY_ANNOTATION_DDL)
    cat_conn.execute(
        "INSERT INTO annotation (id, holding_id, kind, rev) VALUES ('a1', ?, 'highlight', 1)", (hid,))
    # migrate
    rs.SqliteReaderStateStore(cat_conn).ensure_schema()
    cols = {r[1] for r in cat_conn.execute("PRAGMA table_info(annotation)").fetchall()}
    assert "content_hash" in cols                       # upgraded
    fk = cat_conn.execute("PRAGMA foreign_key_list(annotation)").fetchall()
    assert any(f[2] == "holding" and (f[6] or "").upper() == "SET NULL" for f in fk)
    assert cat_conn.execute("SELECT COUNT(*) FROM annotation WHERE id='a1'").fetchone()[0] == 1  # row kept
