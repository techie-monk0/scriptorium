"""Tests for filename → edition-basics detection (catalogue/domain/detect.py) and the
/works/detect/<eid>/detect route.

The Anna's Archive long form packs title/author/edition/publisher/ISBN into the file
name; these pin the parser, the conservative apply policy (overwrite title, fill-if-empty
publisher/year, alias a second ISBN, reuse-link known authors), and the web flow.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import init_db, fold_key
from catalogue.db_store import contributor_store as cs
from catalogue.services import detect


ANNAS_A = ("How to Meditate on the Stages of the Path -- Kathleen McDonald -- PS, 2024 "
           "-- Wisdom Publications -- 9781614298939 "
           "-- c35fa8877a12510b2d48e05376bbf949 -- Anna’s Archive.epub")
ANNAS_B = ("Teachings From the Medicine Buddha Retreat_ Land of Medicine -- Lama Zopa Rinpoche "
           "-- First Edition, 2009 -- Lama Yeshe Wisdom Archive -- 9781891868238 "
           "-- c8d070e5f682c5b674e2ff20dfb32200 -- Anna’s Archive.epub")


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "d.db")
    yield conn
    conn.close()


def _person(db, name, *, aliases=()):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid
    for a in (name,) + tuple(aliases):
        db.execute("INSERT INTO person_alias (person_id, text, normalized_key) VALUES (?, ?, ?)",
                   (pid, a, fold_key(a)))
    return pid


def _edition(db, **cols):
    cols.setdefault("title", "placeholder")
    keys = ", ".join(cols)
    qs = ", ".join("?" * len(cols))
    eid = db.execute(f"INSERT INTO edition ({keys}) VALUES ({qs})", list(cols.values())).lastrowid
    return eid


# ── parser ──────────────────────────────────────────────────────────────────
def test_annas_full_record():
    d = detect.merge(detect.detect(ANNAS_A))
    assert d.title == "How to Meditate on the Stages of the Path"
    assert d.subtitle is None
    assert d.authors == ["Kathleen McDonald"]
    assert d.year == 2024 and d.edition_statement == "PS"
    assert d.publisher == "Wisdom Publications"
    assert d.isbn == "9781614298939"
    assert d.source == "annas_archive" and d.confidence > 0.9


def test_annas_subtitle_split():
    d = detect.detect(ANNAS_B)[0]
    assert d.title == "Teachings From the Medicine Buddha Retreat"
    assert d.subtitle == "Land of Medicine"
    assert d.authors == ["Lama Zopa Rinpoche"]
    assert d.year == 2009 and d.edition_statement == "First Edition"
    assert d.publisher == "Lama Yeshe Wisdom Archive"


def test_non_annas_filename_is_not_detected():
    assert detect.detect("The-Torch-for-the-Definitive-Meaning.pdf") == []
    assert detect.detect("Tantra/Creation and Completion -- Jamgon Kongtrul.pdf") == []  # no signature


def test_multiple_authors_semicolon_and_lastfirst():
    fn = ("A Book -- Smith, John; Doe, Jane -- 2011 -- Pub House -- 9780262033848 "
          "-- aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -- Anna's Archive.pdf")
    d = detect.detect(fn)[0]
    assert d.authors == ["John Smith", "Jane Doe"]            # 'Last, First' → 'First Last'
    assert d.isbn == "9780262033848"


def test_lastfirst_pairs_and_trailing_date_stripped():
    # Real file: 'Landaw, Jonathan, Weber, Andy, 1951-' → two people, date dropped.
    fn = ("Images of enlightenment _ Tibetan art in practice "
          "-- Landaw, Jonathan, Weber, Andy, 1951- -- 1st ed_, Ithaca, N_Y, New York State, 1993 "
          "-- Ithaca, -- isbn13 9781559390248 -- 3c9b69373c0bbb9af9ce065f0e85fd69 -- Anna’s Archive.pdf")
    d = detect.detect(fn)[0]
    assert d.authors == ["Jonathan Landaw", "Andy Weber"]
    assert d.title == "Images of enlightenment" and d.subtitle == "Tibetan art in practice"
    assert d.isbn == "9781559390248" and d.year == 1993


def test_single_author_unchanged():
    assert detect.detect(
        "T -- Kathleen McDonald -- 2024 -- Pub -- 9781614298939 "
        "-- " + "a"*32 + " -- Anna's Archive.epub")[0].authors == ["Kathleen McDonald"]


def test_labelled_isbn_is_parsed():
    # Anna's Archive labels the field 'isbn13 …' — the label's '13' must not corrupt it.
    for label in ("isbn13 9780898002447", "isbn10 089800244X", "ISBN: 978-0-89800-244-7"):
        fn = f"T -- A -- 2000 -- Pub -- {label} -- {'a'*32} -- Anna's Archive.pdf"
        assert detect.detect(fn)[0].isbn == "9780898002447", label


def test_publisher_is_segment_closest_to_isbn():
    # Real messy file: author-slot holds the publisher, a series string precedes the
    # place/publisher. Publisher should be the segment just before the ISBN, not the series.
    fn = ("Holy Places of the Buddha Crystal Mirror 9 -- Dharma Publishing "
          "-- Crystal mirror series ;, v_ 9, Berkeley, CA, California, -- Berkeley, CA _ Dharma "
          "-- isbn13 9780898002447 -- 9f3cca10edcb5f1e34966f4ca96c78d1 -- Anna’s Archive.pdf")
    d = detect.detect(fn)[0]
    assert d.isbn == "9780898002447"
    assert d.publisher == "Berkeley, CA _ Dharma"
    assert "Crystal mirror series" not in (d.publisher or "")


def test_missing_middle_fields_tolerated():
    d = detect.detect("Just A Title -- An Author -- Anna's Archive.epub")[0]
    assert d.title == "Just A Title" and d.authors == ["An Author"]
    assert d.isbn is None and d.year is None and d.publisher is None


def test_md5_segment_never_taken_as_publisher():
    d = detect.detect("T -- A -- 2000 -- deadbeefdeadbeefdeadbeefdeadbeef -- Anna's Archive.pdf")[0]
    assert d.publisher is None and d.year == 2000


def test_merge_prefers_highest_confidence_and_first_nonempty():
    a = detect.Detection(source="x", confidence=0.5, title="Low", publisher="P")
    b = detect.Detection(source="y", confidence=0.9, title="High")
    m = detect.merge([a, b])
    assert m.title == "High"            # higher confidence wins
    assert m.publisher == "P"           # filled from the only hit that has it


# ── enrich_with_isbn (ISBN-sourced metadata wins over the filename) ────────────
def test_enrich_prefers_isbn_title_and_authors_keeps_filename_subtitle():
    fdet = detect.merge(detect.detect(ANNAS_B))          # subtitle 'Land of Medicine'
    meta = {"title": "Teachings from the Medicine Buddha Retreat",
            "authors": ["Lama Zopa Rinpoche"], "publishers": ["Lama Yeshe Wisdom Archive"],
            "publish_date": "2009", "isbn_13": "9781891868238", "source": "openlibrary"}
    out = detect.enrich_with_isbn(fdet, "9781891868238", lookup=lambda i: meta)
    assert out.title == "Teachings from the Medicine Buddha Retreat"   # from the ISBN record
    assert out.authors == ["Lama Zopa Rinpoche"]
    assert out.subtitle == "Land of Medicine"            # filename fills the gap ISBN lacks
    assert out.source.startswith("isbn:")


def test_enrich_miss_or_no_isbn_returns_filename_unchanged():
    fdet = detect.merge(detect.detect(ANNAS_A))
    assert detect.enrich_with_isbn(fdet, "9781614298939", lookup=lambda i: None) is fdet
    assert detect.enrich_with_isbn(fdet, None, lookup=lambda i: {"title": "X"}) is fdet
    assert detect.enrich_with_isbn(fdet, "9781614298939", lookup=None) is fdet


def test_enrich_from_isbn_when_filename_undetected():
    out = detect.enrich_with_isbn(None, "9781614298939", lookup=lambda i: {
        "title": "Real Title", "authors": ["Ann Author"], "publishers": ["P"],
        "publish_date": "March 2001", "isbn_13": "9781614298939"})
    assert out.title == "Real Title" and out.authors == ["Ann Author"] and out.year == 2001


def test_enrich_ignores_isbn_when_title_unrelated_to_filename():
    """A WRONG / shared-across-volumes ISBN (one ISBN stamped on every volume of a set)
    must NOT copy one volume's title onto its siblings — the bulk-detect clobber bug.
    When the ISBN-resolved title is unrelated to the filename's own title, the lookup is
    discarded and detection falls back to filename-only (so authors/year don't leak either)."""
    # A volume whose filename carries a sibling volume's ISBN; the lookup returns the
    # OTHER volume's metadata.
    fdet = detect.merge(detect.detect(
        "Music in the Nineteenth Century -- Richard Taruskin -- 2010 -- Oxford "
        "-- 9780195384819 -- bdddd73c1367e78384ffde8f264af195 -- Anna’s Archive.epub"))
    wrong = {"title": "Music from the Earliest Notations to the Sixteenth Century",
             "authors": ["Somebody Else"], "publishers": ["Oxford"],
             "publish_date": "2005", "isbn_13": "9780195384819", "source": "openlibrary"}
    out = detect.enrich_with_isbn(fdet, "9780195384819", lookup=lambda i: wrong)
    assert out is fdet                                   # lookup discarded entirely
    assert out.title == "Music in the Nineteenth Century"
    assert out.authors == ["Richard Taruskin"]          # ISBN's wrong author didn't leak


def test_enrich_accepts_isbn_title_that_expands_truncated_filename():
    """The legit case the guard must NOT break: the filename TRUNCATES the title and the
    ISBN record gives the fuller form — one is a substring of the other → still wins."""
    fdet = detect.merge(detect.detect(
        "The Blazing Inner Fire of Bliss and Emptiness -- Tsongkhapa -- 2021 -- Wisdom "
        "-- 9781614295440 -- aa11bb22cc33dd44ee55ff6600112233 -- Anna’s Archive.epub"))
    meta = {"title": "The Blazing Inner Fire of Bliss and Emptiness: An Experiential "
            "Commentary on the Practice of the Six Yogas of Naropa",
            "authors": ["Tsongkhapa"], "isbn_13": "9781614295440", "source": "openlibrary"}
    out = detect.enrich_with_isbn(fdet, "9781614295440", lookup=lambda i: meta)
    assert out.title.startswith("The Blazing Inner Fire of Bliss and Emptiness: An Experiential")


# ── resolve_person ────────────────────────────────────────────────────────────
def test_resolve_person_unique_match(db):
    pid = _person(db, "Kathleen McDonald")
    assert detect.resolve_person(db, "Kathleen McDonald") == pid
    assert detect.resolve_person(db, "KATHLEEN MCDONALD") == pid    # case-fold insensitive


def test_resolve_person_none_or_ambiguous(db):
    assert detect.resolve_person(db, "Nobody Here") is None
    _person(db, "Common Name")
    _person(db, "Common Name")                                       # two → ambiguous
    assert detect.resolve_person(db, "Common Name") is None


# ── apply_to_edition ──────────────────────────────────────────────────────────
def test_apply_overwrites_title_and_links_known_author(db):
    pid = _person(db, "Kathleen McDonald")
    eid = _edition(db, title="how to meditate -- kathleen mcdonald -- annas")
    det = detect.merge(detect.detect(ANNAS_A))
    out = detect.apply_to_edition(db, eid, det)
    row = db.execute("SELECT title, publisher, year, isbn FROM edition WHERE id=?", (eid,)).fetchone()
    assert row[0] == "How to Meditate on the Stages of the Path"
    assert row[1] == "Wisdom Publications" and row[2] == 2024 and row[3] == "9781614298939"
    assert cs.edition_author_ids(db, eid) == [pid]
    assert out["linked"]["authors"] == [{"id": pid, "name": "Kathleen McDonald"}]
    assert out["unresolved"]["authors"] == []
    assert out["applied"]["title"]["new"] == "How to Meditate on the Stages of the Path"


def test_apply_preserves_curated_title_and_suggests(db):
    # A richer curated title (filename truncates it) must NOT be clobbered.
    eid = _edition(db, title="How to Meditate on the Stages of the Path: A Guide to the Lamrim")
    det = detect.merge(detect.detect(ANNAS_A))
    out = detect.apply_to_edition(db, eid, det)
    assert db.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] \
        == "How to Meditate on the Stages of the Path: A Guide to the Lamrim"
    assert "title" not in out["applied"]
    assert out["applied"]["title_suggestion"]["detected"] \
        == "How to Meditate on the Stages of the Path"


