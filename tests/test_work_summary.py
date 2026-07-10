"""Read-only context builders for the three-layer edition/work display:
work_summary (Work Basics + Details) and edition_work_summaries (with the
degenerate-placeholder surfacing predicate)."""
from catalogue.db_store import connect, init_db
from catalogue.services import library as L
from catalogue.services import work_identity
from catalogue.services.work_identity import _ensure_alias as alias


def _db(tmp_path):
    return init_db(tmp_path / "c.db")


def test_work_summary_basics_and_details(tmp_path):
    db = _db(tmp_path)
    wid = db.execute(
        "INSERT INTO work (work_type, original_language, era, canonical_system, "
        "canonical_number, notes) VALUES ('root','sa','classical','toh','1234','a note')"
    ).lastrowid
    alias(db, wid, "The Heart Sutra", "english")
    alias(db, wid, "Prajñāpāramitāhṛdaya", "iast")
    alias(db, wid, "shes rab snying po", "wylie")
    alias(db, wid, "heart_sutra.pdf", "filename")
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Nāgārjuna')").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?, 'author')",
               (wid, pid))
    db.commit()

    s = L.work_summary(db, wid)
    assert s["title"] == "The Heart Sutra"          # English preferred over native/filename
    assert s["work_type"] == "root"
    assert s["root"] is None                         # a root has no root-of link
    assert s["authors"] == [{"id": pid, "name": "Nāgārjuna", "role": "author"}]
    assert s["notes"] == "a note"
    assert s["native"]["sanskrit"] == ["Prajñāpāramitāhṛdaya"]
    assert s["native"]["tibetan"] == ["shes rab snying po"]
    # Toh authority link → 84000 read page.
    assert s["authority_links"] == [
        {"label": "toh:1234", "url": "https://read.84000.co/translation/toh1234.html"}]
    # aliases_other excludes the display title, the filename, and the native-title aliases.
    assert s["aliases_other"] == []


def test_work_summary_commentary_links_root(tmp_path):
    db = _db(tmp_path)
    root = db.execute("INSERT INTO work (work_type) VALUES ('root')").lastrowid
    alias(db, root, "Root Verses", "english")
    comm = db.execute("INSERT INTO work (work_type) VALUES ('commentary')").lastrowid
    alias(db, comm, "A Commentary", "english")
    work_identity.relate_commentary(db, comm, root)
    db.commit()

    s = L.work_summary(db, comm)
    assert s["root"] == {"id": root, "title": "Root Verses"}
    assert L.work_summary(db, root)["root"] is None


def test_edition_work_summaries_hides_degenerate_modern_single(tmp_path):
    db = _db(tmp_path)
    # Modern single: one placeholder work, no canonical/type, structure not multi.
    eid = db.execute("INSERT INTO edition (title, structure) VALUES ('Modern Book', 'single')"
                     ).lastrowid
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    alias(db, wid, "Modern Book", "english")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
               (eid, wid))
    db.commit()
    assert L.edition_work_summaries(db, eid) == []           # placeholder not surfaced


def test_edition_work_summaries_surfaces_classical_and_multi(tmp_path):
    db = _db(tmp_path)
    # Classical single: one work but it carries a canonical number → surfaced.
    e1 = db.execute("INSERT INTO edition (title, structure) VALUES ('Classical', 'single')"
                    ).lastrowid
    w1 = db.execute("INSERT INTO work (canonical_system, canonical_number) VALUES ('toh','42')"
                    ).lastrowid
    alias(db, w1, "Canonical Text", "english")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (e1, w1))
    # Multi: two works → both surfaced.
    e2 = db.execute("INSERT INTO edition (title, structure) VALUES ('Anthology', 'multi_work')"
                    ).lastrowid
    for i, t in enumerate(("Text A", "Text B"), 1):
        w = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
        alias(db, w, t, "english")
        db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,?)",
                   (e2, w, i))
    db.commit()

    assert [s["title"] for s in L.edition_work_summaries(db, e1)] == ["Canonical Text"]
    assert {s["title"] for s in L.edition_work_summaries(db, e2)} == {"Text A", "Text B"}


def test_edition_persons_excludes_work_authors(tmp_path):
    db = _db(tmp_path)
    eid = db.execute("INSERT INTO edition (title) VALUES ('Book')").lastrowid
    ed_author = db.execute("INSERT INTO person (primary_name) VALUES ('Editor One')").lastrowid
    tr = db.execute("INSERT INTO person (primary_name) VALUES ('Translator Two')").lastrowid
    work_author = db.execute("INSERT INTO person (primary_name) VALUES ('Work Author')").lastrowid
    db.execute("INSERT INTO edition_author (edition_id, person_id) VALUES (?,?)", (eid, ed_author))
    db.execute("INSERT INTO edition_translator (edition_id, person_id) VALUES (?,?)", (eid, tr))
    w = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (eid, w))
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?, 'author')",
               (w, work_author))
    db.commit()

    p = L.edition_persons(db, eid)
    assert [a["name"] for a in p["authors"]] == ["Editor One"]      # work author NOT included
    assert [t["name"] for t in p["translators"]] == ["Translator Two"]
