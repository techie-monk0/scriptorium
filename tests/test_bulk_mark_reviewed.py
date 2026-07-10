"""Bulk "✓ Mark all reviewed" on the review worklists.

A select-all bar (the shell's opt-in `bulk_select` layer) plus a "Mark all reviewed"
action now sits on every review surface: Books single + multi and Works-needing-review
share one client-side helper (`_bulk_mark_reviewed.html`) that loops the ticked rows
through each page's existing single-item review endpoint; People reuse the picker's own
server-side bulk bar, where the existing confirm-local op is relabelled to the same verb.

Black-box render tests (per the system-test convention): seed one row behind each tab,
render the route, and assert the bulk markup + the page's review endpoint are wired in.
"""
from __future__ import annotations

from catalogue.db_store import init_db
from catalogue.services import work_detect as WD, picker
from catalogue.webui.web import create_app


# DOM markers unique to the shared bulk layer
_SELALL = b'id="bb-selall"'              # the shell's "select all" checkbox
_SELBAR = b'id="bb-selbar"'              # the bulk action bar
_HOST = b'id="bb-bulk-actions"'          # where the action is injected
_HELPER = b'window.BB_REVIEW'            # the per-page bulk config object


def _seed_client(tmp_path):
    dbp = tmp_path / "bmr.db"
    db = init_db(dbp)
    # Books single-work
    eid = db.execute("INSERT INTO edition (title, structure) VALUES "
                     "('Bodhicaryavatara', 'single_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES "
               "(?, 'electronic', '/x.pdf')", (eid,))
    WD.store_detection(db, eid, "single",
                       WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))
    # Books multi-work
    meid = db.execute("INSERT INTO edition (title, structure) VALUES "
                      "('Collected Topics', 'multi_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES "
               "(?, 'electronic', '/m.pdf')", (meid,))
    WD.store_detection(db, meid, "multi", {"kind": "multi", "works": []})
    # Works-needing-review
    db.execute("INSERT INTO work (review_status) VALUES ('needs_fix')")
    # an unresolved person (People tab)
    db.execute("INSERT INTO person (primary_name, verification_status) VALUES "
               "('Tsongkhapa','provisional')")
    db.commit()
    return create_app(str(dbp)).test_client()


def test_books_pages_carry_the_bulk_review_bar(tmp_path):
    """Both Books worklists render the select-all bar and post to /works/detect/<id>/reviewed."""
    c = _seed_client(tmp_path)
    for route in ("/works/detect/single", "/works/detect/multi"):
        r = c.get(route)
        assert r.status_code == 200, route
        for marker in (_SELALL, _SELBAR, _HOST, _HELPER):
            assert marker in r.data, f"{marker!r} missing on {route}"
        assert b"/works/detect/${id}/reviewed" in r.data, route


def test_works_incomplete_carries_the_bulk_review_bar(tmp_path):
    c = _seed_client(tmp_path)
    r = c.get("/works/incomplete")
    assert r.status_code == 200
    for marker in (_SELALL, _SELBAR, _HOST, _HELPER):
        assert marker in r.data
    assert b"/work/${id}/review" in r.data


def test_people_picker_offers_mark_reviewed(tmp_path):
    """The persons bulk bar leads with the 'Mark reviewed' verb (the relabelled
    confirm-local op), and the op still confirms-local under the hood."""
    c = _seed_client(tmp_path)
    r = c.get("/picker/person")
    assert r.status_code == 200
    assert _SELALL in r.data and _SELBAR in r.data
    assert b"Mark reviewed" in r.data
    # the labelled op is still the confirm-local create_new key
    assert any(o.key == "create_new" and "Mark reviewed" in o.label
               for o in picker.bulk_ops("person"))


def test_bulk_helper_only_on_review_pages_not_picker(tmp_path):
    """People reuse the picker's server-side bulk bar, NOT the client-loop helper —
    so the BB_REVIEW config must not leak onto the picker page."""
    c = _seed_client(tmp_path)
    assert _HELPER not in c.get("/picker/person").data


def _single_book_client(tmp_path, *, applied):
    dbp = tmp_path / f"done-{applied}.db"
    db = init_db(dbp)
    eid = db.execute("INSERT INTO edition (title, structure) VALUES "
                     "('Done Book', 'single_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES "
               "(?, 'electronic', '/d.pdf')", (eid,))
    det = WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]})
    det["applied"] = applied
    WD.store_detection(db, eid, "single", det)
    db.commit()
    return create_app(str(dbp)).test_client()


def test_reviewed_rows_stay_selectable_on_books(tmp_path):
    """On the Books worklist (bulk_include_reviewed) an already-reviewed row keeps an
    ENABLED checkbox, so a batch of reviewed books can still be added to a series /
    subject / author. (Mark-all-reviewed itself skips done rows, handled client-side.)
    An unreviewed row is of course selectable too."""
    done = _single_book_client(tmp_path, applied=True).get("/works/detect/single").data
    assert b"window.BB_INCLUDE_REVIEWED = true" in done           # the opt-in is on
    assert b'title="select for a bulk operation"' in done
    assert b'title="select for a bulk operation" disabled' not in done
    todo = _single_book_client(tmp_path, applied=False).get("/works/detect/single").data
    assert b'title="select for a bulk operation"' in todo
    assert b'title="select for a bulk operation" disabled' not in todo


def test_include_reviewed_opt_in_is_scoped_to_books(tmp_path):
    """The 'keep reviewed rows selectable' opt-in is Books-only: Works-needing-review still
    uses the default (reviewed checkbox disabled), so it must NOT carry the opt-in flag."""
    c = _seed_client(tmp_path)
    assert b"window.BB_INCLUDE_REVIEWED = true" in c.get("/works/detect/single").data
    assert b"window.BB_INCLUDE_REVIEWED = true" not in c.get("/works/incomplete").data


def test_reviewed_checkbox_disabled_by_default(tmp_path):
    """The shared partial's default (no bulk_include_reviewed) still disables a reviewed
    row's checkbox — rendered directly so the gate is isolated from any page."""
    from flask import render_template
    app = _single_book_client(tmp_path, applied=True).application
    with app.test_request_context():
        html = render_template(
            "_book_browser.html", bulk_select=True, empty_msg="none",
            detail_template="_open_control.html",
            items=[{"id": 1, "title": "Done", "subtitle": "", "done": True,
                    "has_file": False}])
    assert 'title="select for a bulk operation" disabled' in html