def test_apply_overwrites_raw_filename_title(db):
    eid = _edition(db, title=ANNAS_A)               # the literal filename used as a title
    det = detect.merge(detect.detect(ANNAS_A))
    out = detect.apply_to_edition(db, eid, det)
    assert db.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] \
        == "How to Meditate on the Stages of the Path"
    assert out["applied"]["title"]["new"] == "How to Meditate on the Stages of the Path"


def test_apply_does_not_clobber_curated_publisher_year(db):
    eid = _edition(db, title="x", publisher="Curated Press", year=1999)
    det = detect.merge(detect.detect(ANNAS_A))
    out = detect.apply_to_edition(db, eid, det)
    row = db.execute("SELECT publisher, year FROM edition WHERE id=?", (eid,)).fetchone()
    assert row == ("Curated Press", 1999)                # fill-if-empty left them alone
    assert "publisher" not in out["applied"] and "year" not in out["applied"]


def test_apply_isbn_becomes_alias_when_primary_differs(db):
    eid = _edition(db, title="x", isbn="9780000000000")
    det = detect.merge(detect.detect(ANNAS_A))
    out = detect.apply_to_edition(db, eid, det)
    assert db.execute("SELECT isbn FROM edition WHERE id=?", (eid,)).fetchone()[0] == "9780000000000"
    alias = db.execute("SELECT isbn, note FROM edition_isbn WHERE edition_id=?", (eid,)).fetchone()
    assert alias[0] == "9781614298939" and "filename" in alias[1]
    assert out["applied"]["isbn_alias"] == "9781614298939"


