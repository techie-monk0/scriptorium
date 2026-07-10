"""Content-search index build: cached extraction → edition_text → FTS → /search.

The full-text "Content search" (/search) was scaffolded but never populated; this pins that
`build_content_index` promotes cached `raw_extract_cache` text into `edition_text` so a term
that appears INSIDE a book becomes findable (and that a metadata-only term does not leak in).
"""
from __future__ import annotations

from catalogue.db_store import connect
from catalogue.cli.build_content_index import build_index


def _content(client, q: str) -> list:
    """The content-search JSON books for a query (the shared `/api/v1/content`
    contract the converged /search page renders)."""
    return client.get(f"/api/v1/content?q={q}").get_json()["books"]


def _seed_book_with_text(seed, *, title, file_hash, body):
    eid = seed("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    seed("INSERT INTO holding (edition_id, form, file_path, file_hash) "
         "VALUES (?, 'electronic', ?, ?)", (eid, f"{file_hash}.epub", file_hash))
    seed("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
         "VALUES (?, 1, ?)", (file_hash, body))
    return eid


def test_content_search_finds_in_text_after_build(app_env, seed):
    c, app, _ = app_env
    # A word that is ONLY in the body, not the title — proves it's content, not metadata.
    eid = _seed_book_with_text(
        seed, title="A Plain Title", file_hash="HASHA",
        body="Chapter one. The doctrine of tathagatagarbha appears deep inside this book.")
    # Before building: content search returns no book for this term.
    assert all(b["eid"] != eid for b in _content(c, "tathagatagarbha"))

    conn = connect(app.config["DB_PATH"])
    stats = build_index(conn)
    conn.close()
    assert stats["indexed"] >= 1 and stats["chunks"] >= 1

    books = _content(c, "tathagatagarbha")
    # Grouped by edition, labelled by title.
    hit = next(b for b in books if b["eid"] == eid)
    assert hit["title"] == "A Plain Title"


def test_build_is_resumable_and_skips_uncached(app_env, seed):
    c, app, _ = app_env
    eid = _seed_book_with_text(seed, title="Has Text", file_hash="HASHB",
                               body="unique_marker_zzz lives here")
    # A holding whose file_hash has NO cached extraction → skipped, not crashed.
    seed("INSERT INTO edition (title) VALUES ('No Cache')")
    seed("INSERT INTO holding (edition_id, form, file_path, file_hash) "
         "VALUES ((SELECT max(id) FROM edition), 'electronic', 'x.pdf', 'NOCACHE')")

    conn = connect(app.config["DB_PATH"])
    s1 = build_index(conn)
    assert s1["indexed"] == 1 and s1["no_cached_text"] == 1
    # Second run is a no-op for the already-indexed edition (resumable).
    s2 = build_index(conn)
    conn.close()
    assert s2["indexed"] == 0 and s2["already"] == 1

    assert any(b["eid"] == eid for b in _content(c, "unique_marker_zzz"))


def test_results_group_by_edition_and_show_author(app_env, seed):
    c, app, _ = app_env
    # One edition, TWO copies (epub + pdf) both containing the term, with a book-level author.
    eid = seed("INSERT INTO edition (title) VALUES ('Two Copies Book')").lastrowid
    pid = seed("INSERT INTO person (primary_name) VALUES ('Jane Author')").lastrowid
    seed("INSERT INTO edition_author (edition_id, person_id, role, seq) VALUES (?, ?, 'author', 1)", (eid, pid))
    for n in (1, 2):
        seed("INSERT INTO holding (edition_id, form, file_path, file_hash) "
             "VALUES (?, 'electronic', ?, ?)", (eid, f"c{n}.epub", f"COPY{n}"))
        seed("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
             "VALUES (?, 1, 'shared singular_token_qqq inside both copies')", (f"COPY{n}",))
    conn = connect(app.config["DB_PATH"]); build_index(conn); conn.close()

    books = _content(c, "singular_token_qqq")
    assert len([b for b in books if b["eid"] == eid]) == 1   # grouped: one card, not one per copy
    assert len(books) == 1
    assert "Jane Author" in books[0]["authors"]              # edition author shown, not the edition id


def test_results_show_book_level_author_and_translator(app_env, seed):
    # A book whose only person link is a book-level author (edition_author) — the case that
    # was invisible before (FTS read only work-level work_author).
    c, app, _ = app_env
    eid = seed("INSERT INTO edition (title) VALUES ('Emptiness Yoga')").lastrowid
    pid = seed("INSERT INTO person (primary_name) VALUES ('Jeffrey Hopkins')").lastrowid
    seed("INSERT INTO edition_author (edition_id, person_id, role, seq) VALUES (?, ?, 'author', 1)", (eid, pid))
    seed("INSERT INTO holding (edition_id, form, file_path, file_hash) VALUES (?, 'electronic', 'ey.pdf', 'HASHEY')", (eid,))
    seed("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
         "VALUES ('HASHEY', 1, 'A discussion of the middle way and emptiness_marker_xyz here.')")

    # A translator-only book (no author of any kind).
    teid = seed("INSERT INTO edition (title) VALUES ('Translated Treatise')").lastrowid
    tpid = seed("INSERT INTO person (primary_name) VALUES ('Anne Klein')").lastrowid
    seed("INSERT INTO edition_translator (edition_id, person_id) VALUES (?, ?)", (teid, tpid))
    seed("INSERT INTO holding (edition_id, form, file_path, file_hash) VALUES (?, 'electronic', 't.pdf', 'HASHTR')", (teid,))
    seed("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
         "VALUES ('HASHTR', 1, 'Another emptiness_marker_xyz appears in this translated work.')")

    conn = connect(app.config["DB_PATH"]); build_index(conn); conn.close()
    authors = [a for b in _content(c, "emptiness_marker_xyz") for a in b["authors"]]
    assert "Jeffrey Hopkins" in authors       # book-level author now shows
    assert "Anne Klein (tr.)" in authors      # translator shown, marked


def test_one_book_shows_multiple_passages(app_env, seed):
    # The term appears in two passages far enough apart to land in different chunks → the
    # book card shows multiple snippets (the whole point of finer-grained indexing).
    c, app, _ = app_env
    words = [f"w{i}" for i in range(400)]
    words[5] = "zephyrqux"
    words[300] = "zephyrqux"          # > CHUNK_WORDS apart → a separate chunk
    _seed_book_with_text(seed, title="Long Book", file_hash="HASHLONG", body=" ".join(words))
    conn = connect(app.config["DB_PATH"]); build_index(conn); conn.close()

    books = _content(c, "zephyrqux")
    assert len(books) == 1                                   # grouped to one book…
    assert len(books[0]["snippets"]) >= 2                   # …but two matching passages shown
