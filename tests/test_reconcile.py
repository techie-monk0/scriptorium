"""Tests for the filesystem-reconcile core (catalogue/reconcile.py).

classify/fingerprint/candidates/apply are exercised with synthetic ScannedFile
lists — no real library walk — so the disposition logic is pinned independently
of the (operator-run) scan_dir front end.
"""
from __future__ import annotations

import json
import pytest

from catalogue.db_store import init_db
from catalogue.services import reconcile
from catalogue.services import relink
from catalogue.services.reconcile import ScannedFile, content_fingerprint


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "rec.db")
    yield conn
    conn.close()


def _book(db, *, title, path, fhash, chash, isbn=None, text=None, ts="ocr_good"):
    eid = db.execute("INSERT INTO edition (title, isbn) VALUES (?, ?)",
                     (title, isbn or "")).lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path, file_hash, "
               "content_hash, text_status) VALUES (?, 'electronic', ?, ?, ?, ?)",
               (eid, path, fhash, chash, ts))
    if text:
        db.execute("INSERT INTO edition_text (edition_id, content) VALUES (?, ?)", (eid, text))
    return eid


# ── broken-link detection (file_state / broken_links) ──────────────────────────
def test_file_state_present_missing_none_and_mount_down(tmp_path):
    here = tmp_path / "here.pdf"; here.write_bytes(b"x")
    assert reconcile.file_state(str(here)) == "present"
    assert reconcile.file_state(None) == "none"
    assert reconcile.file_state("") == "none"
    # parent dir present but file gone → genuinely MISSING (deleted/renamed).
    assert reconcile.file_state(str(tmp_path / "gone.pdf")) == "missing"
    # parent ALSO absent (whole mount/folder offline) → 'present' (stay silent, don't
    # cry missing for every book at once).
    assert reconcile.file_state(str(tmp_path / "no" / "dir" / "x.pdf")) == "present"


def test_broken_links_finds_gone_holdings_and_orphan_editions(db, tmp_path):
    here = tmp_path / "ok.pdf"; here.write_bytes(b"%PDF")
    _book(db, title="Present", path=str(here), fhash="h1", chash="b:h1")
    gone_eid = _book(db, title="Gone", path=str(tmp_path / "deleted.pdf"),
                     fhash="h2", chash="b:h2")
    orphan = db.execute("INSERT INTO edition (title) VALUES ('Orphan')").lastrowid
    db.commit()
    bl = reconcile.broken_links(db)
    assert [g["edition_id"] for g in bl["gone"]] == [gone_eid]
    assert all(g["path"] != str(here) for g in bl["gone"])     # present file not flagged
    assert orphan in [o["edition_id"] for o in bl["orphans"]]


def test_classify_does_not_flag_present_unseen_file_as_missing(db, tmp_path):
    """A holding whose file exists but lives OUTSIDE the scanned root must not be
    reported 'missing' just because this scan didn't walk it."""
    elsewhere = tmp_path / "elsewhere.pdf"; elsewhere.write_bytes(b"%PDF")
    _book(db, title="Elsewhere", path=str(elsewhere), fhash="zz", chash="b:zz")
    plan = reconcile.classify(db, [])              # empty scan — nothing walked
    assert not [d for d in plan if d["kind"] in ("missing", "offline")]


# ── fingerprint ───────────────────────────────────────────────────────────────
def test_content_fingerprint_basis():
    long = "the heart sutra " * 10
    assert content_fingerprint(long, "ocr_good", "BYTES").startswith("t:")
    # annotation doesn't change page text → same fingerprint regardless of bytes
    assert content_fingerprint(long, "ocr_good", "AAA") == content_fingerprint(long, "ocr_good", "BBB")
    # image-only / short / poor → byte hash
    assert content_fingerprint(None, "image_only", "BYTES") == "b:BYTES"
    assert content_fingerprint("tiny", "ocr_good", "BYTES") == "b:BYTES"
    assert content_fingerprint("x", "ocr_poor", "BYTES") == "b:BYTES"


