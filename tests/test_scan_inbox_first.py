"""Inbox-first, incremental scanning (catalogue/domain/sweep.walk +
reconcile.scan_roots / iter_scanned / reconcile_stream).

Pins: the walk yields inbox files before the rest; a standalone top-level `_INBOX/`
is pulled into the scan set; and the streaming reconcile classifies + commits inbox
drops FIRST so they're reviewable before a full-library walk finishes.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import init_db
from catalogue.services import sweep, reconcile, mount, filing


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A library tree + vocab. Layout:

        <lib>/Library/_INBOX/            (standalone inbox, sibling of roots)
        <lib>/Library/01 Books - Dharma/ (root)
              ├─ _inbox/   (per-root inbox)
              └─ Tantra/   (a shelf)
    """
    lib = tmp_path / "Library"
    top_inbox = lib / "_INBOX"
    root = lib / "01 Books - Dharma"
    root_inbox = root / "_inbox"
    tantra = root / "Tantra"
    for d in (top_inbox, root_inbox, tantra):
        d.mkdir(parents=True)

    vocab = tmp_path / "vocab.json"
    vocab.write_text(json.dumps({
        "_library_roots": [{"id": 1, "path": str(root), "derive_subject": False}],
        # Inbox membership is by configured folder now (no magic name): the standalone
        # top-level inbox AND a per-root one are both listed explicitly.
        "_inbox_dirs": [str(top_inbox), str(root_inbox)],
    }))
    monkeypatch.setattr(mount, "VOCAB_PATH", vocab)
    monkeypatch.setattr(filing, "VOCAB_PATH", vocab)

    conn = init_db(tmp_path / "s.db")
    yield conn, {"lib": lib, "top_inbox": top_inbox, "root": root,
                 "root_inbox": root_inbox, "tantra": tantra}
    conn.close()


# ── walk ordering ────────────────────────────────────────────────────────────
def test_walk_yields_inbox_files_first(tree):
    _, p = tree
    (p["tantra"] / "shelf.pdf").write_bytes(b"%PDF s")
    (p["root_inbox"] / "rootdrop.pdf").write_bytes(b"%PDF r")
    (p["top_inbox"] / "topdrop.pdf").write_bytes(b"%PDF t")

    order = [pp.name for pp in sweep.walk([p["root"], p["top_inbox"]], (".pdf",))]
    # both inbox files precede the shelved one
    assert order.index("shelf.pdf") == len(order) - 1
    assert set(order[:2]) == {"rootdrop.pdf", "topdrop.pdf"}


def test_walk_inbox_first_can_be_disabled(tree):
    _, p = tree
    (p["tantra"] / "shelf.pdf").write_bytes(b"%PDF s")
    (p["root_inbox"] / "drop.pdf").write_bytes(b"%PDF r")
    order = [pp.name for pp in sweep.walk(p["root"], (".pdf",), inbox_first=False)]
    # plain os.walk order: the root's own subdirs sorted — no inbox prioritisation
    # across the result beyond the per-level `_inbox`-first sort.
    assert set(order) == {"shelf.pdf", "drop.pdf"}


# ── scan set includes the standalone inbox ─────────────────────────────────────
def test_scan_roots_includes_standalone_inbox_dir(tree):
    _, p = tree
    roots = reconcile.scan_roots()
    assert str(p["root"]) in roots
    assert str(p["top_inbox"]) in roots


def test_scan_roots_skips_inbox_nested_in_a_root(tree, monkeypatch):
    # A per-root `_inbox/` is already walked via its root → not added again.
    _, p = tree
    vocab = filing.VOCAB_PATH
    data = json.loads(vocab.read_text())
    data["_inbox_dirs"] = [str(p["root_inbox"])]      # lives INSIDE root id=1
    vocab.write_text(json.dumps(data))
    assert reconcile.scan_roots() == [str(p["root"])]


# ── streaming reconcile: inbox drops classified + committed first ───────────────
def test_reconcile_stream_enqueues_inbox_first(tree):
    conn, p = tree
    (p["tantra"] / "Shelf Book.pdf").write_bytes(b"%PDF shelf bytes")
    (p["top_inbox"] / "Inbox Book.pdf").write_bytes(b"%PDF inbox bytes")

    summary = reconcile.reconcile_stream(conn, reconcile.scan_roots(), batch=1)
    assert summary["scanned"] == 2 and summary["enqueued"] == 2

    # review_queue rowid order = insertion order; the inbox drop was enqueued first.
    rows = conn.execute(
        "SELECT payload_json FROM review_queue WHERE item_type = 'ingest' ORDER BY id"
    ).fetchall()
    paths = [json.loads(r[0])["path"] for r in rows]
    assert paths[0] == str(p["top_inbox"] / "Inbox Book.pdf")


def test_reconcile_stream_commits_each_batch(tree):
    conn, p = tree
    for i in range(3):
        (p["top_inbox"] / f"d{i}.pdf").write_bytes(f"%PDF {i}".encode())

    seen = []
    # A separate connection proves rows are committed (visible) mid-scan, not buffered.
    from catalogue.db_store import connect
    other = connect(str(conn.execute("PRAGMA database_list").fetchone()[2]))

    def _progress(s):
        seen.append(other.execute(
            "SELECT COUNT(*) FROM review_queue WHERE item_type='ingest'").fetchone()[0])

    reconcile.reconcile_stream(conn, reconcile.scan_roots(), batch=1, on_progress=_progress)
    other.close()
    # the committed count is observed growing from another connection
    assert seen and seen[-1] == 3 and seen[0] >= 1
