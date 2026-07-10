"""Tests for the identifier-first edition metadata pass (catalogue/edition_resolve.py).

Authority sources are faked (no network); the LLM fallback uses a fake ladder.
Everything else runs against a real schema.sql DB.
"""
from __future__ import annotations

import json

from catalogue.services import edition_resolve as ER
from catalogue.services.classify import Rung
from catalogue.db_store import add_alias, init_db
from catalogue.services.edition_verify import EditionRecord


# ── fakes ──────────────────────────────────────────────────────────────────────────
class _FakeSource:
    name = "fakeauth"

    def __init__(self, by_isbn=None, by_lccn=None):
        self._isbn = by_isbn or {}
        self._lccn = by_lccn or {}

    def by_isbn(self, isbn):
        return list(self._isbn.get(isbn, []))

    def by_lccn(self, lccn):
        return list(self._lccn.get(lccn, []))


def _rec(title, **kw):
    return EditionRecord(source="fakeauth", title=title, **kw)


class _LLMClient:
    def __init__(self, resp):
        self._resp = resp

    def chat(self, messages, *, max_tokens=512, json_only=True):
        return {"content": self._resp}


def _ladder(resp):
    return [Rung("fake", _LLMClient(resp))]


# A page-LLM that returns no title — use when a test exercises the ISBN path only
# (so the page-title side of the merge is empty and the ISBN title is chosen).
_NO_PAGE = _ladder('{"title": "", "confidence": 0.0}')


def _page(title):
    """A page-LLM ladder that returns `title` as the page-derived title."""
    return _ladder(json.dumps({"title": title, "confidence": 0.95}))


class _MapLLM:
    """Page-LLM that returns a title chosen by a needle in the page text."""
    def __init__(self, mapping):
        self.mapping = mapping

    def chat(self, messages, *, max_tokens=512, json_only=True):
        u = messages[-1]["content"]
        for needle, title in self.mapping.items():
            if needle in u:
                return {"content": json.dumps({"title": title, "confidence": 0.95})}
        return {"content": '{"title": "", "confidence": 0.0}'}


def _map_page(mapping):
    return [Rung("fake", _MapLLM(mapping))]


# ── DB seed helpers ──────────────────────────────────────────────────────────────────
def _book(db, *, edition_title, work_title, text, file_hash="h1"):
    eid = db.execute("INSERT INTO edition (title) VALUES (?)", (edition_title,)).lastrowid
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, work_title, "english")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,0)",
               (eid, wid))
    db.execute("INSERT INTO holding (edition_id, file_hash) VALUES (?, ?)", (eid, file_hash))
    db.execute("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
               "VALUES (?, 1, ?)", (file_hash, text))
    db.commit()
    return eid, wid


def _edition(db, eid):
    return db.execute("SELECT title, isbn, publisher, year FROM edition WHERE id=?",
                      (eid,)).fetchone()


def _contribs(db, wid):
    return db.execute(
        "SELECT p.primary_name, wa.role FROM work_author wa "
        "JOIN person p ON p.id = wa.person_id WHERE wa.work_id=? ORDER BY p.primary_name",
        (wid,)).fetchall()


def _queue(db):
    return db.execute("SELECT id, payload_json FROM review_queue "
                      "WHERE item_type='edition_metadata'").fetchall()