def test_apply_unknown_author_is_unresolved_not_created(db):
    eid = _edition(db, title="x")
    det = detect.merge(detect.detect(ANNAS_A))
    out = detect.apply_to_edition(db, eid, det)
    assert out["unresolved"]["authors"] == ["Kathleen McDonald"]
    assert cs.edition_author_ids(db, eid) == []          # nothing auto-created
    assert db.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0


# ── web route ─────────────────────────────────────────────────────────────────
@pytest.fixture
def app(tmp_path):
    from catalogue.webui.web import create_app
    a = create_app(tmp_path / "web.db")
    a.testing = True
    a.config["ISBN_LOOKUP"] = lambda isbn: None      # never hit the network in tests
    return a


def _seed_book(app, filename, author_name=None):
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    # Seed the title as the raw filename (the common backlog state) so Detect overwrites it.
    eid = conn.execute("INSERT INTO edition (title) VALUES (?)", (filename,)).lastrowid
    conn.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
                 (eid, "/lib/" + filename))
    if author_name:
        pid = conn.execute("INSERT INTO person (primary_name) VALUES (?)", (author_name,)).lastrowid
        conn.execute("INSERT INTO person_alias (person_id, text, normalized_key) VALUES (?,?,?)",
                     (pid, author_name, fold_key(author_name)))
    conn.commit(); conn.close()
    return eid


