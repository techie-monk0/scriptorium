"""The shared left-pane filter (text box + all/unreviewed/reviewed scope radios) lives
once in `_book_browser.html` and now defaults ON for EVERY Review tab — keyed off the
`review_tab` context var, not a per-tab opt-in flag — so any tab added later inherits it
with no extra wiring. Non-review consumers of the same shell stay unaffected.

Black-box HTTP smoke tests: seed one row per tab, render each route, and assert the filter
markup is present (review tabs) or absent (a non-review page), per the system-test convention.
"""
from __future__ import annotations

from catalogue.db_store import init_db
from catalogue.services import work_detect as WD
from catalogue.webui.web import create_app


# the two DOM markers unique to the shared filter widget
_FILTER_INPUT = b'id="bb-find-q"'
_SCOPE_RADIOS = b'name="bbfind-scope"'


def _has_filter(html: bytes) -> bool:
    return _FILTER_INPUT in html and _SCOPE_RADIOS in html


def _seed_client(tmp_path):
    """One DB with a single row behind each review tab — most tabs guard their
    _book_browser include on having items, so an empty DB would hide the pane entirely."""
    dbp = tmp_path / "rf.db"
    db = init_db(dbp)
    # Books: an edition with a stored single-work detection
    eid = db.execute("INSERT INTO edition (title, structure) VALUES ('Bodhicaryavatara', 'single_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/x.pdf')", (eid,))
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))
    # Works: a work flagged for review
    db.execute("INSERT INTO work (review_status) VALUES ('needs_fix')")
    # People: an unresolved person
    db.execute("INSERT INTO person (primary_name, verification_status) VALUES ('Tsongkhapa','provisional')")
    # Subjects: a subject
    db.execute("INSERT INTO subject (name) VALUES ('Madhyamaka')")
    db.commit()
    return create_app(str(dbp)).test_client()


# ── every Review tab carries the same filter ──────────────────────────────────
def test_all_review_tabs_have_the_filter(tmp_path):
    """Books + Subjects already opted in; Works + People now inherit the SAME widget
    via the review_tab default."""
    c = _seed_client(tmp_path)
    for route in ("/works/detect/single",          # Books
                  "/works/incomplete",              # Works  (newly filtered)
                  "/picker/person",                 # People (newly filtered)
                  "/review/subjects"):      # Subjects
        r = c.get(route)
        assert r.status_code == 200, route
        assert _has_filter(r.data), f"filter missing on review tab {route}"


# ── the scope radios are the SAME three the books tab uses ─────────────────────
def test_scope_options_match_the_books_pattern(tmp_path):
    c = _seed_client(tmp_path)
    for route in ("/works/incomplete", "/picker/person"):
        html = c.get(route).data
        for value in (b'value="all"', b'value="unreviewed"', b'value="reviewed"'):
            assert value in html, f"{value!r} missing on {route}"


# ── non-review consumers of the shell are untouched ────────────────────────────
def test_non_review_page_has_no_inherited_filter(tmp_path):
    """/library includes the same _book_browser shell but sets no review_tab, so the
    filter must NOT appear (it has its own server-side Browse search instead)."""
    c = _seed_client(tmp_path)
    r = c.get("/library")
    assert r.status_code == 200
    assert not _has_filter(r.data), "non-review page wrongly inherited the filter"
