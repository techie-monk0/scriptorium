"""Sample-data builders — centralize the per-test seeding boilerplate.

Every access-API test reinvents the same minimal graph: one edition with a holding, one work
(alias + author) linked to it, one person. `seed_minimal` builds exactly that and returns the ids,
so tests assert behavior instead of copy-pasting INSERTs. The caller owns the commit (the seeder
does pure DB work, matching the rest of the codebase).
"""
from __future__ import annotations

from catalogue.db_store import fold_key


def seed_minimal(conn) -> dict:
    """Insert one fully-linked edition + holding + work (alias + author) + person.

    Returns ``{"edition", "holding", "person", "work"}`` ids. Does NOT commit — the caller's
    transaction owns it (call ``conn.commit()`` before reading through a separate `Access`)."""
    eid = conn.execute(
        "INSERT INTO edition (title, isbn) VALUES ('Sample Edition', '9780000000001')").lastrowid
    hid = conn.execute(
        "INSERT INTO holding (edition_id, form, file_path, content_hash, text_status) "
        "VALUES (?, 'electronic', ?, ?, 'ocr_good')",
        (eid, f"/sample/e{eid}.pdf", f"content-hash-{eid}")).lastrowid
    pid = conn.execute("INSERT INTO person (primary_name) VALUES ('Sample Author')").lastrowid
    wid = conn.execute("INSERT INTO work (canonical_system) VALUES ('toh')").lastrowid
    conn.execute(
        "INSERT INTO work_alias (work_id, text, normalized_key) VALUES (?, 'Sample Work', ?)",
        (wid, fold_key("Sample Work")))
    conn.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (wid, pid))
    conn.execute(
        "INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 0)", (eid, wid))
    return {"edition": eid, "holding": hid, "person": pid, "work": wid}