def test_route_detects_applies_and_links(app):
    eid = _seed_book(app, ANNAS_A, author_name="Kathleen McDonald")
    with app.test_client() as c:
        r = c.post(f"/works/detect/{eid}/detect",
                   data=json.dumps({}), content_type="application/json")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["detected"]
    assert body["applied"]["title"]["new"] == "How to Meditate on the Stages of the Path"
    assert [x["name"] for x in body["linked"]["authors"]] == ["Kathleen McDonald"]
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    assert conn.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] \
        == "How to Meditate on the Stages of the Path"
    conn.close()


def test_route_form_post_applies_and_rerenders_card(app):
    # The UI submits a plain form (not JSON); the route must apply and return the
    # re-rendered edit card so the book-browser host can swap it in.
    eid = _seed_book(app, ANNAS_A, author_name="Kathleen McDonald")
    with app.test_client() as c:
        r = c.post(f"/works/detect/{eid}/detect")            # form-encoded, no JSON
    assert r.status_code == 200
    assert b"How to Meditate on the Stages of the Path" in r.data   # cleaned title in the card
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    assert conn.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] \
        == "How to Meditate on the Stages of the Path"
    conn.close()


def test_edit_card_shows_live_isbn_and_filename_author_chips(app):
    # Regression for the "no isbn / no author" report: the edit card must show the LIVE
    # edition.isbn (not a stale detection snapshot) and surface filename author names —
    # one-click for an existing person, picker-prefill chip for a new one.
    from catalogue.db_store import connect, fold_key
    conn = connect(app.config["DB_PATH"])
    eid = conn.execute("INSERT INTO edition (title, isbn) VALUES ('Images of enlightenment', "
                       "'9781559390248')").lastrowid
    conn.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
                 (eid, "/lib/Images of enlightenment _ Tibetan art in practice -- "
                       "Landaw, Jonathan, Weber, Andy, 1951- -- 1993 -- Ithaca, -- "
                       "isbn13 9781559390248 -- " + "a" * 32 + " -- Anna’s Archive.pdf"))
    pid = conn.execute("INSERT INTO person (primary_name) VALUES ('Jonathan Landaw')").lastrowid
    conn.execute("INSERT INTO person_alias (person_id, text, normalized_key) VALUES (?, ?, ?)",
                 (pid, "Jonathan Landaw", fold_key("Jonathan Landaw")))
    conn.commit(); conn.close()
    with app.test_client() as c:
        html = c.get(f"/works/detect/{eid}/edit").get_data(as_text=True)
    assert "9781559390248" in html                       # live edition.isbn (Bug 1)
    assert f'value="{pid}"' in html and "Jonathan Landaw" in html   # existing → one-click
    assert 'data-name="Andy Weber"' in html              # new → picker-prefill chip