# ── apply edition fields (ISBN in front matter); NO contributor linking ─────────────
def test_isbn_resolves_and_applies_edition_fields(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, wid = _book(db, edition_title="JUNK -- Author -- 9781614298939 -- Anna",
                     work_title="Wrong Alias",
                     text="copyright page ISBN 978-1-61429-893-9")
    src = _FakeSource(by_isbn={"9781614298939": [_rec(
        "How to Meditate on the Stages of the Path",
        authors=("Kathleen McDonald",), publisher="Wisdom Publications", year=2024,
        isbn="9781614298939")]})
    status = ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE)
    assert status == "applied"

    title, isbn, pub, year = _edition(db, eid)
    assert title == "How to Meditate on the Stages of the Path"
    assert isbn == "9781614298939" and pub == "Wisdom Publications" and year == 2024
    # work primary alias replaced; old kept as a 'filename' alias
    aliases = db.execute("SELECT text, scheme FROM work_alias WHERE work_id=? ORDER BY id",
                         (wid,)).fetchall()
    assert aliases[0] == ("How to Meditate on the Stages of the Path", "english")
    assert ("Wrong Alias", "filename") in aliases
    # contributors are NOT written (role-aware passes own them); shown in payload only
    assert _contribs(db, wid) == []
    p = json.loads(_queue(db)[0][1])
    assert p["applied"] is True and p["id_scheme"] == "isbn"
    assert p["authors"] == ["Kathleen McDonald"]      # carried for review display


# ── REQUIREMENT: identifiers come from the OCR page, NEVER the filename ──────────────
def test_isbn_taken_from_page_not_filename(tmp_path):
    """The filename carries one ISBN, the copyright page another. The page must win."""
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db,
                   edition_title="Series Set -- 9781559391887 -- Anna",   # filename ISBN
                   work_title="x",
                   text="Copyright page. ISBN 978-1-55939-345-4")          # the book's own
    src = _FakeSource(by_isbn={
        "9781559393454": [_rec("This Volume's Real Title")],   # the page ISBN
        "9781559391887": [_rec("WRONG — the filename/set ISBN")],
    })
    assert ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE) == "applied"
    p = json.loads(_queue(db)[0][1])
    assert p["id_value"] == "9781559393454"                   # page, not filename
    assert _edition(db, eid)[0] == "This Volume's Real Title"


def test_isbn_found_deep_in_text_not_just_head(tmp_path):
    """The copyright/CIP page can be in the MIDDLE of the cached text (out-of-order
    EPUB extraction). resolve_edition must scan the whole text, not a head window."""
    db = init_db(tmp_path / "t.db")
    deep = ("chapter body " * 5000
            + "Library of Congress Cataloging-in-Publication Data ISBN 978-1-61429-472-6"
            + " endnotes " * 5000)            # ISBN mid-text, head is body, tail is notes
    eid, _ = _book(db, edition_title="x", work_title="x", text=deep)
    src = _FakeSource(by_isbn={"9781614294726": [_rec("The Found Title")]})
    assert ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE) == "applied"
    assert _edition(db, eid)[0] == "The Found Title"
    assert json.loads(_queue(db)[0][1])["id_value"] == "9781614294726"


def test_filename_only_isbn_is_ignored(tmp_path):
    """An ISBN present ONLY in the filename (page has none) must NOT be used."""
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="Book -- 9781559391887 -- Anna", work_title="x",
                   text="a clean page with no identifier printed on it")
    src = _FakeSource(by_isbn={"9781559391887": [_rec("Should never be used")]})
    assert ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE) == "no_identifier"
    assert _queue(db) == []
    assert _edition(db, eid)[1] is None                       # no isbn stored either


def test_multivolume_volumes_resolve_to_distinct_records(tmp_path):
    """Two volumes whose FILENAMES share the set's (Book 1's) ISBN, but whose pages
    carry their own distinct ISBNs, must resolve to DIFFERENT records — not collapse
    onto Book 1 (the bug)."""
    db = init_db(tmp_path / "t.db")
    v1, _ = _book(db, edition_title="Treasury, Book 1 -- 9781559391887", work_title="v1",
                  text="Book One. ISBN 978-1-55939-188-7", file_hash="hv1")
    v2, _ = _book(db, edition_title="Treasury, Book 5 -- 9781559391887", work_title="v2",
                  text="Book Five. ISBN 978-1-55939-066-8", file_hash="hv2")
    src = _FakeSource(by_isbn={
        "9781559391887": [_rec("Treasury, Book 1")],
        "9781559390668": [_rec("Treasury, Book 5: Buddhist Ethics")],
    })
    ER.resolve_edition(db, v1, sources=[src], ladder=_NO_PAGE)
    ER.resolve_edition(db, v2, sources=[src], ladder=_NO_PAGE)
    assert _edition(db, v1)[0] == "Treasury, Book 1"
    assert _edition(db, v2)[0] == "Treasury, Book 5: Buddhist Ethics"   # NOT Book 1


