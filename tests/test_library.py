"""Tests for the single-dashboard service + routes (catalogue/library.py, /library).

Covers the FRBR cross-link payload (author→works, translator→editions,
work→other editions with volume-set grouping), browse/search rows, the
add-by-upload ingest (hermetic, no extractor), and the HTTP surface."""
from __future__ import annotations

import io

import pytest

from catalogue.services import library as L
from catalogue.db_store import add_alias, connect, init_db
from catalogue.webui.web import create_app


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.config["UPLOAD_PROCESS"] = False        # hermetic: no extractor/LLM on upload
    app.testing = True
    with app.test_client() as c:
        yield c, app


def _db(app):
    return connect(app.config["DB_PATH"])


def _person(db, name):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid
    add_alias(db, "person", pid, name, "english")
    return pid


def _work(db, title, author_pid):
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", wid, title, "english")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?, ?, 'author')",
               (wid, author_pid))
    return wid


def _edition(db, title, wid, *, translator_pid=None, volume_set_id=None,
             volume=None, volume_seq=None, language=None):
    eid = db.execute(
        "INSERT INTO edition (title, language, volume, volume_set_id, volume_seq) "
        "VALUES (?, ?, ?, ?, ?)", (title, language, volume, volume_set_id, volume_seq)
    ).lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)",
               (eid, wid))
    if translator_pid is not None:
        db.execute("INSERT INTO edition_translator (edition_id, person_id, seq) "
                   "VALUES (?, ?, 1)", (eid, translator_pid))
    return eid


# ── Service: cross-links ───────────────────────────────────────────────────
def test_edition_links_authors_and_translators(env):
    _, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    tr = _person(db, "Kate Crosby")
    wid = _work(db, "Bodhicaryāvatāra", author)
    eid = _edition(db, "The Way of the Bodhisattva", wid, translator_pid=tr)
    db.commit()

    links = L.edition_links(db, eid)
    assert links["title"] == "The Way of the Bodhisattva"
    assert [a["name"] for a in links["authors"]] == ["Śāntideva"]
    assert links["authors"][0]["n_works"] == 1
    assert [t["name"] for t in links["translators"]] == ["Kate Crosby"]
    assert links["translators"][0]["n_editions"] == 1
    # The single work has no other editions → empty groups.
    assert links["works"][0]["work_id"] == wid
    assert links["works"][0]["groups"] == []
    db.close()


def test_edition_links_multiplicity_other_editions(env):
    """A work shared across two editions surfaces each as the other's sibling."""
    _, app = env
    db = _db(app)
    author = _person(db, "Kamalaśīla")
    t1, t2 = _person(db, "Translator A"), _person(db, "Translator B")
    wid = _work(db, "Stages of Meditation", author)
    e1 = _edition(db, "Stages of Meditation (Geshe ed.)", wid, translator_pid=t1)
    e2 = _edition(db, "Stages of Meditation (Other ed.)", wid, translator_pid=t2)
    db.commit()

    links = L.edition_links(db, e1)
    groups = links["works"][0]["groups"]
    assert len(groups) == 1 and groups[0]["is_set"] is False
    assert groups[0]["edition"]["id"] == e2
    assert groups[0]["edition"]["translators"] == ["Translator B"]
    # Translator A now shows 1 edition; symmetric view from e2 lists e1.
    back = L.edition_links(db, e2)
    assert back["works"][0]["groups"][0]["edition"]["id"] == e1
    db.close()


def test_edition_links_volume_set_grouped_once(env):
    """Multi-volume sets collapse to ONE group (not bogus duplicate editions),
    keep the current volume flagged, and order by volume_seq."""
    _, app = env
    db = _db(app)
    author = _person(db, "Tsongkhapa")
    wid = _work(db, "Lamrim Chenmo", author)
    db.execute("INSERT INTO edition (title) VALUES ('set-anchor')")
    set_id = db.execute("SELECT max(id) FROM edition").fetchone()[0]
    v1 = _edition(db, "Lamrim Chenmo Vol 1", wid, volume_set_id=set_id,
                  volume="Vol. 1", volume_seq=1)
    v2 = _edition(db, "Lamrim Chenmo Vol 2", wid, volume_set_id=set_id,
                  volume="Vol. 2", volume_seq=2)
    v3 = _edition(db, "Lamrim Chenmo Vol 3", wid, volume_set_id=set_id,
                  volume="Vol. 3", volume_seq=3)
    db.commit()

    links = L.edition_links(db, v2)
    groups = links["works"][0]["groups"]
    assert len(groups) == 1 and groups[0]["is_set"] is True
    vols = groups[0]["volumes"]
    assert [v["volume_seq"] for v in vols] == [1, 2, 3]
    assert [v["is_current"] for v in vols] == [False, True, False]
    db.close()


