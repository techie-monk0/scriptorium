"""Consolidate duplicate editions into one edition with many holdings (reversible)."""
from __future__ import annotations

from catalogue.db_store import add_alias, init_db
from catalogue.services import contributor_undo as undo
from catalogue.services import edition_consolidate as EC


def _person(db, name):
    return db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid


def _work(db, title, author_pid):
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", wid, title, "english")
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (wid, author_pid))
    return wid


def _edition(db, title, *, isbn=None, year=None, fmt="pdf", file_path="/x.pdf",
             text_pages=0, work_for=None, author=None, volume=None):
    eid = db.execute(
        "INSERT INTO edition (title, isbn, year, volume, structure) VALUES (?,?,?,?, 'single_work')",
        (title, isbn, year, volume)).lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path, holding_type, text_status) "
               "VALUES (?, 'electronic', ?, ?, 'ocr_good')", (eid, file_path, fmt))
    for p in range(text_pages):
        db.execute("INSERT INTO edition_text (edition_id, page, content) VALUES (?,?,?)",
                   (eid, p, f"page {p} text body"))
    if work_for is not None:
        db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
                   (eid, work_for))
    return eid


# ── detection ─────────────────────────────────────────────────────────────────
def test_format_dup_detected_and_merged_with_work_dedup(tmp_path):
    """epub + pdf of one book, each with its OWN duplicate work → one cluster classified
    format_dup; consolidate folds the holdings under one edition AND merges the twin works."""
    db = init_db(tmp_path / "c.db")
    auth = _person(db, "Karl Brunnhölzl")
    w_epub = _work(db, "The Book of Kadam", auth)
    w_pdf = _work(db, "The Book of Kadam", auth)        # the duplicated work (same title+author)
    e_epub = _edition(db, "The Book of Kadam", isbn="978-0-86171-441-4", fmt="epub",
                      file_path="/k.epub", text_pages=10, work_for=w_epub)
    e_pdf = _edition(db, "The Book of Kadam", isbn="9780861714414", fmt="pdf",
                     file_path="/k.pdf", text_pages=10, work_for=w_pdf)
    db.commit()

    clusters = EC.find_clusters(db)
    assert len(clusters) == 1 and clusters[0]["action"] == "format_dup"
    canon = clusters[0]["canonical_id"]
    dups = clusters[0]["dup_ids"]
    assert set([canon, *dups]) == {e_epub, e_pdf}

    res = EC.consolidate(db, canon, dups)
    assert res["status"] == "consolidated"
    # both holdings now live under the one canonical edition…
    assert db.execute("SELECT COUNT(*) FROM holding WHERE edition_id = ?", (canon,)).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM edition WHERE id = ?", (dups[0],)).fetchone()[0] == 0
    # …and the duplicated works collapsed to one.
    assert db.execute("SELECT COUNT(*) FROM work").fetchone()[0] == 1
    assert len(res["merged_works"]) == 1


def test_volume_set_is_not_merged(tmp_path):
    """Same title, but the filenames carry Vol2/Vol3 and the text lengths differ widely →
    classified volume_set, never format_dup."""
    db = init_db(tmp_path / "c.db")
    a = _person(db, "Author")
    e2 = _edition(db, "Sounds of Innate Freedom", isbn="978-1-61429-714-7", fmt="epub",
                  file_path="/Sounds_Vol2.epub", text_pages=100, work_for=_work(db, "S2", a))
    e3 = _edition(db, "Sounds of Innate Freedom", isbn="978-1-61429-716-1", fmt="epub",
                  file_path="/Sounds_Vol3.epub", text_pages=40, work_for=_work(db, "S3", a))
    db.commit()
    c = EC.find_clusters(db)
    assert len(c) == 1 and c[0]["action"] == "volume_set"

    EC.link_volume_set(db, [e2, e3])
    sets = {r[0] for r in db.execute("SELECT volume_set_id FROM edition WHERE id IN (?,?)", (e2, e3))}
    assert len(sets) == 1 and None not in sets           # both share one volume_set_id
    assert db.execute("SELECT COUNT(*) FROM edition").fetchone()[0] == 2   # neither was deleted