# ── classify ──────────────────────────────────────────────────────────────────
def test_classify_unchanged_and_moved(db):
    _book(db, title="A", path="/lib/a.pdf", fhash="H1", chash="t:c1")
    same = [ScannedFile("/lib/a.pdf", "H1", "t:c1")]
    assert classify_kind(db, same) == ["unchanged"]
    moved = [ScannedFile("/lib/sub/a.pdf", "H1", "t:c1")]
    d = reconcile.classify(db, moved)[0]
    assert d["kind"] == "moved" and d["path"] == "/lib/sub/a.pdf"


def test_classify_annotated_vs_content_changed(db):
    _book(db, title="A", path="/lib/a.pdf", fhash="H1", chash="t:c1")
    # same path, new bytes, SAME text fingerprint → annotated
    ann = reconcile.classify(db, [ScannedFile("/lib/a.pdf", "H2", "t:c1")])[0]
    assert ann["kind"] == "annotated"
    # same path, new bytes, DIFFERENT text → content_changed (re-OCR/edit)
    ch = reconcile.classify(db, [ScannedFile("/lib/a.pdf", "H2", "t:c2")])[0]
    assert ch["kind"] == "content_changed"


def test_classify_new_and_content_match(db):
    _book(db, title="Heart Sutra", path="/lib/hs.pdf", fhash="H1", chash="t:cHS")
    # new file whose text fingerprint equals an existing copy → content_match
    cm = reconcile.classify(db, [ScannedFile("/lib/hs_annotated.pdf", "H9", "t:cHS")])[0]
    assert cm["kind"] == "content_match" and cm["candidates"]
    # totally new, no similarity → new
    nw = reconcile.classify(db, [ScannedFile("/lib/zzz.pdf", "H8", "t:cZ",
                                             title="Quantum Gravity")])[0]
    assert nw["kind"] == "new"


def test_classify_new_maybe_reocr_by_title(db):
    _book(db, title="Prasannapada In Clear Words", path="/lib/p.pdf", fhash="H1", chash="t:c1")
    # different bytes/text, but the filename/title overlaps → flagged with candidate
    d = reconcile.classify(db, [ScannedFile(
        "/lib/Prasannapada_v2.pdf", "H2", "t:c2", title="Prasannapada In Clear Words v2")])[0]
    assert d["kind"] == "new_maybe_reocr"
    assert d["candidates"] and d["candidates"][0]["edition_id"]


def test_multivolume_sibling_is_new_not_reocr(db):
    """A new volume of a multi-volume set must NOT be flagged as a re-OCR of its
    siblings. The shared series name ("the new grove dictionary of music and
    musicians") recurs across every volume, so df-weighting collapses it; only the
    distinctive A-Z range words ("liturgy to martini") carry the match — and a brand
    new volume shares none of those with any catalogued sibling. Regression for the
    real New Grove vol-15 false 'maybe a re-OCR of…' flag. The boilerplate discount
    strengthens with the number of siblings — exactly the case that triggers the bug
    (the real set has 24 volumes), so the test populates a realistically-sized set."""
    base = "The New Grove Dictionary of Music and Musicians 2ed"
    for n, rng in [("01", "A to Aristotle"), ("02", "Aristoxenus to Bax"),
                   ("03", "Baxter to Borosini"), ("05", "Canon to Classic Rock"),
                   ("08", "Egypt to Flor"), ("10", "Glinka to Harp"),
                   ("13", "Jennens to Kuerti"), ("17", "Monnet to Nirvana")]:
        _book(db, title=f"{n} - {rng} - {base}", path=f"/lib/grove/{n}.pdf",
              fhash=f"H{n}", chash=f"t:c{n}")
    # New, uncatalogued volume 15 — overlaps siblings only on boilerplate.
    sibling = reconcile.classify(db, [ScannedFile(
        "/lib/grove/15.pdf", "H15", "t:c15",
        title=f"15 - Liturgy to Martini - {base}")])[0]
    assert sibling["kind"] == "new"
    assert not sibling["candidates"]
    # A genuine re-OCR of vol 13 (shares the distinctive range words) still matches.
    reocr = reconcile.classify(db, [ScannedFile(
        "/lib/grove/13_reocr.pdf", "H13b", "t:c13b",
        title=f"13 - Jennens to Kuerti - {base} Second Edition")])[0]
    assert reocr["kind"] == "new_maybe_reocr"
    assert reocr["candidates"] and reocr["candidates"][0]["title"].startswith("13 -")