def test_sentence_case_title_is_titlecased(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x -- 9781614298939", work_title="x", text="copyright page ISBN 9781614298939")
    src = _FakeSource(by_isbn={"9781614298939": [_rec("The Dalai Lamas on tantra")]})
    ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE)
    assert _edition(db, eid)[0] == "The Dalai Lamas on Tantra"    # 'tantra' fixed, 'on' kept


# ── scheme-agnostic: LCCN-only book resolves via the same path ──────────────────────
def test_lccn_only_resolves(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, wid = _book(db, edition_title="No ISBN Here", work_title="No ISBN Here",
                     text="Library of Congress Control Number: 2009925465")
    src = _FakeSource(by_lccn={"2009925465": [_rec("Authoritative Title",
                                                   authors=("Some Author",))]})
    assert ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE) == "applied"
    assert _edition(db, eid)[0] == "Authoritative Title"
    p = json.loads(_queue(db)[0][1])
    assert p["id_scheme"] == "lccn"


# ── no identifier / miss ────────────────────────────────────────────────────────────
def test_no_identifier(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="Plain Title", work_title="Plain Title",
                   text="a page with no isbn or lccn")
    assert ER.resolve_edition(db, eid, sources=[_FakeSource()], ladder=_NO_PAGE) == "no_identifier"


def test_miss_keeps_isbn_and_does_not_llm(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x -- 9781614298939", work_title="x", text="copyright page ISBN 9781614298939")
    assert ER.resolve_edition(db, eid, sources=[_FakeSource()], ladder=_NO_PAGE) == "miss"
    assert _queue(db) == []                           # nothing applied/queued
    # the validated ISBN is still recorded for a later retry; title untouched
    title, isbn, _, _ = _edition(db, eid)
    assert isbn == "9781614298939"
    assert title == "x -- 9781614298939"


# ── merge: keep the FULLER of the ISBN title and the page (LLM) title ───────────────
def test_page_title_wins_when_authority_is_generic(tmp_path):
    """Multi-volume case: OpenLibrary returns a generic series title, but the page
    has the full volume title → the page title wins (and the ISBN is still stored)."""
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x", work_title="x",
                   text="The Treasury of Knowledge, Book Five: Buddhist Ethics. "
                        "ISBN 978-1-55939-066-8")
    src = _FakeSource(by_isbn={"9781559390668": [_rec("Buddhist ethics")]})   # generic
    page = _page("The Treasury of Knowledge, Book Five: Buddhist Ethics")
    assert ER.resolve_edition(db, eid, sources=[src], ladder=page) == "applied"
    p = json.loads(_queue(db)[0][1])
    assert p["title_source"] == "page"
    assert _edition(db, eid)[0] == "The Treasury of Knowledge, Book Five: Buddhist Ethics"
    assert _edition(db, eid)[1] == "9781559390668"        # ISBN still stored


def test_cip_title_wins_over_isbn_and_page(tmp_path):
    """The structured LoC CIP 'Title:' field (with subtitle) is authoritative — it
    beats both OpenLibrary (dropped subtitle) and an over-reading page LLM."""
    db = init_db(tmp_path / "t.db")
    cip = ("Library of Congress Cataloging-in-Publication Data\n"
           "Names: McDonald, Kathleen, 1952– author.\n"
           "Title: How to meditate on the stages of the path: a guide to the Lamrim "
           "/ by Kathleen McDonald.\n"
           "Identifiers: LCCN 2024008414 | ISBN 9781614298939 (paperback)\n")
    eid, _ = _book(db, edition_title="x", work_title="x", text=cip)
    src = _FakeSource(by_isbn={"9781614298939": [_rec("How to Meditate on the Stages of the Path")]})
    page = _page("How to Meditate on the Stages of the Path Death and Impermanence")  # over-read
    assert ER.resolve_edition(db, eid, sources=[src], ladder=page) == "applied"
    p = json.loads(_queue(db)[0][1])
    assert p["title_source"] == "cip"
    assert _edition(db, eid)[0] == "How to Meditate on the Stages of the Path: A Guide to the Lamrim"