def test_revision_flagged_for_review(tmp_path):
    """Same title + same format but a different YEAR → not auto-merged (likely a revision)."""
    db = init_db(tmp_path / "c.db")
    a = _person(db, "A")
    _edition(db, "Stages of Meditation", isbn="111", year=2001, file_path="/a.pdf",
             text_pages=50, work_for=_work(db, "x", a))
    _edition(db, "Stages of Meditation", isbn="222", year=2019, file_path="/b.pdf",
             text_pages=50, work_for=_work(db, "y", a))
    db.commit()
    assert EC.find_clusters(db)[0]["action"] == "review"


# ── reversibility ───────────────────────────────────────────────────────────────
def test_consolidate_is_reversible(tmp_path):
    db = init_db(tmp_path / "c.db")
    auth = _person(db, "A")
    w1 = _work(db, "Same Book", auth)
    w2 = _work(db, "Same Book", auth)
    e1 = _edition(db, "Same Book", isbn="5", fmt="epub", file_path="/a.epub", text_pages=8, work_for=w1)
    e2 = _edition(db, "Same Book", isbn="5", fmt="pdf", file_path="/a.pdf", text_pages=8, work_for=w2)
    db.commit()
    before_editions = db.execute("SELECT COUNT(*) FROM edition").fetchone()[0]
    before_works = db.execute("SELECT COUNT(*) FROM work").fetchone()[0]

    res = EC.consolidate(db, e1, [e2])
    assert db.execute("SELECT COUNT(*) FROM edition").fetchone()[0] == before_editions - 1
    assert db.execute("SELECT COUNT(*) FROM work").fetchone()[0] == before_works - 1

    undo.apply_undo(db, res["undo_token"])
    # both editions, both holdings, both works restored exactly.
    assert db.execute("SELECT COUNT(*) FROM edition").fetchone()[0] == before_editions
    assert db.execute("SELECT COUNT(*) FROM work").fetchone()[0] == before_works
    assert {r[0] for r in db.execute("SELECT id FROM edition")} == {e1, e2}
    assert db.execute("SELECT COUNT(*) FROM holding WHERE edition_id = ?", (e2,)).fetchone()[0] == 1


# ── normalized ISBN audit ─────────────────────────────────────────────────────
def test_normalized_isbn_collision_catches_hyphenation_typo(tmp_path):
    db = init_db(tmp_path / "c.db")
    a = _person(db, "A")
    # Two DIFFERENT books that normalize to the same ISBN (a typo): hyphenated vs plain.
    _edition(db, "Volume Three", isbn="9781614296362", file_path="/v3.pdf", work_for=_work(db, "t3", a))
    _edition(db, "Volume Five", isbn="978-1-61429-636-2", file_path="/v5.pdf", work_for=_work(db, "t5", a))
    db.commit()
    coll = EC.normalized_isbn_collisions(db)
    assert len(coll) == 1
    assert coll[0]["isbn_norm"] == "9781614296362"
    assert coll[0]["same_title"] is False           # different titles ⇒ flagged as a typo


def test_primary_holding_prefers_text_bearing(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = db.execute("INSERT INTO edition (title) VALUES ('x')").lastrowid
    img = db.execute("INSERT INTO holding (edition_id, form, text_status) VALUES (?, 'electronic', 'image_only')",
                     (eid,)).lastrowid
    good = db.execute("INSERT INTO holding (edition_id, form, text_status) VALUES (?, 'electronic', 'ocr_good')",
                      (eid,)).lastrowid
    db.commit()
    assert EC.primary_holding(db, eid) == good       # the readable one, not the lower-id image-only