def test_route_enriches_from_isbn(app):
    # Inject ISBN metadata (no network); Detect should prefer the authoritative title +
    # author over the filename parse, and cache the lookup.
    app.config["ISBN_LOOKUP"] = lambda isbn: ({
        "title": "How to Meditate on the Stages of the Path: A Guide to the Lamrim",
        "authors": ["Kathleen McDonald"], "publishers": ["Wisdom Publications"],
        "publish_date": "2024", "isbn_13": "9781614298939", "source": "openlibrary",
    } if isbn == "9781614298939" else None)
    eid = _seed_book(app, ANNAS_A, author_name="Kathleen McDonald")   # title = raw filename
    with app.test_client() as c:
        r = c.post(f"/works/detect/{eid}/detect",
                   data=json.dumps({}), content_type="application/json").get_json()
    assert r["ok"] and r["detected"] and r["source"].startswith("isbn:")
    assert [x["name"] for x in r["linked"]["authors"]] == ["Kathleen McDonald"]
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    # the ISBN's fuller title (with subtitle) wins over the filename's truncated one
    assert conn.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] \
        == "How to Meditate on the Stages of the Path: A Guide to the Lamrim"
    conn.close()


def test_route_no_detection_is_graceful(app):
    eid = _seed_book(app, "Plain-File-Name.pdf")
    with app.test_client() as c:
        r = c.post(f"/works/detect/{eid}/detect",
                   data=json.dumps({}), content_type="application/json")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["detected"] is False