def test_cip_title_rescues_isbn_lookup_miss(tmp_path):
    """Even when the ISBN doesn't resolve to a record, a CIP title still titles the
    book (and the ISBN is stored)."""
    db = init_db(tmp_path / "t.db")
    cip = ("Library of Congress Cataloging-in-Publication Data\n"
           "Names: Someone, A., author.\n"
           "Title: A Real Book: the subtitle / by Someone.\n"
           "Identifiers: ISBN 978-1-61429-893-9\n")
    eid, _ = _book(db, edition_title="x", work_title="x", text=cip)
    assert ER.resolve_edition(db, eid, sources=[_FakeSource()], ladder=_NO_PAGE) == "applied"
    p = json.loads(_queue(db)[0][1])
    assert p["title_source"] == "cip"
    assert _edition(db, eid)[0] == "A Real Book: The Subtitle"
    assert _edition(db, eid)[1] == "9781614298939"        # ISBN stored despite lookup miss


# (CIP-title extraction unit tests now live in tests/test_cip.py — the parser moved
#  to catalogue/cip.py and edition_resolve calls cip.parse_cip.)


# ── title-confirmation gate: an ISBN whose lookup title disagrees with the page/CIP
#    title is the WRONG book → dropped ───────────────────────────────────────────────
def test_isbn_dropped_when_lookup_title_disagrees_with_page(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x", work_title="x",
                   text="copyright page ISBN 9781614298939")
    # the ISBN resolves to a COMPLETELY different book; the page says otherwise
    src = _FakeSource(by_isbn={"9781614298939": [_rec("Gardening for Beginners",
                                                      publisher="Wrong Pub", year=1999)]})
    page = _page("The Tibetan Book of the Dead: A Biography")
    ER.resolve_edition(db, eid, sources=[src], ladder=page)
    title, isbn, pub, year = _edition(db, eid)
    assert title == "The Tibetan Book of the Dead: A Biography"   # page wins, not the wrong ISBN
    assert isbn is None and pub is None and year is None          # wrong ISBN's metadata dropped


def test_isbn_kept_when_lookup_title_agrees(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x", work_title="x",
                   text="copyright page ISBN 9781614298939")
    src = _FakeSource(by_isbn={"9781614298939": [_rec(
        "How to Meditate on the Stages of the Path", publisher="Wisdom", year=2024)]})
    page = _page("How to Meditate on the Stages of the Path")     # agrees
    ER.resolve_edition(db, eid, sources=[src], ladder=page)
    _, isbn, pub, year = _edition(db, eid)
    assert isbn == "9781614298939" and pub == "Wisdom" and year == 2024


# ── merge refinement: page beats a clean ISBN title ONLY if it adds structure ───────
def test_page_over_read_loses_to_clean_isbn(tmp_path):
    """No CIP. The LLM over-reads ('… Death and Impermanence', no subtitle/volume)
    past a clean ISBN title → the ISBN title wins (the e1-without-CIP case)."""
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x", work_title="x", text="ISBN 9781614298939")
    src = _FakeSource(by_isbn={"9781614298939": [_rec("How to Meditate on the Stages of the Path")]})
    page = _page("How to Meditate on the Stages of the Path Death and Impermanence")
    ER.resolve_edition(db, eid, sources=[src], ladder=page)
    p = json.loads(_queue(db)[0][1])
    assert p["title_source"] == "isbn"
    assert _edition(db, eid)[0] == "How to Meditate on the Stages of the Path"