def test_classify_missing_and_offline(db, tmp_path):
    _book(db, title="Gone", path=str(tmp_path / "gone.pdf"), fhash="H1", chash="t:c1")
    placeholder = tmp_path / "evicted.pdf"; placeholder.write_bytes(b"")          # 0-byte stub
    _book(db, title="Evicted", path=str(placeholder), fhash="H2", chash="t:c2")
    plan = reconcile.classify(db, [])                  # scan saw nothing
    kinds = {d["path"]: d["kind"] for d in plan}
    assert kinds[str(tmp_path / "gone.pdf")] == "missing"
    assert kinds[str(placeholder)] == "offline"


# ── candidates ────────────────────────────────────────────────────────────────
def test_find_candidates_isbn_and_title(db):
    _book(db, title="Introduction to the Middle Way", path="/l/a.pdf",
          fhash="H1", chash="t:1", isbn="9781559393324")
    by_isbn = reconcile.find_candidate_editions(db, isbn="9781559393324")
    assert by_isbn and by_isbn[0]["why"] == ["isbn"]
    by_title = reconcile.find_candidate_editions(db, title="Introduction to the Middle Way")
    assert by_title and "title" in by_title[0]["why"]


# ── apply ─────────────────────────────────────────────────────────────────────
def test_reconcile_auto_applies_moved_enqueues_new(db):
    _book(db, title="A", path="/lib/a.pdf", fhash="H1", chash="t:c1")
    scanned = [ScannedFile("/lib/moved/a.pdf", "H1", "t:c1"),       # moved → auto
               ScannedFile("/lib/new.pdf", "H7", "t:c7", title="Brand New")]  # new → enqueue
    summ = reconcile.reconcile(db, scanned)
    assert summ["applied"] == 1 and summ["enqueued"] == 1
    assert db.execute("SELECT file_path FROM holding WHERE file_hash='H1'").fetchone()[0] == "/lib/moved/a.pdf"
    assert db.execute("SELECT COUNT(*) FROM review_queue WHERE item_type='ingest'").fetchone()[0] == 1


def test_reconcile_rescan_does_not_duplicate_pending(db):
    # Re-running the scan while a 'new' item is still pending must not pile up
    # duplicate ingest rows for the same path (the Scan-page duplicates bug).
    scanned = [ScannedFile("/lib/new.pdf", "H7", "t:c7", title="Brand New")]
    reconcile.reconcile(db, scanned)
    summ = reconcile.reconcile(db, scanned)          # operator re-scans
    assert summ["enqueued"] == 1                      # still counted, but...
    rows = db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE item_type='ingest' AND status='pending'"
    ).fetchone()[0]
    assert rows == 1                                  # ...one pending row, not two

    # A resolved item for the same path frees the path; a later re-scan of a
    # genuinely new file there enqueues afresh rather than reviving the old one.
    iid = db.execute("SELECT id FROM review_queue WHERE item_type='ingest'").fetchone()[0]
    db.execute("UPDATE review_queue SET status='resolved' WHERE id=?", (iid,))
    reconcile.reconcile(db, [ScannedFile("/lib/new.pdf", "H8", "t:c8", title="Brand New")])
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE item_type='ingest' AND status='pending'"
    ).fetchone()[0] == 1


