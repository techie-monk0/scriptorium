"""test-kit self-tests — the shared fixtures, fake store, policies, and sample seeder.

Lives under tests/ (the suite's testpaths) so it's collected by the root pytest run, which also
proves the `pytest11` entry point auto-loaded the plugin (cat_db/cat_conn/cat_acc resolve with no
conftest import). See catalogue/test_kit/.
"""
import pytest

from catalogue.access_api.holdings import HoldingRepo
from catalogue.contracts import AccessMode, Denied, Ref
from catalogue.test_kit import (
    DenyAll,
    InMemoryHoldingStore,
    RecordingPolicy,
    principal,
    seed_minimal,
)


# ── fixtures + sample seeder (proves the plugin loaded) ───────────────────────────
def test_seed_minimal_links_through_the_access_api(cat_conn, cat_acc):
    ids = seed_minimal(cat_conn)
    cat_conn.commit()
    # read back through a bound Access on the same DB file
    h = cat_acc.holdings.reads.get(ids["holding"])
    assert h is not None and h.edition_id == ids["edition"]
    w = cat_acc.works.reads.get(ids["work"])
    assert w.title == "Sample Work" and w.author_ids == (ids["person"],)
    assert [x.id for x in cat_acc.works.reads.by_edition(ids["edition"])] == [ids["work"]]


# ── in-memory fake store: the access layer with NO database ───────────────────────
def test_fake_holding_store_drives_reads_and_update(cat_acc):
    fake = InMemoryHoldingStore([
        {"id": 1, "edition_id": 7, "content_hash": "h1", "text_status": "ocr_poor"},
    ])
    cat_acc.holdings = HoldingRepo(cat_acc, fake)        # inject — no SQLite touched
    assert cat_acc.holdings.reads.get(1).text_status == "ocr_poor"
    plan = cat_acc.holdings.writes.plan_set_text_status(Ref("holding", 1, "h1"), "ocr_good")
    assert plan.appliable and plan.changes == {"text_status": "ocr_good"}
    cat_acc.holdings.writes.apply(plan)
    assert cat_acc.holdings.reads.get(1).text_status == "ocr_good"


def test_fake_holding_store_delete_enumerates_nonfk_closure(cat_acc):
    # two holdings share a file_hash; deleting one must KEEP the shared caches (the other holds it)
    fake = InMemoryHoldingStore([
        {"id": 1, "edition_id": 7, "file_hash": "fh", "file_path": "/a.pdf", "content_hash": "c1"},
        {"id": 2, "edition_id": 7, "file_hash": "fh", "file_path": "/b.pdf", "content_hash": "c2"},
    ])
    cat_acc.holdings = HoldingRepo(cat_acc, fake)
    plan = cat_acc.holdings.writes.plan_delete(Ref("holding", 1, "c1"))
    assert plan.ref_purges == ()                          # shared hash kept
    assert {f.path for f in plan.file_ops} == {"/a.pdf"}  # only this holding's unique file
    cat_acc.holdings.writes.apply(plan)
    assert cat_acc.holdings.reads.get(1) is None and cat_acc.holdings.reads.get(2) is not None


# ── test policies: drive the authz gate ───────────────────────────────────────────
def test_deny_all_blocks_a_read(cat_acc):
    cat_acc.policy = DenyAll()
    cat_acc.holdings = HoldingRepo(cat_acc, InMemoryHoldingStore())
    with pytest.raises(Denied):
        cat_acc.holdings.reads.get(1)


def test_recording_policy_captures_declared_actions(cat_acc):
    rec = RecordingPolicy()
    cat_acc.policy = rec
    cat_acc.holdings = HoldingRepo(cat_acc, InMemoryHoldingStore())
    cat_acc.holdings.reads.get(1)
    a = rec.actions()[-1]
    assert (a.resource, a.verb, a.mode) == ("holding", "get", AccessMode.READ)


def test_principal_builder():
    p = principal("alice", roles=["editor"], scopes=["write:holding"])
    assert p.id == "alice" and "editor" in p.roles and "write:holding" in p.scopes