# ── Service: a contributor's books (person review pane) ─────────────────────
def test_person_books_author_and_translator(env):
    _, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    tr = _person(db, "Kate Crosby")
    wid = _work(db, "Bodhicaryāvatāra", author)
    e1 = _edition(db, "The Way of the Bodhisattva", wid, translator_pid=tr)
    # A second edition the translator also worked on, author elsewhere.
    wid2 = _work(db, "Training Anthology", author)
    e2 = _edition(db, "Śikṣāsamuccaya", wid2)
    # Give e1 a holding so its title becomes an open link.
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
               (e1, "/tmp/way.pdf"))
    db.commit()

    abooks = L.person_books(db, author)
    assert {b["edition_id"] for b in abooks} == {e1, e2}
    assert next(b for b in abooks if b["edition_id"] == e1)["roles"] == ["author"]
    assert next(b for b in abooks if b["edition_id"] == e1)["has_file"] is True
    assert next(b for b in abooks if b["edition_id"] == e1)["file_ext"] == "pdf"

    tbooks = L.person_books(db, tr)
    assert [b["edition_id"] for b in tbooks] == [e1]
    assert tbooks[0]["roles"] == ["translator"]
    assert L.person_books(db, 99999) == []
    db.close()


def test_person_works_author_and_translator(env):
    """The FRBR layer above person_books: one row per WORK, with author vs
    translator roles kept distinct (a work the person only authored must NOT be
    labelled translator even though it shows up in person_work_ids)."""
    _, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    tr = _person(db, "Kate Crosby")
    wid = _work(db, "Bodhicaryāvatāra", author)
    _edition(db, "The Way of the Bodhisattva", wid, translator_pid=tr)
    wid2 = _work(db, "Training Anthology", author)
    _edition(db, "Śikṣāsamuccaya", wid2)
    db.commit()

    aworks = L.person_works(db, author)
    assert {w["work_id"] for w in aworks} == {wid, wid2}
    assert all(w["roles"] == ["author"] for w in aworks)   # authored, never translator
    assert next(w for w in aworks if w["work_id"] == wid)["title"] == "Bodhicaryāvatāra"

    tworks = L.person_works(db, tr)
    assert [w["work_id"] for w in tworks] == [wid]
    assert tworks[0]["roles"] == ["translator"]
    assert L.person_works(db, 99999) == []
    db.close()


def test_person_works_fragment_links_to_work(env):
    c, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    db.commit(); db.close()

    r = c.get(f"/picker/person/{author}/works")
    assert r.status_code == 200
    assert f"/work/{wid}".encode() in r.data            # title links to the work page
    assert b"Bodhic" in r.data
    assert b"author" in r.data                           # role label
    # And the person pane wires it as a collapsible, expanded-by-default section.
    page = c.get("/picker/person").data
    assert b"Works by this contributor" in page
    assert f"/picker/person/{author}/works".encode() in page


def test_person_books_fragment_title_links_to_edition_icon_opens_file(env):
    c, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    eid = _edition(db, "The Way of the Bodhisattva", wid)
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
               (eid, "/tmp/way.pdf"))
    db.commit(); db.close()

    r = c.get(f"/picker/person/{author}/books")
    assert r.status_code == 200
    # Standard: the title navigates to the edition page; the 📖 icon opens the file.
    assert f'href="/edition/{eid}"'.encode() in r.data and b"edition-title" in r.data
    assert b"bookopen iconbtn" in r.data                # separate open-file affordance
    assert b"The Way of the Bodhisattva" in r.data
    assert b"author" in r.data                          # role label
    # And the person pane wires it as a collapsible, expanded-by-default section.
    page = c.get("/picker/person").data
    assert b"Books with this contributor" in page
    assert f"/picker/person/{author}/books".encode() in page


