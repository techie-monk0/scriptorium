"""`person.notes` (the curator's disambiguation rationale) must be visible on both
person surfaces: the REVIEW pane (picker detail) shows it read-only with an edit link,
and the BROWSE card (/person/<id>) shows it in an editable textarea that saves.

Black-box render + round-trip tests (system-test convention).
"""
from __future__ import annotations

from catalogue.db_store import init_db
from catalogue.webui.web import create_app

NOTE = "KEEP SEPARATE from #999; no authority record located."


def _client(tmp_path, *, status="provisional", external_id=None):
    dbp = tmp_path / "pn.db"
    db = init_db(dbp)
    db.execute("INSERT INTO person (id, primary_name, dates, external_id, "
               "verification_status, notes) VALUES (701,'Test Siddha','11th c.',?,?,?)",
               (external_id, status, NOTE))
    db.commit()
    return create_app(str(dbp)).test_client()


def test_review_pane_shows_note(tmp_path):
    """A provisional/unbound person is in the review queue; its detail pane renders the
    note block and a link out to the editable person page."""
    html = _client(tmp_path).get("/picker/person").get_data(as_text=True)
    assert "picker-notes" in html
    assert NOTE in html
    assert '/person/701"' in html          # edit ↗ link to the full person page


def test_browse_card_shows_and_edits_note(tmp_path):
    c = _client(tmp_path, status="confirmed_local")
    card = c.get("/person/701/card").get_data(as_text=True)
    assert 'name="notes"' in card and NOTE in card        # editable textarea, pre-filled
    full = c.get("/person/701").get_data(as_text=True)
    assert NOTE in full

    # the textarea round-trips through /person/<id>/edit
    c.post("/person/701/edit", data={"primary_name": "Test Siddha", "notes": "edited"})
    again = c.get("/person/701/card").get_data(as_text=True)
    assert "edited" in again and NOTE not in again


def test_blank_note_renders_no_review_block(tmp_path):
    """No note → no empty yellow box cluttering the review pane."""
    dbp = tmp_path / "blank.db"
    db = init_db(dbp)
    db.execute("INSERT INTO person (id, primary_name, verification_status) "
               "VALUES (702,'No Note','provisional')")
    db.commit()
    html = create_app(str(dbp)).test_client().get("/picker/person").get_data(as_text=True)
    assert "picker-notes" not in html
