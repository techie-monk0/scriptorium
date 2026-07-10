"""§14 capture integration contract — server side.

Exercises POST /capture (JSON), POST /capture/batch, and GET
/capture/version against the schema in plan §14.2–14.5. All assertions go
through the HTTP surface so the iOS client and the pipeline share one
test target.
"""
from __future__ import annotations


def test_capture_version_advertises_contract(app_env):
    c, _, _ = app_env
    r = c.get("/capture/version")
    assert r.status_code == 200
    assert r.get_json() == {"contract_version": "4"}


def test_valid_ios_scan_returns_201_and_stages(app_env):
    c, _, _ = app_env
    r = c.post(
        "/capture",
        json={"isbn": "9780861711765",
              "scanned_at": "2026-05-28T19:30:00Z",
              "source": "ios"},
    )
    assert r.status_code == 201
    body = r.get_json()
    assert body["status"] == "ok"
    assert body["isbn"] == "9780861711765"
    assert body["duplicate"] is False
    assert isinstance(body["staging_id"], int)


def test_duplicate_pending_scan_returns_existing_staging_id(app_env):
    c, _, _ = app_env
    first = c.post("/capture", json={"isbn": "9780861711765", "source": "ios"})
    second = c.post("/capture", json={"isbn": "9780861711765", "source": "ios"})
    assert first.status_code == 201 and second.status_code == 201
    assert first.get_json()["duplicate"] is False
    assert second.get_json()["duplicate"] is True
    assert first.get_json()["staging_id"] == second.get_json()["staging_id"]


def test_invalid_checksum_returns_422_with_reason(app_env):
    c, _, _ = app_env
    # Last digit bumped — fails the ISBN-13 checksum.
    r = c.post("/capture", json={"isbn": "9780861711760", "source": "ios"})
    assert r.status_code == 422
    assert r.get_json() == {"status": "invalid", "reason": "checksum"}


def test_short_isbn_returns_422_length(app_env):
    c, _, _ = app_env
    r = c.post("/capture", json={"isbn": "978086171", "source": "ios"})
    assert r.status_code == 422
    assert r.get_json()["reason"] == "length"


def test_non_digit_isbn_returns_422_format(app_env):
    c, _, _ = app_env
    r = c.post("/capture", json={"isbn": "ABCDEFGHIJKLM", "source": "ios"})
    assert r.status_code == 422
    assert r.get_json()["reason"] == "format"


def test_batch_endpoint_returns_per_item_results_in_order(app_env):
    c, _, _ = app_env
    r = c.post(
        "/capture/batch",
        json={"scans": [
            {"isbn": "9780861711765", "source": "ios"},
            {"isbn": "9780861711760", "source": "ios"},   # bad checksum
            {"isbn": "9780205309023", "source": "ios"},
        ]},
    )
    assert r.status_code == 201
    results = r.get_json()["results"]
    assert [x["status"] for x in results] == ["ok", "invalid", "ok"]
    assert results[1]["reason"] == "checksum"
    assert results[0]["isbn"] == "9780861711765"
    assert results[2]["isbn"] == "9780205309023"


def test_scanned_at_persists_on_insert(app_env):
    """§14.2: the client's ISO-8601 scanned_at survives queue-flush delay."""
    c, app, _ = app_env
    ts = "2026-05-28T19:30:00Z"
    r = c.post("/capture", json={"isbn": "9780861711765",
                                 "scanned_at": ts, "source": "ios"})
    sid = r.get_json()["staging_id"]
    import sqlite3
    conn = sqlite3.connect(app.config["DB_PATH"])
    row = conn.execute(
        "SELECT scanned_at, source FROM capture_staging WHERE id = ?", (sid,)
    ).fetchone()
    conn.close()
    assert row == (ts, "ios")


def test_scanned_at_first_value_wins_on_duplicate(app_env):
    """Re-posting a pending ISBN must NOT overwrite the original time-of-scan."""
    c, app, _ = app_env
    first = "2026-05-28T19:30:00Z"
    later = "2026-05-29T08:00:00Z"
    c.post("/capture", json={"isbn": "9780861711765",
                             "scanned_at": first, "source": "ios"})
    c.post("/capture", json={"isbn": "9780861711765",
                             "scanned_at": later, "source": "ios"})
    import sqlite3
    conn = sqlite3.connect(app.config["DB_PATH"])
    rows = conn.execute(
        "SELECT scanned_at FROM capture_staging WHERE raw_isbn = ?",
        ("9780861711765",),
    ).fetchall()
    conn.close()
    assert rows == [(first,)]


def test_missing_scanned_at_is_accepted_as_null(app_env):
    """Web form path and old clients won't send scanned_at — that's OK."""
    c, app, _ = app_env
    r = c.post("/capture", json={"isbn": "9780861711765", "source": "ios"})
    assert r.status_code == 201
    sid = r.get_json()["staging_id"]
    import sqlite3
    conn = sqlite3.connect(app.config["DB_PATH"])
    val = conn.execute(
        "SELECT scanned_at FROM capture_staging WHERE id = ?", (sid,)
    ).fetchone()[0]
    conn.close()
    assert val is None


def test_form_post_still_works_for_browser_capture(app_env):
    """Regression: §14 JSON path must not break the existing web form."""
    c, _, _ = app_env
    r = c.post("/capture", data={"isbn": "9780205309023"})
    # Form path returns HTML 200 (or JSON via Accept negotiation) — just
    # confirm it didn't get hijacked by the JSON branch as 422.
    assert r.status_code == 200