# ── Service: browse / search rows ───────────────────────────────────────────
def test_browse_and_search_rows(env):
    _, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    eid = _edition(db, "The Way of the Bodhisattva", wid)
    # The edition's by-line is its OWN people (edition_author), not the contained work's
    # author — so record the book-level author for the browse row to show it.
    db.execute("INSERT INTO edition_author (edition_id, person_id, role, seq) "
               "VALUES (?, ?, 'author', 1)", (eid, author))
    db.commit()

    rows = L.browse(db)
    assert any(r["id"] == eid and "Śāntideva" in r["subtitle"] for r in rows)
    # Diacritic-insensitive author search via find_books (matches via the contained work).
    hits = L.search(db, author="Santideva")
    assert [r["id"] for r in hits] == [eid]
    assert L.search(db, book_title="nope") == []
    db.close()


# ── Service: add-by-upload (hermetic) ───────────────────────────────────────
def test_ingest_upload_registers_edition(env, tmp_path):
    _, app = env
    db = _db(app)
    src = tmp_path / "My Book.pdf"
    src.write_bytes(b"%PDF-1.4 dummy")
    res = L.ingest_upload(db, src, dest_dir=tmp_path / "uploads",
                          filename="My Book.pdf", process=False)
    assert res["edition_id"] and res["holding_id"]
    title = db.execute("SELECT title FROM edition WHERE id = ?",
                       (res["edition_id"],)).fetchone()[0]
    assert title.endswith("My_Book")
    # The holding points at a uuid-prefixed COPY inside the managed dir, never
    # the original source path.
    fp = db.execute("SELECT file_path FROM holding WHERE id = ?",
                    (res["holding_id"],)).fetchone()[0]
    assert "uploads" in fp and fp != str(src) and fp.endswith("_My_Book.pdf")
    db.close()


# ── HTTP surface ─────────────────────────────────────────────────────────────
def test_library_page_browse(env):
    c, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    _edition(db, "The Way of the Bodhisattva", wid)
    db.commit(); db.close()

    r = c.get("/library")
    assert r.status_code == 200
    assert b"+ Add book" in r.data
    assert "The Way of the Bodhisattva".encode() in r.data


def test_library_search_and_deeplink(env):
    c, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    eid = _edition(db, "The Way of the Bodhisattva", wid)
    db.commit(); db.close()

    r = c.get("/library?author=Santideva")
    assert "The Way of the Bodhisattva".encode() in r.data
    r2 = c.get(f"/library?eid={eid}")
    assert r2.status_code == 200 and "The Way of the Bodhisattva".encode() in r2.data


def test_library_book_title_edition_number_jump(env):
    # The book-title box doubles as an edition-number lookup (the #N jump that used
    # to live in the dropped /find box): "#N" / a bare integer resolves straight to
    # that edition's detail, both on form submit and in the autocomplete dropdown.
    c, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    eid = _edition(db, "The Way of the Bodhisattva", wid)
    db.commit(); db.close()

    # Form submit with "#N" → 302 to the edition's detail.
    r = c.get(f"/library?book_title=%23{eid}")
    assert r.status_code == 302 and f"/library?eid={eid}" in r.headers["Location"]
    # A bare integer works too.
    r = c.get(f"/library?book_title={eid}")
    assert r.status_code == 302 and f"/library?eid={eid}" in r.headers["Location"]
    # A non-existent id is NOT a jump — falls through to a normal (empty) search.
    r = c.get("/library?book_title=%2399999")
    assert r.status_code == 200
    # The autocomplete dropdown surfaces edition #N for the "#N" query.
    j = c.get(f"/editions/search?q=%23{eid}").get_json()
    assert any(m["edition_id"] == eid for m in j["matches"])


