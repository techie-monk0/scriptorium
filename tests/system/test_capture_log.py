"""Capture page log of phone scans that were NOT found in the catalogue.

Black-box over HTTP: the phone POSTs barcode/CIP scans; GET /capture renders a
log of every `ios` capture whose cross-format verdict was "not in catalogue",
carrying the ISBN, any OCR text that was sent, and the time of capture.
Resolvers are stubbed offline by `app_env`, so a scan against an empty catalogue
is always "not found".
"""
from __future__ import annotations

VALID_ISBN = "9780861711765"

CIP_WITH_ISBN = (
    "Library of Congress Cataloging-in-Publication Data\n"
    "Title: Illuminating the intent / Tsongkhapa.\n"
    "Identifiers: LCCN 2020045678 | ISBN 9781614294412\n"
)


def _log(client) -> str:
    return client.get("/capture").get_data(as_text=True)


def test_phone_barcode_not_in_catalogue_is_logged_with_isbn_and_time(app_env):
    c, _, _ = app_env
    c.post("/capture", json={"isbn": VALID_ISBN, "source": "ios",
                             "scanned_at": "2026-06-12T10:00:00Z"})
    html = _log(c)
    assert "not in the catalogue" in html.lower()
    assert VALID_ISBN in html
    assert "2026-06-12T10:00:00Z" in html      # timestamp of capture


def test_in_catalogue_scan_is_not_logged(app_env, seed):
    c, _, _ = app_env
    seed("INSERT INTO edition (id, title, isbn) VALUES (5, 'Held', ?)", (VALID_ISBN,))
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (5, 'physical', 'x')")
    c.post("/capture", json={"isbn": VALID_ISBN, "source": "ios"})
    assert VALID_ISBN not in _log(c)           # matched by ISBN → excluded from the log


def test_cip_capture_logs_the_ocr_pages_that_were_sent(app_env):
    c, _, _ = app_env
    c.post("/capture/cip", json={
        "source": "ios",
        "scanned_at": "2026-06-12T11:30:00Z",
        "pages": [{"label": "copyright", "text": CIP_WITH_ISBN}],
    })
    html = _log(c)
    assert "9781614294412" in html             # ISBN parsed from the CIP block
    assert "Illuminating the intent" in html   # OCR text is kept and shown
    assert "2026-06-12T11:30:00Z" in html


def test_capture_title_is_shown_from_metadata(app_env, seed):
    c, _, _ = app_env
    seed(
        "INSERT INTO capture_staging "
        "(id, form, raw_isbn, source, status, in_catalogue, metadata_json) "
        "VALUES (50, 'physical', ?, 'ios', 'raw', 0, ?)",
        (VALID_ISBN, '{"title": "A Distinctive Captured Title", "authors": []}'),
    )
    assert "A Distinctive Captured Title" in _log(c)   # title shown beside the ISBN


def test_barcode_scan_stores_title_at_capture(app_env):
    c, app, _ = app_env
    app.config["ISBN_LOOKUP"] = lambda _i: {
        "title": "Fetched At Capture Time", "authors": ["A. Author"],
        "isbn_13": _i, "source": "openlibrary",
    }
    c.post("/capture", json={"isbn": VALID_ISBN, "source": "ios"})
    # The bare barcode path now fetches + stores metadata, so the log shows a title.
    assert "Fetched At Capture Time" in _section(_log(c), MISSING_H)


def test_barcode_capture_survives_lookup_failure(app_env):
    c, app, _ = app_env

    def boom(_i):
        raise RuntimeError("offline")
    app.config["ISBN_LOOKUP"] = boom
    r = c.post("/capture", json={"isbn": VALID_ISBN, "source": "ios"})
    assert r.status_code == 201                       # scan still saved
    assert VALID_ISBN in _section(_log(c), MISSING_H)  # just title-less


def test_web_form_capture_is_not_in_the_phone_log(app_env):
    c, _, _ = app_env
    # The multipart form path stores source='web' — not a phone scan.
    c.post("/capture", data={"isbn": VALID_ISBN})
    assert VALID_ISBN not in _log(c)


def test_empty_log_renders_without_the_section(app_env):
    c, _, _ = app_env
    assert "not in the catalogue" not in _log(c).lower()