def test_freeform_cip_fragment_loses_to_structured_page(tmp_path):
    """A free-form CIP parse that yields a structureless FRAGMENT (e226's 'Journey to
    Tibet') must NOT clobber a correct, structured page title."""
    db = init_db(tmp_path / "t.db")
    # CIP region where the loose parse would grab a fragment ("Journey to Tibet")
    text = ("ISBN 9781559393454. Library of Congress Cataloging-in-Publication Data. "
            "Four. Journey to Tibet / translated by the Kalu Rinpoche group. p. cm.")
    eid, _ = _book(db, edition_title="x", work_title="x", text=text)
    src = _FakeSource(by_isbn={"9781559393454": [_rec("The Treasury of Knowledge")]})
    page = _page("The Treasury of Knowledge, Books Two, Three, and Four: Buddhism's Journey to Tibet")
    ER.resolve_edition(db, eid, sources=[src], ladder=page)
    title, _, _, _ = _edition(db, eid)
    assert "Books Two, Three, and Four" in title          # the page title, not the fragment
    assert title != "Journey to Tibet"


def test_page_with_real_subtitle_beats_generic_isbn(tmp_path):
    """Page adds a real ':' subtitle + volume the generic ISBN title lacks → page."""
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x", work_title="x", text="ISBN 9781559393898")
    src = _FakeSource(by_isbn={"9781559393898": [_rec("The Treasury of Knowledge")]})
    page = _page("The Treasury of Knowledge, Book Six, Parts One and Two: Indo-Tibetan Classical Learning")
    ER.resolve_edition(db, eid, sources=[src], ladder=page)
    p = json.loads(_queue(db)[0][1])
    assert p["title_source"] == "page"
    assert "Book Six" in _edition(db, eid)[0]


def test_all_caps_page_title_is_titlecased(tmp_path):
    """A page that prints the title in ALL CAPS ('BUDDHIST ETHICS') is normalized to
    title case when stored (long all-caps words → title case; acronyms kept)."""
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x", work_title="x",
                   text="BUDDHIST ETHICS ISBN 9781559390668")
    src = _FakeSource(by_isbn={"9781559390668": [_rec("Buddhist ethics")]})
    ER.resolve_edition(db, eid, sources=[src], ladder=_page("BUDDHIST ETHICS"))
    assert _edition(db, eid)[0] == "Buddhist Ethics"        # not "BUDDHIST ETHICS"


def test_isbn_title_wins_when_fuller(tmp_path):
    """Clean single book: the ISBN title is the fuller/cleaner one → it wins over a
    shorter page reading."""
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x", work_title="x",
                   text="How to Meditate ... ISBN 9781614298939")
    src = _FakeSource(by_isbn={"9781614298939":
                               [_rec("How to Meditate on the Stages of the Path")]})
    page = _page("How to Meditate")                       # page reading is shorter
    assert ER.resolve_edition(db, eid, sources=[src], ladder=page) == "applied"
    p = json.loads(_queue(db)[0][1])
    assert p["title_source"] == "isbn"
    assert _edition(db, eid)[0] == "How to Meditate on the Stages of the Path"


# ── reject reverts everything (title, scalars, alias, contributor) ──────────────────
def test_reject_reverts_full_record(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, wid = _book(db, edition_title="OLD -- 9781614298939", work_title="Old Work",
                     text="ISBN 978-1-61429-893-9")
    src = _FakeSource(by_isbn={"9781614298939": [_rec(
        "New Title", authors=("New Author",), publisher="Pub", year=2020,
        isbn="9781614298939")]})
    ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE)
    iid = _queue(db)[0][0]
    assert ER.reject_edition_metadata(db, iid) is True

    title, isbn, pub, year = _edition(db, eid)
    assert title == "OLD -- 9781614298939"           # edition scalars reverted
    assert isbn is None and pub is None and year is None
    assert db.execute("SELECT text, scheme FROM work_alias WHERE work_id=? ORDER BY id",
                      (wid,)).fetchall() == [("Old Work", "english")]   # filename alias gone
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (iid,)).fetchone()[0] == "rejected"