def test_library_book_title_multi_token_fuzzy(env):
    # The book-title box matches whitespace-separated tokens in ANY order (AND of
    # tokens), not just a contiguous substring: "discourses buddha" finds every book
    # whose title contains both words, however far apart or reordered.
    c, app = env
    db = _db(app)
    wid = _work(db, "Root", _person(db, "A"))
    e1 = _edition(db, "Connected Discourses of the Buddha", wid)
    e2 = _edition(db, "Long Discourses of the Buddha", wid)
    e3 = _edition(db, "In the Buddha's Words: An Anthology of Discourses", wid)
    e4 = _edition(db, "The Way of the Bodhisattva", wid)   # control: no match
    db.commit(); db.close()

    r = c.get("/library?book_title=discourses+buddha")
    data = r.data
    assert r.status_code == 200
    for title in (b"Connected Discourses of the Buddha",
                  b"Long Discourses of the Buddha",
                  b"Discourses"):   # the Anthology too
        assert title in data, title
    assert b"The Way of the Bodhisattva" not in data
    # Same logic powers the autocomplete dropdown.
    matches = c.get("/editions/search?q=discourses+buddha").get_json()["matches"]
    got = {m["edition_id"] for m in matches}
    assert {e1, e2, e3} <= got and e4 not in got


def test_edition_links_fragment(env):
    c, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    tr = _person(db, "Kate Crosby")
    wid = _work(db, "Bodhicaryāvatāra", author)
    eid = _edition(db, "The Way of the Bodhisattva", wid, translator_pid=tr)
    db.commit(); db.close()

    r = c.get(f"/edition/{eid}/links")
    assert r.status_code == 200
    assert b"Kate Crosby" in r.data and b"Bodhicary" in r.data
    assert c.get("/edition/99999/links").status_code == 404


def test_add_form_and_upload_redirects_to_editor(env):
    c, app = env
    r = c.get("/library/add")
    assert r.status_code == 200 and b"Add a book" in r.data
    # Reject non-PDF/EPUB.
    bad = c.post("/library/add", data={"file": (io.BytesIO(b"x"), "note.txt")},
                 content_type="multipart/form-data")
    assert bad.status_code == 400
    # A PDF is accepted (UPLOAD_PROCESS off → no extractor) and lands in the editable
    # Review surface with the new book selected.
    ok = c.post("/library/add",
                data={"file": (io.BytesIO(b"%PDF-1.4 hi"), "Great Book.pdf")},
                content_type="multipart/form-data")
    assert ok.status_code == 302 and "/review?eid=" in ok.headers["Location"]


# ── Type-scoped candidates + entity decomposition (sectioned Review pane) ──────
def test_search_works_lists_work_candidates(env):
    _, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    _edition(db, "The Way of the Bodhisattva", wid)
    db.commit()
    # A work-title search returns WORK rows (not editions), tagged seltype='work'.
    cands = L.search_works(db, "Bodhicaryavatara")
    assert [r["id"] for r in cands] == [wid]
    assert cands[0]["seltype"] == "work" and "ed." in cands[0]["subtitle"]
    db.close()


def test_search_persons_lists_person_candidates(env):
    _, app = env
    db = _db(app)
    pid = _person(db, "Śāntideva")
    cands = L.search_persons(db, "Santideva")
    assert [r["id"] for r in cands] == [pid]
    assert cands[0]["seltype"] == "person"
    db.close()


def test_decompose_work_has_editions_and_authors(env):
    _, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    eid = _edition(db, "The Way of the Bodhisattva", wid)
    db.commit()
    secs = {s["key"]: s for s in L.decompose_work(db, wid)}
    assert [r["id"] for r in secs["editions"]["rows"]] == [eid]
    assert secs["editions"]["seltype"] == "edition"
    assert [r["id"] for r in secs["authors"]["rows"]] == [author]
    assert secs["authors"]["seltype"] == "person"
    db.close()


def test_decompose_person_has_works_and_editions(env):
    _, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    eid = _edition(db, "The Way of the Bodhisattva", wid)
    db.commit()
    secs = {s["key"]: s for s in L.decompose_person(db, author)}
    assert [r["id"] for r in secs["works"]["rows"]] == [wid]
    assert [r["id"] for r in secs["editions"]["rows"]] == [eid]
    db.close()


