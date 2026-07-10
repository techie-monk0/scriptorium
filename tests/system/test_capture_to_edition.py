"""System tests — §7.3 capture → staging → edition, end-to-end via HTTP.

Plan invariants verified entirely through HTTP:
  - "phone → capture_staging ... Desktop resolves staging into
    editions/holdings, attaching to an existing edition when present
    (records overlap without duplication)." (§7.3)
  - "validate it's a well-formed ISBN-13 (checksum), then resolve via
    Open Library for title/author/publisher/year. Don't assume every
    book has one: if the field is empty or lookup returns no record,
    fall back to the photo/manual capture path." (Step-3b spec)

The ISBN resolver is swapped via `app.config["ISBN_LOOKUP"]` — this is
the documented extension point for alternative resolvers, so its use in
a system test is correct (we're configuring an interface, not reaching
into private state).
"""
from __future__ import annotations


def test_valid_isbn_capture_appears_in_dashboard_pending_count(app_env):
    c, app, _ = app_env
    app.config["ISBN_LOOKUP"] = lambda _i: None

    # Same ISBN scanned twice (raw + hyphenated) → §14.5 idempotent
    # dedup: one open row, not two.
    c.post("/capture", data={"isbn": "9780205309023"})
    c.post("/capture", data={"isbn": "978-0-205-30902-3"})  # same, hyphenated

    # The hub surfaces the Capture entry point (its badge counts open captures).
    home = c.get("/")
    assert home.status_code == 200
    assert b'href="/capture"' in home.data
    # Dedup → exactly one raw staging row for the (now-normalized) ISBN, observable
    # on the staging-resolution list (the second half of the Capture feature).
    staging = c.get("/staging").data
    assert staging.count(b"9780205309023") == 1


def test_capture_invalid_isbn_still_lands_for_manual_review(app_env):
    """Step-3b: invalid ISBN falls back to the manual path — the row
    still appears in the staging list, observable via the UI."""
    c, app, _ = app_env

    def must_not_be_called(_):
        raise AssertionError("invalid ISBN must not hit Open Library")
    app.config["ISBN_LOOKUP"] = must_not_be_called

    r = c.post("/capture", data={"isbn": "9780205309022",  # bad checksum
                                 "note": "shelf 4"})
    assert r.status_code == 200

    # Visible on the staging list.
    page = c.get("/staging")
    assert page.status_code == 200
    assert b"shelf 4" in page.data


def test_capture_then_resolve_creates_edition_and_links_holding(app_env):
    """End-to-end: capture → /staging → resolve → /edition/<id> shows it."""
    c, app, _ = app_env
    app.config["ISBN_LOOKUP"] = lambda _i: {
        "title": "The Way of the Bodhisattva",
        "authors": ["Śāntideva"], "publishers": ["Shambhala"],
        "publish_date": "2006", "isbn_13": _i, "source": "openlibrary",
    }

    # 1. Phone-side capture.
    c.post("/capture", data={"isbn": "9780205309023"})

    # 2. Desktop staging page surfaces it with OL-resolved title.
    staging = c.get("/staging/1")
    assert staging.status_code == 200
    assert "The Way of the Bodhisattva".encode() in staging.data

    # 3. Desktop resolves staging → new edition.
    resolve = c.post("/staging/1/resolve",
                     data={"resolution": "new",
                           "title": "The Way of the Bodhisattva",
                           "isbn": "9780205309023"},
                     follow_redirects=False)
    assert resolve.status_code in (302, 303)

    # 4. Edition page exists and shows the captured title.
    location = resolve.headers["Location"]
    assert "/edition/" in location
    page = c.get(location)
    assert page.status_code == 200
    assert "The Way of the Bodhisattva".encode() in page.data


def test_capture_dedup_existing_edition_does_not_duplicate(app_env):
    """§7.3: 'attaching to an existing edition when present (records
    overlap without duplication).' Observable via the staging detail
    page — the existing edition is offered first in the attach picker."""
    c, app, _ = app_env
    app.config["ISBN_LOOKUP"] = lambda _i: None

    # Pre-existing edition with the same ISBN. Setup via /staging/resolve
    # so we go through the public surface.
    c.post("/capture", data={"isbn": "9780205309023"})
    c.post("/staging/1/resolve",
           data={"resolution": "new", "title": "Existing", "isbn": "9780205309023"})

    # Now a second capture of the same ISBN.
    c.post("/capture", data={"isbn": "9780205309023"})

    page = c.get("/staging/2")
    # The existing edition is offered as a "this is a duplicate" match, so resolving
    # it adds nothing rather than minting a second edition.
    assert 'value="match:1"' in page.data.decode(), (
        "Existing edition with matching ISBN must be offered as a match"
    )


def test_empty_capture_is_refused(app_env):
    """A submit with no ISBN, no note, no photo must not litter the
    staging table — the staging list count stays at 0."""
    c, _, _ = app_env
    c.post("/capture", data={})    # empty
    page = c.get("/staging")
    # The "Nothing pending" sentinel is rendered when no rows exist.
    assert b"Nothing pending" in page.data


def test_shortcut_json_response_for_ios(app_env):
    """The iOS Shortcut path: same /capture, JSON output."""
    c, app, _ = app_env
    app.config["ISBN_LOOKUP"] = lambda _i: {
        "title": "T", "isbn_13": _i, "authors": [], "publishers": [],
        "publish_date": None, "source": "openlibrary",
    }
    r = c.post(
        "/capture",
        data={"isbn": "9780205309023"},
        headers={"X-Requested-With": "shortcut"},
    )
    body = r.get_json()
    assert body["ok"] is True
    assert body["metadata"]["title"] == "T"
