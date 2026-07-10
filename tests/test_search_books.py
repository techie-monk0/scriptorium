"""Structured metadata search (catalogue/search.py find_books + CLI).

Proves the two requirements: canonical matching (diacritic- AND digraph-insensitive)
and matching across ALL aliases of a work/person, not just the primary spelling.
"""
from __future__ import annotations

from catalogue.db_store import add_alias, init_db
from catalogue.services.search import find_books


def _db():
    return init_db(":memory:")


def _person(db, name, *aliases):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid
    add_alias(db, "person", pid, name, "other")
    for a in aliases:
        add_alias(db, "person", pid, a, "other")
    return pid


def _work(db, *aliases):
    wid = db.execute("INSERT INTO work (notes) VALUES (NULL)").lastrowid
    for a in aliases:
        add_alias(db, "work", wid, a, "english")
    return wid


def _edition(db, title, **kw):
    cols = ["title"] + list(kw)
    ph = ", ".join("?" for _ in cols)
    return db.execute(f"INSERT INTO edition ({', '.join(cols)}) VALUES ({ph})",
                      [title, *kw.values()]).lastrowid


def _link(db, eid, wid):
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) "
               "VALUES (?, ?, 1)", (eid, wid))


def _contrib(db, wid, pid, role="author"):
    db.execute("INSERT INTO work_author (work_id, person_id, role) "
               "VALUES (?, ?, ?)", (wid, pid, role))


def _holding(db, eid, path):
    db.execute("INSERT INTO holding (edition_id, file_path, form) "
               "VALUES (?, ?, 'electronic')", (eid, path))


def _seed(db):
    # A book whose work has a diacritic native title + a plain alias, by Śāntideva.
    w = _work(db, "Bodhicaryāvatāra", "A Guide to the Bodhisattva's Way of Life")
    p = _person(db, "Śāntideva", "Shantideva")
    _contrib(db, w, p)
    e = _edition(db, "The Way of the Bodhisattva")
    _link(db, e, w)
    _holding(db, e, "/books/Shantideva - Way of the Bodhisattva.pdf")
    return e


def test_work_title_matches_without_diacritics():
    db = _db(); e = _seed(db)
    # query has NO diacritics; alias is stored WITH them → must still match
    rows = find_books(db, work_title="Bodhicaryavatara")
    assert [r["edition_id"] for r in rows] == [e]
    assert rows[0]["title"] == "The Way of the Bodhisattva"
    assert "Shantideva - Way of the Bodhisattva.pdf" in rows[0]["files"]


def test_work_title_matches_an_alternate_alias():
    db = _db(); e = _seed(db)
    # the English alias, not the title we'd display
    assert [r["edition_id"] for r in find_books(db, work_title="bodhisattva's way")] == [e]


def test_author_matches_by_alias_and_diacritics():
    db = _db(); e = _seed(db)
    # primary name has diacritics; query is plain ASCII → matches via fold
    assert [r["edition_id"] for r in find_books(db, authors=["Santideva"])] == [e]
    # and via the explicit alias
    assert [r["edition_id"] for r in find_books(db, authors=["Shantideva"])] == [e]


def test_digraph_insensitive():
    db = _db(); e = _seed(db)
    # fold_key collapses 'sh'→'s': "Santideva" already covered; check work side too
    # ("Bodhicaryavatara" has no digraph, so add a digraph case on the author)
    assert find_books(db, authors=["Shantideva"]) == find_books(db, authors=["Santideva"])
    assert find_books(db, authors=["Santideva"])[0]["edition_id"] == e


def test_book_title_matches_edition_own_title():
    db = _db(); e = _seed(db)
    assert [r["edition_id"] for r in find_books(db, book_title="way of the bodhisattva")] == [e]


def test_multiple_fields_are_anded():
    db = _db(); e = _seed(db)
    # both correct → match
    assert [r["edition_id"] for r in find_books(
        db, work_title="Bodhicaryavatara", authors=["Santideva"])] == [e]
    # right work, wrong author → no match (intersection)
    assert find_books(db, work_title="Bodhicaryavatara", authors=["Nagarjuna"]) == []


def test_no_criteria_returns_empty():
    db = _db(); _seed(db)
    assert find_books(db) == []


def test_no_match_returns_empty():
    db = _db(); _seed(db)
    assert find_books(db, work_title="Pramanavarttika") == []


# ── ordinal- and office-aware author matching (Dalai Lama) ────────────────────────
def _seed_dalai(db):
    """A book authored by a person stored ONLY as 'Dalai Lama XIV'."""
    w = _work(db, "The World of Tibetan Buddhism")
    p = _person(db, "Dalai Lama XIV")
    _contrib(db, w, p)
    e = _edition(db, "The World of Tibetan Buddhism")
    _link(db, e, w)
    return e


def test_dalai_lama_ordinal_forms_unify():
    db = _db(); e = _seed_dalai(db)
    # stored as "Dalai Lama XIV"; all of these must return the same book
    for q in ["Dalai Lama XIV", "14th Dalai Lama", "Fourteenth Dalai Lama",
              "Dalai Lama, XIV"]:
        assert [r["edition_id"] for r in find_books(db, authors=[q])] == [e], q


def test_dalai_lama_personal_name_unifies():
    db = _db(); e = _seed_dalai(db)
    # personal name resolves to the same incumbent via canonical_dalai_lama
    assert [r["edition_id"] for r in find_books(db, authors=["Tenzin Gyatso"])] == [e]


def test_different_incumbent_does_not_match():
    db = _db(); _seed_dalai(db)
    # the 7th is a different person — ordinal disagreement must block the match
    assert find_books(db, authors=["7th Dalai Lama"]) == []
    assert find_books(db, authors=["Seventh Dalai Lama"]) == []
