"""v_live_person repoint — person LIST reads (picker, autocomplete) exclude tombstoned persons
(reorg Phase 4, the prerequisite for routing person delete/merge through the access-API tombstone).
"""
from __future__ import annotations

from catalogue.db_store import init_db
from catalogue.services import picker


def test_picker_unresolved_list_and_count_exclude_tombstones(tmp_path):
    c = init_db(tmp_path / "p.db")
    live = c.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('Live Provisional', 'provisional')").lastrowid
    dead = c.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('Dead Provisional', 'provisional')").lastrowid
    c.execute("UPDATE person SET deleted_at = datetime('now') WHERE id = ?", (dead,))
    c.commit()

    ids = {r[0] for r in picker._person_unresolved(c)}
    assert live in ids and dead not in ids
    assert picker._person_unresolved_count(c) == 1            # tombstone not counted
    # explicit-id fetch also filters the tombstone
    by_id = {r[0] for r in picker._person_unresolved(c, ids=[live, dead])}
    assert by_id == {live}