MISSING_H = "<h2>Scanned — not in the catalogue</h2>"
ADDED_H = "<h2>Added</h2>"


def _section(html: str, heading: str) -> str:
    """The slice of the page belonging to one section: from its <h2> to the next
    <h2> (or end of page). Lets us assert which section an ISBN landed in."""
    if heading not in html:
        return ""
    rest = html.split(heading, 1)[1]
    return rest.split("<h2>", 1)[0]


def test_scan_moves_to_added_once_catalogued(app_env, seed):
    c, _, _ = app_env
    # A phone scan that is not in the catalogue when scanned…
    c.post("/capture", json={"isbn": VALID_ISBN, "source": "ios"})
    assert VALID_ISBN in _section(_log(c), MISSING_H)
    # …then the book gets catalogued independently (its ISBN now matches).
    seed("INSERT INTO edition (id, title, isbn) VALUES (7, 'Now held', ?)", (VALID_ISBN,))
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (7, 'physical', 'x')")
    html = _log(c)
    added = _section(html, ADDED_H)
    assert VALID_ISBN in added                          # now under "Added"
    assert "Now held" in added                          # shows the edition it was added as
    assert '/edition/7' in added                        # linked to that edition
    assert VALID_ISBN not in _section(html, MISSING_H)  # no longer listed as missing


def test_still_missing_scan_stays_out_of_added(app_env):
    c, _, _ = app_env
    c.post("/capture", json={"isbn": VALID_ISBN, "source": "ios"})
    assert "<h2>Added</h2>" not in _log(c)


def test_scan_moves_to_added_when_catalogued_under_a_different_isbn(app_env, seed):
    """A scan whose copy was catalogued under a DIFFERENT ISBN (a different
    printing that public sources don't link) still moves to Added, matched by
    title + a shared author — even with a spurious extra 'author' (a CIP block
    often lists the book's subject), which only makes the match partial."""
    c, app, _ = app_env
    scan_isbn = "9781611806472"
    app.config["ISBN_LOOKUP"] = lambda _i: {
        "title": "Atiśa Dīpaṃkara: Illuminator of the Awakened Mind",
        "authors": ["James B. Apple", "Atisa"],     # 2nd is the subject, not a real author
        "isbn_13": _i, "source": "openlibrary"}
    c.post("/capture", json={"isbn": scan_isbn, "source": "ios"})
    assert scan_isbn in _section(_log(c), MISSING_H)         # missing at first

    # Catalogued under a DIFFERENT ISBN, by the matching author only.
    seed("INSERT INTO edition (id, title, isbn) VALUES (9, "
         "'Atiśa Dīpaṃkara: Illuminator of the Awakened Mind', '9780834842205')")
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (9, 'physical', 'x')")
    pid = seed("INSERT INTO person (primary_name) VALUES ('James B. Apple')").lastrowid
    seed("INSERT INTO edition_author (edition_id, person_id) VALUES (9, ?)", (pid,))

    added = _section(_log(c), ADDED_H)
    assert "Illuminator of the Awakened Mind" in added       # matched cross-edition
    assert "/edition/9" in added
    assert scan_isbn not in _section(_log(c), MISSING_H)


def test_added_edition_title_shows_volume_number(app_env, seed):
    """A matched edition that is a volume in a multi-volume set shows its volume
    number alongside the title, so sibling volumes aren't ambiguous."""
    c, app, _ = app_env
    app.config["ISBN_LOOKUP"] = lambda _i: {
        "title": "The Great Treatise on the Stages of the Path to Enlightenment",
        "authors": ["Tsong-kha-pa"], "isbn_13": _i, "source": "openlibrary"}
    c.post("/capture", json={"isbn": "9781559391528", "source": "ios"})

    seed("INSERT INTO edition (id, title, isbn, volume) VALUES (9, "
         "'The Great Treatise on the Stages of the Path to Enlightenment', '9781559391689', '3')")
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (9, 'physical', 'x')")
    pid = seed("INSERT INTO person (primary_name) VALUES ('Tsong-kha-pa')").lastrowid
    seed("INSERT INTO edition_author (edition_id, person_id) VALUES (9, ?)", (pid,))

    assert "· vol. 3" in _section(_log(c), ADDED_H)
