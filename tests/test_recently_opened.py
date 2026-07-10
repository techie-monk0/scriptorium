"""`recently_opened` = the home 'recently read' rail. It must include ONLY editions
that were genuinely opened (a real holding.last_opened), ranked by that timestamp.

Regression: it formerly ordered by COALESCE(last_opened, date_added), so a never-opened
book was treated as 'recently opened' (ranked by its added date) and leaked into the
Home "Recent" rail — which is what surfaced books after unrelated edits (e.g. changing
a subject). Recently-ADDED books are surfaced separately by homeVM from date_added.
"""
from __future__ import annotations

from catalogue.access_api import system_access
from catalogue.db_store import init_db


def test_recently_opened_excludes_never_opened(tmp_path):
    db = tmp_path / "t.db"
    c = init_db(db)
    e_read = c.execute("INSERT INTO edition (title) VALUES ('Read Me')").lastrowid
    e_new = c.execute("INSERT INTO edition (title) VALUES ('Never Opened')").lastrowid
    # e_read was opened long ago; e_new was never opened but has a much NEWER date_added —
    # under the old COALESCE fallback it would have ranked first. It must not appear at all.
    c.execute("INSERT INTO holding (edition_id, form, last_opened, date_added) "
              "VALUES (?, 'electronic', '2024-01-01 00:00:00', '2024-01-01 00:00:00')", (e_read,))
    c.execute("INSERT INTO holding (edition_id, form, last_opened, date_added) "
              "VALUES (?, 'electronic', NULL, '2030-01-01 00:00:00')", (e_new,))
    c.commit()
    with system_access(str(db)) as acc:
        ids = acc.editions.reads.recently_opened(24)
    assert ids == [e_read]


def test_recently_opened_orders_by_last_opened(tmp_path):
    db = tmp_path / "t.db"
    c = init_db(db)
    e_old = c.execute("INSERT INTO edition (title) VALUES ('Older Read')").lastrowid
    e_recent = c.execute("INSERT INTO edition (title) VALUES ('Newer Read')").lastrowid
    c.execute("INSERT INTO holding (edition_id, form, last_opened) "
              "VALUES (?, 'electronic', '2024-01-01 00:00:00')", (e_old,))
    c.execute("INSERT INTO holding (edition_id, form, last_opened) "
              "VALUES (?, 'electronic', '2024-06-01 00:00:00')", (e_recent,))
    c.commit()
    with system_access(str(db)) as acc:
        ids = acc.editions.reads.recently_opened(24)
    assert ids == [e_recent, e_old]   # most-recently-opened first