# ── bulk detect: refuse to manufacture duplicate titles within the selection ───
def test_bulk_detect_refuses_duplicate_titles_and_applies_nothing(app):
    # Two DIFFERENT books whose filenames converge to the same title → the bulk op would
    # create a within-batch duplicate. It must refuse and change NOTHING (the clobber guard).
    a = _seed_book(app, "The Same Book -- Author One -- 2020 -- Pub -- "
                   + "1" * 32 + " -- Anna’s Archive.epub")
    b = _seed_book(app, "The Same Book -- Author Two -- 2021 -- Pub -- "
                   + "2" * 32 + " -- Anna’s Archive.epub")
    c_ = _seed_book(app, "A Different Book -- Author Three -- 2022 -- Pub -- "
                    + "3" * 32 + " -- Anna’s Archive.epub")
    with app.test_client() as cl:
        r = cl.post("/works/detect/bulk-detect", json={"ids": [a, b, c_]}).get_json()
    assert r["ok"] is False
    assert r["collisions"] and r["collisions"][0]["title"] == "The Same Book"
    assert sorted(r["collisions"][0]["ids"]) == sorted([a, b])
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    # Nothing applied — titles still the raw filenames.
    for eid in (a, b, c_):
        assert " -- " in conn.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0]
    conn.close()


def test_bulk_detect_applies_when_no_collision(app):
    a = _seed_book(app, ANNAS_A)        # → "How to Meditate on the Stages of the Path"
    b = _seed_book(app, ANNAS_B)        # → "Teachings From the Medicine Buddha Retreat"
    with app.test_client() as cl:
        r = cl.post("/works/detect/bulk-detect", json={"ids": [a, b]}).get_json()
    assert r["ok"] and len(r["applied"]) == 2 and not r["failed"]
    assert sorted(r["changed"]) == sorted([a, b])
    # the client sets each left-pane row's title directly from this map
    assert r["titles"][str(a)] == "How to Meditate on the Stages of the Path"
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    assert conn.execute("SELECT title FROM edition WHERE id=?", (a,)).fetchone()[0] \
        == "How to Meditate on the Stages of the Path"
    conn.close()


def test_bulk_detect_classifies_changed_unchanged_undetected(app):
    """The honest per-edition summary that makes a no-op legible: a raw title is CHANGED,
    an already-curated title is UNCHANGED (preserved), a non-Anna's filename is UNDETECTED."""
    from catalogue.db_store import connect
    raw = _seed_book(app, ANNAS_A)                      # raw filename title → cleaned
    conn = connect(app.config["DB_PATH"])
    # already-curated: every field the Anna's filename would fill is pre-set (so there's
    # nothing left to WRITE) — only the differing filename title remains, offered as a
    # suggestion rather than applied (a curated title is never clobbered).
    curated = conn.execute(
        "INSERT INTO edition (title, subtitle, publisher, year, isbn) "
        "VALUES ('A Curated Title', 'Land of Medicine', 'Lama Yeshe Wisdom Archive', "
        "2009, '9781891868238')").lastrowid
    conn.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
                 (curated, "/lib/" + ANNAS_B))
    # undetected: a plain filename the parser doesn't recognize
    plain = conn.execute("INSERT INTO edition (title) VALUES ('Mahabharata 1 -- Debroy')").lastrowid
    conn.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
                 (plain, "/lib/Mahabharata 1 -- Debroy, Bibek.epub"))
    conn.commit(); conn.close()
    with app.test_client() as cl:
        r = cl.post("/works/detect/bulk-detect", json={"ids": [raw, curated, plain]}).get_json()
    assert r["ok"]
    assert r["changed"] == [raw]
    assert r["undetected"] == [plain]
    # curated → kept, surfaced as a suggestion (filename title differs from the curated one)
    assert [s["id"] for s in r["suggested"]] == [curated]
