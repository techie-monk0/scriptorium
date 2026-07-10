"""The /by-author index: editions segmented by TOP-LEVEL subject (A–Z by title),
plus authors / translators A–Z, each with their editions and works.

Editions link to a cover page (/edition/<id>/coverpage) that shows the cover + a
"Tap / click to read" prompt and links onward to the reader (/edition/<id>/read);
works link to /work/<id>.

Covers: both parts render; editions bucket under the first segment of their subject
path (via own tags and contained-work tags); untagged editions fall in
"(Uncategorized)", listed last; subject headings are alphabetical; edition links go
to the cover page; the cover page renders the prompt and the reader link; people are
alphabetized and carry their editions/works.
"""
from __future__ import annotations

import pytest

from catalogue.webui.web import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "web.db")
    app.testing = True
    with app.test_client() as c:
        yield c, app


def _seed(app):
    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO person (id, primary_name) VALUES (1, 'Śāntideva')")
    conn.execute("INSERT INTO person (id, primary_name) VALUES (2, 'Candrakīrti')")

    # Slash-path subjects → top level is the first segment.
    conn.execute("INSERT INTO subject (id, name, kind) VALUES (1, 'Buddhism/Madhyamaka', 'topic')")
    conn.execute("INSERT INTO subject (id, name, kind) VALUES (2, 'Philosophy/Logic', 'topic')")

    # Work by Śāntideva, tagged 'Buddhism/…', contained in edition 10 → edition 10
    # inherits the Buddhism subject and Śāntideva appears on it (contained-work author).
    conn.execute("INSERT INTO work (id) VALUES (100)")
    conn.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
                 "VALUES (100, 'Bodhicaryāvatāra', 'english', 'bodicaryavatara')")
    conn.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (100, 1, 'author')")
    conn.execute("INSERT INTO work_subject (work_id, subject_id) VALUES (100, 1)")
    conn.execute("INSERT INTO edition (id, title) VALUES (10, 'The Way of the Bodhisattva')")
    conn.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (10, 100, 1)")

    # Edition authored by Candrakīrti, translated by Śāntideva, tagged 'Philosophy/…'.
    conn.execute("INSERT INTO edition (id, title) VALUES (11, 'A Guide to the Middle Way')")
    conn.execute("INSERT INTO edition_author (edition_id, person_id, role) VALUES (11, 2, 'author')")
    conn.execute("INSERT INTO edition_translator (edition_id, person_id) VALUES (11, 1)")
    conn.execute("INSERT INTO edition_subject (edition_id, subject_id) VALUES (11, 2)")

    # An edition with NO subject → the "(Uncategorized)" bucket.
    conn.execute("INSERT INTO edition (id, title) VALUES (12, 'Untagged Reader')")

    # A standalone work by Candrakīrti (for the works section).
    conn.execute("INSERT INTO work (id) VALUES (101)")
    conn.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
                 "VALUES (101, 'Madhyamakāvatāra', 'english', 'madhyamakavatara')")
    conn.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (101, 2, 'author')")
    conn.commit()
    conn.close()


def test_by_author_renders_both_parts(client):
    c, app = client
    _seed(app)
    r = c.get("/by-author")
    assert r.status_code == 200
    body = r.data.decode()
    # Page title + the two-section note.
    assert "<h1>Library holdings list</h1>" in body
    assert "This list has two sections" in body
    # Section headings with their anchor ids.
    assert '<h2 id="editions">Editions' in body
    assert '<h2 id="authors">Authors' in body
    # Jump links at the top point at both sections.
    assert 'href="#editions"' in body and 'href="#authors"' in body
    # A separator precedes each section (one <hr> before each <h2>).
    assert body.count("<hr>") >= 2


def test_editions_segmented_by_top_level_subject(client):
    c, app = client
    _seed(app)
    body = c.get("/by-author").data.decode()
    # Top-level subject headings from the slash-paths, plus the untagged bucket.
    for heading in ("Buddhism", "Philosophy", "(Uncategorized)"):
        assert f">{heading} " in body or f">{heading}<" in body, heading
    # Alphabetical, with the no-subject bucket last.
    assert body.index("Buddhism") < body.index("Philosophy") < body.index("(Uncategorized)")
    # Edition 10 (inherits Buddhism via its contained work) sits in the Buddhism section.
    assert body.index("Buddhism") < body.index('/edition/10/coverpage') < body.index("Philosophy")
    # The untagged edition 12 is under (Uncategorized).
    assert body.index("(Uncategorized)") < body.index('/edition/12/coverpage')


def test_editions_section_shows_author_and_translator(client):
    c, app = client
    _seed(app)
    body = c.get("/by-author").data.decode()
    # Edition 11 in the Editions section carries its byline: author Candrakīrti, trans. Śāntideva.
    guide = body.index("A Guide to the Middle Way")
    window = body[guide:guide + 200]
    assert "Candrakīrti" in window and "trans. Śāntideva" in window


def test_editions_link_to_cover_page_not_reader(client):
    c, app = client
    _seed(app)
    body = c.get("/by-author").data.decode()
    assert '/edition/10/coverpage' in body and '/edition/11/coverpage' in body
    # The by-author list does NOT jump straight to the reader.
    assert '/edition/10/read"' not in body


def test_cover_page_shows_prompt_and_reader_link(client):
    c, app = client
    _seed(app)
    r = c.get("/edition/10/coverpage")
    assert r.status_code == 200
    body = r.data
    assert b"Tap / click to read" in body
    assert b'/edition/10/read"' in body          # the cover links onward to the reader
    assert b'/edition/10/cover.jpg' in body       # the cover image itself
    assert "The Way of the Bodhisattva".encode() in body


def test_people_section_lists_editions_and_works(client):
    c, app = client
    _seed(app)
    body = c.get("/by-author").data.decode()
    # Person names appear but are NOT links.
    assert "Candrakīrti" in body and "Śāntideva" in body
    assert 'href="/person/1"' not in body and 'href="/person/2"' not in body
    # A–Z by name — checked within the Authors section (names also appear in Editions bylines).
    authors = body[body.index('<h2 id="authors">'):]
    assert authors.index("Candrakīrti") < authors.index("Śāntideva")
    assert 'href="/work/100"' in body and 'href="/work/101"' in body
    # Edition links in a person's section also route through the cover page.
    assert body.count('/edition/10/coverpage') >= 2   # Buddhism section + Śāntideva section