def test_ignore_persists_across_rescans(db):
    # Operator ignores a new file; classify must never surface it again, even on
    # a fresh scan and even if the file later moves (hash-matched) or is
    # re-OCR'd in place (path-matched).
    sf = ScannedFile("/lib/junk.pdf", "H7", "t:c7", title="Junk")
    reconcile.reconcile(db, [sf])
    iid = db.execute("SELECT id FROM review_queue WHERE item_type='ingest'").fetchone()[0]
    reconcile.apply_decision(db, iid, "ignore")
    assert db.execute("SELECT COUNT(*) FROM ingest_ignore").fetchone()[0] == 1

    # re-scan of the same file → no disposition emitted at all
    assert classify_kind(db, [sf]) == []
    # same bytes, moved to a new path → still ignored (hash match)
    assert classify_kind(db, [ScannedFile("/lib/moved/junk.pdf", "H7", "t:c7")]) == []
    # re-OCR'd in place (new bytes, same path) → still ignored (path match)
    assert classify_kind(db, [ScannedFile("/lib/junk.pdf", "H9", "t:c9")]) == []
    # nothing re-enqueued
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE item_type='ingest' AND status='pending'"
    ).fetchone()[0] == 0


def test_new_file_lands_in_books_review_pile(db):
    # Resolving a scanned file as a new book must NOT silently catalogue it as
    # finished — it goes into the Books review pile (a work_detection row) so the
    # operator confirms details / catches duplicates first.
    iid = db.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('ingest', ?)",
                     (json.dumps({"kind": "new", "path": "/lib/Some New Book.pdf",
                                  "file_hash": "H9", "content_hash": "t:c9",
                                  "title": "Some New Book", "isbn": "9780000000001"}),)).lastrowid
    out = reconcile.apply_decision(db, iid, "new")
    eid = out["edition_id"]
    row = db.execute("SELECT kind, payload_json FROM work_detection WHERE edition_id=?",
                     (eid,)).fetchone()
    assert row is not None and row[0] == "single"          # appears in the review worklist
    payload = json.loads(row[1])
    assert payload["source"] == "scan-new"
    assert payload["title"]["english"] == "Some New Book"
    assert payload["isbn"] == "9780000000001"
    assert payload["file"]["holding_id"] == out["holding_id"]
    assert "applied" not in payload                          # NOT pre-marked done


def test_apply_decision_distinct_and_add_copy(db):
    eid = _book(db, title="Existing", path="/lib/e.pdf", fhash="H1", chash="t:c1")
    # enqueue a 'new' ingest item, then resolve it as a distinct new book
    iid = db.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('ingest', ?)",
                     (json.dumps({"kind": "new", "path": "/lib/Brand New.pdf",
                                  "file_hash": "H9", "content_hash": "t:c9"}),)).lastrowid
    out = reconcile.apply_decision(db, iid, "distinct")
    assert db.execute("SELECT file_hash FROM holding WHERE id=?", (out["holding_id"],)).fetchone()[0] == "H9"
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (iid,)).fetchone()[0] == "resolved"

    # add_copy onto the existing edition
    iid2 = db.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('ingest', ?)",
                      (json.dumps({"kind": "content_match", "path": "/lib/e_copy.pdf",
                                   "file_hash": "H1b", "content_hash": "t:c1"}),)).lastrowid
    out2 = reconcile.apply_decision(db, iid2, "add_copy", target_edition_id=eid)
    assert db.execute("SELECT edition_id FROM holding WHERE id=?", (out2["holding_id"],)).fetchone()[0] == eid


def classify_kind(db, scanned):
    return [d["kind"] for d in reconcile.classify(db, scanned)]


# ── Auto-relink moved files (broken-link healing) ──────────────────────────────
# These pin the decision logic. _fingerprint_file is monkeypatched so the tests
# don't need a real PDF/EPUB text extractor; real (empty) files on disk satisfy
# the stat-only walk and the os.path.isfile guard.

