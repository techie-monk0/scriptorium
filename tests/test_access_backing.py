"""The Backing port — a holding's file is one pluggable backing.

The access layer performs no filesystem effect directly; it goes through `acc.backing`, so the bytes
can live on the local disk (LocalBacking, default), an object store, or a test fake. A holding delete
hands its post-commit FileOps to the backing — here an injected recorder proves it, with no real I/O.
Uses the test-kit fixtures. See access_api/backing.py.
"""
from catalogue.access_api import bind
from catalogue.access_api.backing import Backing, LocalBacking
from catalogue.contracts import AllowAll, SYSTEM, FileOp
from catalogue.test_kit import seed_minimal


class RecordingBacking(Backing):
    """Records the FileOps it's handed instead of touching disk."""

    def __init__(self):
        self.ran = []

    def exists(self, path):
        return False

    def run(self, file_ops, trash_dir):
        self.ran.extend(file_ops)


def test_default_backing_is_local(cat_acc):
    assert isinstance(cat_acc.backing, LocalBacking)


def test_holding_delete_routes_fileops_through_injected_backing(cat_db, cat_conn):
    ids = seed_minimal(cat_conn)
    cat_conn.commit()
    fake = RecordingBacking()
    with bind(SYSTEM, AllowAll(), cat_db, backing=fake) as acc:
        h = acc.holdings.reads.get(ids["holding"])
        acc.holdings.writes.apply(acc.holdings.writes.plan_delete(h.ref()))
    assert any(f.op == "trash" and f.path == f"/sample/e{ids['edition']}.pdf" for f in fake.ran)


def test_local_backing_trashes_and_moves(tmp_path):
    b = LocalBacking()
    src = tmp_path / "a.pdf"
    src.write_bytes(b"x")
    trash = tmp_path / ".trash"
    b.run([FileOp("trash", str(src))], str(trash))
    assert not src.exists() and (trash / "a.pdf").exists()

    moved_from = tmp_path / "b.pdf"
    moved_from.write_bytes(b"y")
    dest = tmp_path / "sub" / "c.pdf"
    b.run([FileOp("move", str(moved_from), str(dest))], str(trash))
    assert dest.exists() and not moved_from.exists()
    assert b.exists(str(dest)) and not b.exists(str(moved_from))


def test_local_backing_skips_missing_source(tmp_path):
    LocalBacking().run([FileOp("trash", str(tmp_path / "nope.pdf"))], str(tmp_path / ".trash"))
    assert not (tmp_path / ".trash").exists()       # nothing created for a missing source