def test_accept_resolves_applied_item(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x -- 9781614298939", work_title="x", text="copyright page ISBN 9781614298939")
    src = _FakeSource(by_isbn={"9781614298939": [_rec("T", authors=("A",))]})
    ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE)
    iid = _queue(db)[0][0]
    assert ER.accept_edition_metadata(db, iid) is True
    assert db.execute("SELECT status FROM review_queue WHERE id=?", (iid,)).fetchone()[0] == "resolved"
    assert ER.accept_edition_metadata(db, iid) is False     # no longer pending


def test_rerun_is_idempotent(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x -- 9781614298939", work_title="x", text="copyright page ISBN 9781614298939")
    src = _FakeSource(by_isbn={"9781614298939": [_rec("T")]})
    ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE)
    assert ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE) == "already"
    assert len(_queue(db)) == 1


# ── the walk: partition by identifier (both partitions in one run) ──────────────────
def test_walk_partitions_identifier_vs_llm(tmp_path):
    db = init_db(tmp_path / "t.db")
    a, _ = _book(db, edition_title="A -- 9781614298939", work_title="A",
                 text="ISBN 9781614298939", file_hash="ha")        # has ISBN → identifier
    b, _ = _book(db, edition_title="B junk", work_title="B junk",
                 text="The Real B Title page", file_hash="hb")     # no id → LLM
    src = _FakeSource(by_isbn={"9781614298939": [_rec("Authoritative Alpha")]})
    # page-LLM keyed by content so book A (identifier) isn't handed book B's title;
    # A's page yields nothing → its ISBN title wins.
    ladder = _map_page({"The Real B Title page": "The Real B Title"})

    tally = ER.resolve_all_editions(db, sources=[src], ladder=ladder)
    assert tally["id_applied"] == 1 and tally["llm_applied"] == 1
    assert _edition(db, a)[0] == "Authoritative Alpha"
    assert _edition(db, b)[0] == "The Real B Title"


def test_only_partitions_run_independently(tmp_path):
    db = init_db(tmp_path / "t.db")
    a, _ = _book(db, edition_title="A -- 9781614298939", work_title="A",
                 text="ISBN 9781614298939", file_hash="ha")        # identifier book
    b, _ = _book(db, edition_title="B junk", work_title="B junk",
                 text="The Real B Title page", file_hash="hb")     # llm book
    src = _FakeSource(by_isbn={"9781614298939": [_rec("Authoritative Alpha")]})

    # identifier-only job: touches A (page yields nothing → ISBN title), skips B
    t1 = ER.resolve_all_editions(db, sources=[src], ladder=_NO_PAGE, only="identifier")
    assert t1["id_applied"] == 1 and t1["skipped"] == 1
    assert _edition(db, a)[0] == "Authoritative Alpha"
    assert _edition(db, b)[0] == "B junk"            # untouched by the identifier job

    # llm-only job: touches B, skips A
    t2 = ER.resolve_all_editions(db, ladder=_map_page({"The Real B Title page": "The Real B Title"}),
                                 only="llm")
    assert t2["llm_applied"] == 1 and t2["skipped"] == 1
    assert _edition(db, b)[0] == "The Real B Title"


def test_identifier_apply_supersedes_pending_llm_title(tmp_path):
    db = init_db(tmp_path / "t.db")
    eid, _ = _book(db, edition_title="x -- 9781614298939", work_title="x",
                   text="ISBN 9781614298939 front matter")
    # a stale LLM title proposal already sits in the queue for this book
    db.execute("INSERT INTO review_queue (item_type, payload_json) VALUES "
               "('title_proposal', ?)", (json.dumps({"edition_id": eid, "applied": False,
                                                      "new_title": "LLM Guess"}),))
    db.commit()
    src = _FakeSource(by_isbn={"9781614298939": [_rec("Authoritative Title")]})
    ER.resolve_edition(db, eid, sources=[src], ladder=_NO_PAGE)
    # the LLM proposal was superseded (resolved), not left dangling
    tp = db.execute("SELECT status FROM review_queue WHERE item_type='title_proposal'").fetchone()
    assert tp[0] == "resolved"