def _fp_map(monkeypatch, mapping):
    """Patch relink._signature_of_file with a path -> (wire_signature, byte_hash) map."""
    from catalogue.db_store import signature as sig
    def fake(p):
        v = mapping.get(str(p))
        if v is None:
            return (None, None)
        wire, bh = v
        return (sig.parse(wire), bh)
    monkeypatch.setattr(relink, "_signature_of_file", fake)


def test_relink_auto_repoints_unique_text_match_byte_hash_stale(db, tmp_path, monkeypatch):
    """The Yamantaka case: file MOVED + cloud REWROTE bytes (byte-hash stale), but the
    text fingerprint is unchanged → silent auto-repoint, file_hash refreshed,
    content_hash kept, undo token issued."""
    root = tmp_path / "lib"; (root / "old").mkdir(parents=True); (root / "new").mkdir()
    old = root / "old" / "Book.pdf"          # recorded path — now GONE
    new = root / "new" / "Book.pdf"          # same basename, moved here
    new.write_bytes(b"%PDF rewritten by kdrive")
    eid = _book(db, title="Book", path=str(old), fhash="OLDBYTES", chash="t:textfp")
    db.commit()
    _fp_map(monkeypatch, {str(new): ("t:textfp", "NEWBYTES")})

    res = relink.relink_moved(db, [str(root)])
    assert [r["holding_id"] for r in res["relinked"]]            # one relink
    assert res["undo_token"] is not None
    row = db.execute("SELECT file_path, file_hash, content_hash FROM holding "
                     "WHERE edition_id = ?", (eid,)).fetchone()
    assert row[0] == str(new)                 # repointed
    assert row[1] == "NEWBYTES"               # rehashed to the bytes on disk
    assert row[2] == "t:textfp"               # content fingerprint preserved
    assert reconcile.broken_links(db)["gone"] == []             # link healed