def test_library_work_search_renders_sections(env):
    # A single work-title match auto-selects the work and decomposes it: the page shows
    # the Work card host plus an "Editions of this work" section listing the edition.
    c, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    _edition(db, "The Way of the Bodhisattva", wid)
    db.commit(); db.close()
    r = c.get("/library?work_title=Bodhicaryavatara")
    assert r.status_code == 200
    assert b"Editions of this work" in r.data
    # Read-only Search page: the work detail is the read-only summary, not the editable card.
    assert f"/work/{wid}/summary".encode() in r.data
    assert f"/work/{wid}/card".encode() not in r.data
    assert b"The Way of the Bodhisattva" in r.data


def test_library_person_deeplink_decomposes(env):
    # ?pid= deep-links a person: read-only header + Works + Editions sections, no redirect.
    c, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    _edition(db, "The Way of the Bodhisattva", wid)
    db.commit(); db.close()
    r = c.get(f"/library?pid={author}")
    assert r.status_code == 200
    # Read-only Search page: no editable person card; a link to the person page instead.
    assert f"/person/{author}/card".encode() not in r.data
    assert f'href="/person/{author}"'.encode() in r.data
    assert b"person page" in r.data
    # Both decomposition sections rendered: the work row + the edition row.
    assert "Bodhicaryāvatāra".encode() in r.data
    assert b"The Way of the Bodhisattva" in r.data
    assert b'class="n">' in r.data          # section header count chips


def test_library_combined_fields_and_fallback(env):
    # Two fields set (via URL) → AND-combine over editions (the fallback path).
    c, app = env
    db = _db(app)
    author = _person(db, "Śāntideva")
    wid = _work(db, "Bodhicaryāvatāra", author)
    eid = _edition(db, "The Way of the Bodhisattva", wid)
    db.execute("INSERT INTO edition_author (edition_id, person_id, role, seq) "
               "VALUES (?, ?, 'author', 1)", (eid, author))
    db.commit(); db.close()
    r = c.get("/library?book_title=Bodhisattva&person=Santideva")
    assert r.status_code == 200
    assert b"The Way of the Bodhisattva" in r.data


def test_nav_has_the_feature_links(env):
    # The redesigned nav is a flat feature bar (no "Experimental" dropdown), rendered by
    # the shared LibraryUI.nav component from a JS `items` array (href: '/path'). Browse
    # (/find) was dropped — the Search page (/search) covers it, including the #N
    # edition-number jump that used to live in the unified /find box.
    c, _ = env
    r = c.get("/search")
    # Nav is now manifest-driven (LibraryCore.APP_SECTIONS + a per-surface HREF map), so the page
    # carries the paths as HREF values rather than inline `href: '/path'`.
    for href in (b"'/search'", b"'/review'", b"'/reconcile'", b"'/capture'"):
        assert href in r.data, href
    assert b"'/find'" not in r.data
    assert b"Experimental" not in r.data


# ── soft-delete: tombstoned editions never surface in lists (reorg Phase 4) ──────
def test_browse_and_subject_shelf_hide_tombstoned_editions(tmp_path):
    """A soft-deleted edition must not reappear in the master browse list or a subject shelf —
    library.browse + subject_tree.editions_for_subject filter deleted_at."""
    from catalogue.services import subject_tree as T
    db = init_db(tmp_path / "c.db")
    sid = db.execute("INSERT INTO subject (name) VALUES ('Madhyamaka')").lastrowid
    live = db.execute("INSERT INTO edition (title) VALUES ('Live')").lastrowid
    dead = db.execute("INSERT INTO edition (title) VALUES ('Dead')").lastrowid
    for e in (live, dead):
        db.execute("INSERT INTO edition_subject (edition_id, subject_id) VALUES (?, ?)", (e, sid))
    db.execute("UPDATE edition SET deleted_at = datetime('now') WHERE id = ?", (dead,))
    db.commit()

    assert {r["id"] for r in L.browse(db)} == {live}          # master list excludes the tombstone
    assert T.editions_for_subject(db, sid) == [live]          # subject shelf excludes it too
