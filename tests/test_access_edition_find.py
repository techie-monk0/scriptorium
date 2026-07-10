"""acc.editions.reads.find_ids — the reusable edition filter (subject / author / since-date /
since-edition), intersected, tombstone-excluding. The query primitives books_by_subject (and the
webui read sites) route through instead of bespoke SQL. See entity_api_model.md §8.
"""
from __future__ import annotations

from catalogue.access_api import system_access
from catalogue.db_store import init_db


def _seed(tmp_path):
    db = tmp_path / "t.db"
    c = init_db(db)
    # subject hierarchy: Buddhism > Tantra > Kalachakra
    s_bud = c.execute("INSERT INTO subject (name) VALUES ('Buddhism')").lastrowid
    s_tan = c.execute("INSERT INTO subject (name) VALUES ('Buddhism/Tantra')").lastrowid
    s_kal = c.execute("INSERT INTO subject (name) VALUES ('Buddhism/Tantra/Kalachakra')").lastrowid
    s_other = c.execute("INSERT INTO subject (name) VALUES ('Grammar')").lastrowid

    def ed(title):
        return c.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid

    def person(name):
        return c.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid

    e_direct = ed("Kalachakra Tantra")        # tagged Kalachakra directly
    c.execute("INSERT INTO edition_subject (edition_id, subject_id) VALUES (?, ?)", (e_direct, s_kal))
    e_viawork = ed("Tantra Collection")       # tagged Tantra via a contained work
    w = c.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    c.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (e_viawork, w))
    c.execute("INSERT INTO work_subject (work_id, subject_id) VALUES (?, ?)", (w, s_tan))
    e_other = ed("Sanskrit Grammar")          # unrelated subject
    c.execute("INSERT INTO edition_subject (edition_id, subject_id) VALUES (?, ?)", (e_other, s_other))
    e_dead = ed("Deleted Tantra")             # tombstoned — must never surface
    c.execute("INSERT INTO edition_subject (edition_id, subject_id) VALUES (?, ?)", (e_dead, s_tan))
    c.execute("UPDATE edition SET deleted_at = datetime('now') WHERE id = ?", (e_dead,))

    # authors: Tsongkhapa authors e_direct; Jinpa translates e_viawork
    p_author = person("Tsongkhapa")
    c.execute("INSERT INTO edition_author (edition_id, person_id, seq) VALUES (?,?,1)",
              (e_direct, p_author))
    p_trans = person("Thupten Jinpa")
    c.execute("INSERT INTO edition_translator (edition_id, person_id, seq) VALUES (?,?,1)",
              (e_viawork, p_trans))

    # holdings with dates (for since_date)
    c.execute("INSERT INTO holding (edition_id, form, file_path, date_added) "
              "VALUES (?, 'electronic', '/a.pdf', '2026-01-01')", (e_direct,))
    c.execute("INSERT INTO holding (edition_id, form, file_path, date_added) "
              "VALUES (?, 'electronic', '/b.pdf', '2026-06-20')", (e_viawork,))
    c.commit(); c.close()
    return dict(db=db, direct=e_direct, viawork=e_viawork, other=e_other, dead=e_dead,
                author=p_author, trans=p_trans)


def test_subject_prefix_inclusive_and_via_work(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        ids = acc.editions.reads.find_ids(subject="Buddhism/Tantra")
        assert set(ids) == {s["direct"], s["viawork"]}     # Kalachakra (nested) + via-work; not Grammar
        assert s["dead"] not in ids                        # tombstone excluded
        # the broader node rolls up the same two (+ anything else under Buddhism)
        assert {s["direct"], s["viawork"]} <= set(acc.editions.reads.find_ids(subject="Buddhism"))


def test_author_filter_and_intersection(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        assert acc.editions.reads.find_ids(author="Tsongkhapa") == [s["direct"]]
        assert acc.editions.reads.find_ids(author="Jinpa") == [s["viawork"]]    # translator counts
        # intersection: Tantra ∩ Tsongkhapa = just the direct edition
        assert acc.editions.reads.find_ids(subject="Buddhism/Tantra", author="Tsongkhapa") == [s["direct"]]


def test_since_date_and_since_edition(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        assert acc.editions.reads.find_ids(since_date="2026-06-01") == [s["viawork"]]
        assert s["direct"] not in acc.editions.reads.find_ids(since_edition=s["direct"])


def test_all_excludes_tombstones_and_is_id_ordered(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        eds = acc.editions.reads.all()
        ids = [e.id for e in eds]
        assert s["dead"] not in ids                                  # tombstone excluded
        assert {s["direct"], s["viawork"], s["other"]} <= set(ids)
        assert ids == sorted(ids)                                    # id-ordered


def test_first_isbn_own_column_then_edition_isbn_alias(tmp_path):
    db = tmp_path / "isbn.db"
    c = init_db(db)
    own = c.execute("INSERT INTO edition (title, isbn) VALUES ('Own', '978-own')").lastrowid
    via = c.execute("INSERT INTO edition (title) VALUES ('Via')").lastrowid
    c.execute("INSERT INTO edition_isbn (edition_id, isbn) VALUES (?, '978-alias')", (via,))
    none = c.execute("INSERT INTO edition (title) VALUES ('None')").lastrowid
    c.commit(); c.close()
    with system_access(db) as acc:
        assert acc.editions.reads.first_isbn(own) == "978-own"      # own column wins
        assert acc.editions.reads.first_isbn(via) == "978-alias"    # falls back to the alias
        assert acc.editions.reads.first_isbn(none) is None


def test_content_source_readers(tmp_path):
    db = tmp_path / "text.db"
    c = init_db(db)
    e1 = c.execute("INSERT INTO edition (title) VALUES ('Book One')").lastrowid
    e2 = c.execute("INSERT INTO edition (title) VALUES ('Book Two')").lastrowid
    c.execute("INSERT INTO edition_text (edition_id, page, content) VALUES (?, 1, 'alpha')", (e1,))
    c.execute("INSERT INTO edition_text (edition_id, page, content) VALUES (?, 2, 'beta')", (e1,))
    c.execute("INSERT INTO edition_text (edition_id, page, content) VALUES (?, 1, 'gamma')", (e2,))
    c.commit(); c.close()
    with system_access(db) as acc:
        passages = acc.editions.reads.text_passages()
        assert len(passages) == 3 and all(len(r) == 4 for r in passages)   # (id, edition_id, page, content)
        assert set(acc.editions.reads.edition_ids_with_text()) == {e1, e2}
        n, mx, total = acc.editions.reads.text_signature()
        assert n == 3 and total == len("alpha") + len("beta") + len("gamma")


def test_no_match_returns_none_vs_empty(tmp_path):
    s = _seed(tmp_path)
    with system_access(s["db"]) as acc:
        assert acc.editions.reads.find_ids(subject="Nonexistent") is None     # no such subject
        assert acc.editions.reads.find_ids(author="Nobody") is None           # no such person
