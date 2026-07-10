"""Work-authority engine surface — the identity-at-creation reads/writes work_identity routes through.

`work_identity.get_or_create_work` (and the manual 'Add a work' form) used to issue raw SQL for
canonical/title-key lookup, author overlap, alias-existence, native-title backfill, type registration
and commentary linking. Those now go through `acc.works.reads`/`acc.works.writes` over the caller's
own connection (`system_conn`), staged so the service owns the commit. These tests pin that surface
directly; `test_work_identity.py` covers the service behavior end-to-end.
"""
from __future__ import annotations

from catalogue.access_api import system_conn
from catalogue.db_store import fold_key, init_db


def _seed(tmp_path):
    db = tmp_path / "wa.db"
    c = init_db(db)
    w = c.execute(
        "INSERT INTO work (canonical_system, canonical_number) VALUES ('toh', '42')").lastrowid
    c.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) VALUES (?,?,?,?)",
              (w, "Heart Sutra", "english", fold_key("Heart Sutra")))
    p = c.execute("INSERT INTO person (primary_name) VALUES ('Nagarjuna')").lastrowid
    c.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w, p))
    c.commit()
    return c, dict(w=w, p=p)


def test_reads_find_and_lookup(tmp_path):
    c, ids = _seed(tmp_path)
    acc = system_conn(c)
    assert acc.works.reads.find_by_canonical("toh", "42") == ids["w"]
    assert acc.works.reads.find_by_canonical("toh", "999") is None
    assert acc.works.reads.find_by_canonical(None, None) is None
    assert acc.works.reads.ids_by_alias_key(fold_key("Heart Sutra")) == [ids["w"]]
    assert acc.works.reads.ids_by_alias_key(fold_key("nope")) == []
    assert set(acc.works.reads.author_ids(ids["w"])) == {ids["p"]}
    assert acc.works.reads.has_alias_key(ids["w"], fold_key("Heart Sutra")) is True
    assert acc.works.reads.has_alias_key(ids["w"], fold_key("Other")) is False


def test_reads_skip_tombstoned(tmp_path):
    c, ids = _seed(tmp_path)
    c.execute("UPDATE work SET deleted_at = datetime('now') WHERE id = ?", (ids["w"],))
    c.commit()
    acc = system_conn(c)
    # A dead work must not be a get_or_create attach target.
    assert acc.works.reads.find_by_canonical("toh", "42") is None
    assert acc.works.reads.ids_by_alias_key(fold_key("Heart Sutra")) == []


def test_insert_and_scalar_writes(tmp_path):
    c, _ = _seed(tmp_path)
    acc = system_conn(c)
    wid = acc.works.writes.insert_work(
        {"original_language": "sa", "canonical_system": "bdrc", "canonical_number": "W7"})
    c.commit()
    row = c.execute(
        "SELECT original_language, canonical_system, canonical_number FROM work WHERE id=?",
        (wid,)).fetchone()
    assert tuple(row) == ("sa", "bdrc", "W7")

    # coalesce_scalars fills ONLY where empty; set_scalars overwrites.
    acc.works.writes.fill_scalars(wid, {"era": "classical", "original_language": "OVERWRITE?"})
    c.commit()
    r = c.execute("SELECT era, original_language FROM work WHERE id=?", (wid,)).fetchone()
    assert r[0] == "classical" and r[1] == "sa"          # original_language was non-empty → kept
    acc.works.writes.set_scalars(wid, {"era": "modern"})
    c.commit()
    assert c.execute("SELECT era FROM work WHERE id=?", (wid,)).fetchone()[0] == "modern"


def test_work_type_registration_and_native_title(tmp_path):
    c, ids = _seed(tmp_path)
    acc = system_conn(c)
    acc.works.writes.set_work_type(ids["w"], "commentary")
    c.commit()
    assert c.execute("SELECT work_type FROM work WHERE id=?", (ids["w"],)).fetchone()[0] == "commentary"
    assert c.execute("SELECT 1 FROM work_type WHERE code='commentary'").fetchone()  # FK code registered

    acc.works.writes.set_native_title(ids["w"], "sanskrit_title", "Prajñāpāramitā")
    c.commit()
    assert c.execute("SELECT sanskrit_title FROM work WHERE id=?", (ids["w"],)).fetchone()[0] \
        == "Prajñāpāramitā"
    acc.works.writes.set_native_title(ids["w"], "sanskrit_title", None)  # alias vanished → clear
    c.commit()
    assert c.execute("SELECT sanskrit_title FROM work WHERE id=?", (ids["w"],)).fetchone()[0] is None


def test_relate_commentary_idempotent(tmp_path):
    c, ids = _seed(tmp_path)
    root = c.execute("INSERT INTO work (canonical_system) VALUES ('toh')").lastrowid
    c.commit()
    acc = system_conn(c)
    acc.works.writes.relate_commentary(ids["w"], root)
    acc.works.writes.relate_commentary(ids["w"], root)   # idempotent — no duplicate edge
    c.commit()
    n = c.execute("SELECT count(*) FROM relationship WHERE from_work_id=? AND relation='commentary_on' "
                  "AND to_work_id=?", (ids["w"], root)).fetchone()[0]
    assert n == 1
    assert acc.works.reads.commentary_root_id(ids["w"]) == root
    assert c.execute("SELECT work_type FROM work WHERE id=?", (ids["w"],)).fetchone()[0] == "commentary"
    assert c.execute("SELECT work_type FROM work WHERE id=?", (root,)).fetchone()[0] == "root"


def test_first_alias_text(tmp_path):
    c, ids = _seed(tmp_path)
    c.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) VALUES (?,?,?,?)",
              (ids["w"], "snying po", "wylie", fold_key("snying po")))
    c.commit()
    acc = system_conn(c)
    assert acc.works.reads.first_alias_text(ids["w"], "wylie") == "snying po"
    assert acc.works.reads.first_alias_text(ids["w"], "iast") is None