def test_relink_suggests_not_auto_when_fingerprint_differs(db, tmp_path, monkeypatch):
    """A same-name file whose text does NOT match → suggestion only, never silent."""
    root = tmp_path / "lib"; (root / "new").mkdir(parents=True)
    new = root / "new" / "Book.pdf"; new.write_bytes(b"%PDF other book")
    eid = _book(db, title="Book", path=str(root / "Book.pdf"), fhash="H", chash="t:mine")
    db.commit()
    _fp_map(monkeypatch, {str(new): ("t:DIFFERENT", "B")})

    res = relink.relink_moved(db, [str(root)])
    assert res["relinked"] == []
    hid = db.execute("SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]
    assert res["suggestions"][hid] == [str(new)]
    # holding untouched
    assert db.execute("SELECT file_path FROM holding WHERE id=?", (hid,)).fetchone()[0] \
        == str(root / "Book.pdf")


def test_relink_byte_based_holding_never_auto(db, tmp_path, monkeypatch):
    """Holding with a byte-based content_hash ('b:…', no trustworthy text) is never
    silently repointed — text confirmation is required for auto."""
    root = tmp_path / "lib"; (root / "x").mkdir(parents=True)
    new = root / "x" / "Book.pdf"; new.write_bytes(b"%PDF")
    eid = _book(db, title="Book", path=str(root / "Book.pdf"), fhash="H", chash="b:H")
    db.commit()
    _fp_map(monkeypatch, {str(new): ("b:H", "H")})   # even identical bytes
    res = relink.relink_moved(db, [str(root)])
    assert res["relinked"] == []
    hid = db.execute("SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]
    assert res["suggestions"].get(hid) == [str(new)]   # offered, not applied


def test_relink_ambiguous_two_text_matches_suggests_both(db, tmp_path, monkeypatch):
    """Two same-name files both matching the fingerprint → ambiguous → suggest both."""
    root = tmp_path / "lib"; (root / "a").mkdir(parents=True); (root / "b").mkdir()
    p1 = root / "a" / "Book.pdf"; p1.write_bytes(b"%PDF1")
    p2 = root / "b" / "Book.pdf"; p2.write_bytes(b"%PDF2")
    eid = _book(db, title="Book", path=str(root / "Book.pdf"), fhash="H", chash="t:fp")
    db.commit()
    _fp_map(monkeypatch, {str(p1): ("t:fp", "B1"), str(p2): ("t:fp", "B2")})
    res = relink.relink_moved(db, [str(root)])
    assert res["relinked"] == []
    hid = db.execute("SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]
    assert sorted(res["suggestions"][hid]) == sorted([str(p1), str(p2)])


def test_relink_no_basename_candidate_stays_broken(db, tmp_path, monkeypatch):
    root = tmp_path / "lib"; root.mkdir()
    (root / "Unrelated.pdf").write_bytes(b"%PDF")
    eid = _book(db, title="Book", path=str(root / "Book.pdf"), fhash="H", chash="t:fp")
    db.commit()
    res = relink.relink_moved(db, [str(root)])
    assert res["relinked"] == [] and res["suggestions"] == {}
    assert reconcile.broken_links(db)["gone"]                  # still broken


def test_relink_skips_file_serving_a_present_holding(db, tmp_path, monkeypatch):
    """A same-name file that is ALREADY a present holding's file must not be stolen."""
    root = tmp_path / "lib"; (root / "keep").mkdir(parents=True)
    present = root / "keep" / "Book.pdf"; present.write_bytes(b"%PDF")
    _book(db, title="Owner", path=str(present), fhash="H1", chash="t:owner")     # present
    eid = _book(db, title="Broken", path=str(root / "Book.pdf"), fhash="H2", chash="t:owner")
    db.commit()
    _fp_map(monkeypatch, {str(present): ("t:owner", "H1")})
    res = relink.relink_moved(db, [str(root)])
    assert res["relinked"] == []                  # the present file is off-limits
    hid = db.execute("SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]
    assert res["suggestions"].get(hid) in (None, [])


def test_relink_undo_restores_old_path_and_hash(db, tmp_path, monkeypatch):
    root = tmp_path / "lib"; (root / "new").mkdir(parents=True)
    new = root / "new" / "Book.pdf"; new.write_bytes(b"%PDF")
    eid = _book(db, title="Book", path=str(root / "Book.pdf"), fhash="OLD", chash="t:fp")
    db.commit()
    _fp_map(monkeypatch, {str(new): ("t:fp", "NEW")})
    res = relink.relink_moved(db, [str(root)])
    token = res["undo_token"]; assert token

    from catalogue.services import contributor_undo as U
    out = U.apply_undo(db, token)
    assert "error" not in out
    row = db.execute("SELECT file_path, file_hash FROM holding WHERE edition_id=?",
                     (eid,)).fetchone()
    assert row[0] == str(root / "Book.pdf") and row[1] == "OLD"   # fully reverted


def test_relink_to_operator_confirm_repoints_and_journals(db, tmp_path, monkeypatch):
    root = tmp_path / "lib"; root.mkdir()
    chosen = root / "Chosen.pdf"; chosen.write_bytes(b"%PDF")
    eid = _book(db, title="Book", path=str(root / "Book.pdf"), fhash="OLD", chash="t:fp")
    hid = db.execute("SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]
    db.commit()
    _fp_map(monkeypatch, {str(chosen): ("t:fp", "NEW")})
    out = relink.relink_to(db, hid, str(chosen))
    assert out["undo_token"]
    assert db.execute("SELECT file_path FROM holding WHERE id=?", (hid,)).fetchone()[0] \
        == str(chosen)
    with pytest.raises(ValueError):                # nonexistent path refused
        relink.relink_to(db, hid, str(root / "nope.pdf"))


def test_relink_uses_injected_resolver_abstraction(db, tmp_path):
    """The service layer depends only on the MoveResolver abstraction: a fake resolver
    that knows nothing about fingerprints drives a repoint just the same."""
    eid = _book(db, title="Book", path=str(tmp_path / "Book.pdf"), fhash="OLD", chash="t:fp")
    hid = db.execute("SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]
    db.commit()

    class FakeResolver(relink.MoveResolver):
        def build_pool(self, db, roots):
            return {"sentinel": True}
        def resolve(self, holding, pool):
            assert pool == {"sentinel": True}
            return relink.Resolution(confirmed=relink.Match("/somewhere/Book.pdf", "NEWHASH"))

    res = relink.relink_moved(db, [str(tmp_path)], resolver=FakeResolver())
    assert [r["new_path"] for r in res["relinked"]] == ["/somewhere/Book.pdf"]
    row = db.execute("SELECT file_path, file_hash FROM holding WHERE id=?", (hid,)).fetchone()
    assert row == ("/somewhere/Book.pdf", "NEWHASH")


# ── Signature abstraction (format is owned only by signature.py) ───────────────
def test_signature_abstraction_hides_wire_format():
    from catalogue.db_store import signature as sig
    long = "the heart sutra " * 10
    s = sig.of(long, "ocr_good", "BYTES")
    assert s.is_text and s.matches(sig.of(long, "ocr_good", "OTHERBYTES"))  # text-based, byte-agnostic
    b = sig.of(None, "image_only", "BYTES")
    assert (not b.is_text) and b.matches("b:BYTES")        # byte-based; matches a wire string too
    assert sig.parse(None) is None and sig.of(None, None, None) is None
    assert not s.matches(None) and not s.matches(b)

# ── prune_stale_ingest (Reconcile page self-heals) ─────────────────────────────
def test_prune_stale_ingest_drops_obsolete_items(db, tmp_path):
    """A 'new'/'content_match' item whose path is now a holding's current file, and a
    'missing' item whose holding is now present, are pruned; real ones survive."""
    import json
    present = tmp_path / "moved" / "Book.pdf"; present.parent.mkdir(parents=True)
    present.write_bytes(b"%PDF")
    eid = _book(db, title="Book", path=str(present), fhash="H", chash="t:c")   # holding now here
    hid = db.execute("SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]

    def enq(kind, path, holding_id=None):
        return db.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('ingest', ?)",
                          (json.dumps({"kind": kind, "path": path, "holding_id": holding_id}),)).lastrowid
    stale_new = enq("new", str(present))                       # path == holding's file → drop
    stale_missing = enq("missing", "/old/Book.pdf", hid)      # holding now present → drop
    real_new = enq("new", str(tmp_path / "really_new.pdf"))   # unknown file → keep
    db.commit()

    n = reconcile.prune_stale_ingest(db)
    assert n == 2
    left = {r[0] for r in db.execute(
        "SELECT id FROM review_queue WHERE item_type='ingest' AND status='pending'")}
    assert left == {real_new}


def test_prune_keeps_content_changed_at_holding_path(db, tmp_path):
    """An in-place edit (content_changed) shares the holding's path on purpose — it
    must NOT be pruned just because its path matches a holding's current file."""
    import json
    f = tmp_path / "edited.pdf"; f.write_bytes(b"%PDF new bytes")
    eid = _book(db, title="Edited", path=str(f), fhash="OLD", chash="t:old")
    hid = db.execute("SELECT id FROM holding WHERE edition_id=?", (eid,)).fetchone()[0]
    rid = db.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('ingest', ?)",
        (json.dumps({"kind": "content_changed", "path": str(f), "holding_id": hid,
                     "file_hash": "NEW", "content_hash": "t:new"}),)).lastrowid
    db.commit()
    assert reconcile.prune_stale_ingest(db) == 0
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (rid,)).fetchone()[0] == "pending"
