"""Tests for the WorkAuthorityResolver (catalogue/work_authority.py).

The engine logic is exercised with stub sources (no network); the 84000 source
is exercised against a crafted local TEI file (no network); `apply_to_work` is
exercised against a real DB.
"""
from __future__ import annotations

import catalogue.services.work_authority as WA
from catalogue.db_store import add_alias, init_db


class _StubSource(WA.WorkAuthoritySource):
    def __init__(self, name, records):
        self.name = name
        self._records = records

    def lookup(self, title, *, language=None, aliases=()):
        return list(self._records)


def _stub_resolver(*sources, **kw):
    return WA.WorkAuthorityResolver(sources=list(sources), db=None, **kw)


def _work_with_title(db, title):
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, title, "english")
    return wid


def _rec(source, title, **kw):
    return WA.WorkAuthorityRecord(source=source, title=title, **kw)


# ── similarity ─────────────────────────────────────────────────────────────────
def test_similar_is_diacritic_insensitive():
    assert WA._similar("Bodhicaryavatara", "Bodhicaryāvatāra") == 1.0
    assert WA._similar("Bodhicaryavatara", "Pramanavarttika") < 0.5


# ── Tibetan syllable-spacing variants ────────────────────────────────────────────
def test_spacing_variants_respaces_whole_title():
    # "Lamrim Chenmo" → the space-collapsed form + ONE fully re-spaced whole title
    # ("lam rim chen mo"), not per-token fragments.
    v = WA.tibetan_spacing_variants("Lamrim Chenmo")
    assert "lam rim chen mo" in v                 # the form that matches Wikidata
    assert "LamrimChenmo" in v                    # space-collapsed
    assert "lam rim" not in v and "chen mo" not in v   # no fragments


def test_spacing_variants_skip_english_titles():
    # Punctuation ⇒ English (user's rule); function words / long titles ⇒ English.
    assert WA.tibetan_spacing_variants("Illuminating the Intent") == []
    assert WA.tibetan_spacing_variants("The Words of My Perfect Teacher") == []
    assert WA.tibetan_spacing_variants("Mind, and Its World") == []      # punctuation
    # IAST Sanskrit (diacritics) is not romanized Tibetan → skip.
    assert WA.tibetan_spacing_variants("Bodhicaryāvatāra") == []


def test_maybe_tibetan_signal():
    assert WA._maybe_tibetan("Lamrim Chenmo")
    assert WA._maybe_tibetan("Kunzang Lamai Shelung")
    assert not WA._maybe_tibetan("A History of the Path")   # function words
    assert not WA._maybe_tibetan("")


# ── alias-scoring: a cross-script hit the search validated clears the title-gate ──
def test_resolve_matches_via_item_alias_not_english_label():
    """Found "lam rim chen mo" → an item whose ENGLISH title is "The Great
    Treatise…". The query can't match that English label, but it matches the item's
    own alias — which the source carries — so the title-gate must still pass."""
    rec = _rec("wikidata", "The Great Treatise on the Stages of the Path",
               authors=("Tsongkhapa",), aliases=("lam rim chen mo", "Lamrim Chenmo"),
               author_ids=({"name": "Tsongkhapa", "external_id": "wikidata:Q323439",
                            "extra_ids": {}},),
               canonical_system="wikidata", canonical_number="Q323439")
    r = _stub_resolver(_StubSource("wikidata", [rec]))
    c = r.resolve("Lamrim Chenmo")                # phonetic DB title, no English
    assert c.verdict in ("verified", "candidate")
    assert c.authors == ["Tsongkhapa"]
    assert [a["external_id"] for a in c.author_ids] == ["wikidata:Q323439"]


def test_resolve_english_title_matches_directly_without_aliases():
    """The efficient path: a real English DB title matches the item's English title
    head-on — no aliases needed."""
    rec = _rec("wikidata", "The Great Treatise on the Stages of the Path",
               authors=("Tsongkhapa",))
    r = _stub_resolver(_StubSource("wikidata", [rec]))
    c = r.resolve("The Great Treatise on the Stages of the Path")
    assert c.authors == ["Tsongkhapa"]


def test_resolve_does_not_match_unrelated_item_via_aliases():
    """The gate still rejects junk: an unrelated item whose aliases don't resemble
    the query stays out, even though aliases are now in scope."""
    rec = _rec("wikidata", "Some Unrelated Sutra",
               authors=("X",), aliases=("an unrelated alias", "another one"))
    r = _stub_resolver(_StubSource("wikidata", [rec]))
    assert r.resolve("Lamrim Chenmo").verdict == "none"


# ── consensus ──────────────────────────────────────────────────────────────────
def test_two_sources_agree_verifies():
    r = WA.WorkAuthorityResolver(sources=[
        _StubSource("a", [_rec("a", "Bodhicaryavatara", authors=("Śāntideva",))]),
        _StubSource("b", [_rec("b", "Bodhicaryāvatāra", authors=("Shantideva",),
                                canonical_system="toh", canonical_number="3871")]),
    ], db=None)
    c = r.resolve("Bodhicaryāvatāra")
    assert c.verdict == "verified"
    assert c.agreement == 2
    assert len(c.authors) == 1                 # the two spellings folded to one
    assert c.canonical_number == "3871"


def test_single_source_is_candidate():
    r = WA.WorkAuthorityResolver(sources=[
        _StubSource("a", [_rec("a", "Some Treatise", authors=("Author One",))]),
    ], db=None)
    c = r.resolve("Some Treatise")
    assert c.verdict == "candidate"
    assert c.authors == ["Author One"]
    assert c.agreement == 1


def test_single_strong_catalog_hit_verifies():
    r = WA.WorkAuthorityResolver(sources=[
        _StubSource("84000", [_rec("84000", "The Sutra of the Ten Bhumis",
                                    authors=("Buddha",), canonical_system="toh",
                                    canonical_number="44")]),
    ], db=None)
    c = r.resolve("The Sutra of the Ten Bhumis")
    assert c.verdict == "verified"             # catalog id + strong title + author
    assert c.canonical_number == "44"


def test_single_wikidata_hit_is_candidate_not_verified():
    """Regression: a lone Wikidata hit carries a Q-id in canonical_number, but
    Wikidata is an identity hub, not an authorship catalogue. An exact same-title
    Wikidata match on a generic title (the "Buddhist Ethics"→Robert Ford Campany /
    "Appearance and Reality"→F.H. Bradley false positives) must stay a CANDIDATE,
    and the Q-id must NOT be promoted into work.canonical_number."""
    r = WA.WorkAuthorityResolver(sources=[
        _StubSource("wikidata", [_rec("wikidata", "Buddhist Ethics",
                                       authors=("Robert Ford Campany",),
                                       external_id="Q99533550",
                                       canonical_system="wikidata",
                                       canonical_number="Q99533550")]),
    ], db=None)
    c = r.resolve("Buddhist Ethics")
    assert c.verdict == "candidate"            # NOT verified — single non-Toh source
    assert c.canonical_number is None          # a Q-id is not a canonical catalogue id
    assert c.canonical_system is None
    # the Q-id is still available for audit/cross-linking via external_ids
    assert c.external_ids.get("wikidata") == "Q99533550"


def test_wikidata_plus_84000_agreement_verifies():
    """A second source agreeing on the author DOES verify the Wikidata hit (the
    documented escape hatch), and a Toh number present anywhere becomes canonical."""
    r = WA.WorkAuthorityResolver(sources=[
        _StubSource("wikidata", [_rec("wikidata", "Letter to a Friend",
                                       authors=("Nagarjuna",),
                                       canonical_system="wikidata",
                                       canonical_number="Q123")]),
        _StubSource("84000", [_rec("84000", "Letter to a Friend",
                                   authors=("Nagarjuna",), canonical_system="toh",
                                   canonical_number="4182")]),
    ], db=None)
    c = r.resolve("Letter to a Friend")
    assert c.verdict == "verified"             # two sources agree on the author
    assert c.canonical_system == "toh" and c.canonical_number == "4182"


# ── distinctive-word containment gate ─────────────────────────────────────────────
def test_titles_contained_rescues_buried_native_title():
    # our verbose title CONTAINS the bare native title (catalog side shorter)…
    assert WA._titles_contained(
        "Nagarjunas Middle Way: The Mulamadhyamakakarika", "Mūlamadhyamakakārikā")
    # …and the reverse: catalog has the LONGER formal title, ours is the bare one.
    assert WA._titles_contained(
        "Mulamadhyamakakarika", "Root Verses on the Middle Way (Mūlamadhyamakakārikā)")


def test_titles_contained_rejects_generic_collisions():
    # shared words are generic ("buddhist", "ethics") → NOT proof of the same work,
    # so the two unrelated "Buddhist Ethics" books must not be linked.
    assert not WA._titles_contained(
        "Buddhist Ethics", "Buddhist Ethics: A Very Short Introduction")
    assert not WA._titles_contained(
        "Buddhist Ethics", "The Treasury of Knowledge: Buddhist Ethics")
    # no shared distinctive word at all → no match
    assert not WA._titles_contained(
        "Mulamadhyamakakarika", "The Way of the Bodhisattva")


def test_resolve_matches_buried_native_title_as_candidate():
    """The MMK case: a verbose stored title whose full-string similarity to the
    catalogue's bare 'Mūlamadhyamakakārikā' is below the 0.80 gate is still recovered
    via the distinctive-word containment fallback — as a CANDIDATE (single source,
    not auto-verified), and a Wikidata Q-id stays out of canonical_number."""
    r = WA.WorkAuthorityResolver(sources=[
        _StubSource("wikidata", [_rec("wikidata", "Mūlamadhyamakakārikā",
                                       authors=("Nāgārjuna",), external_id="Q207666",
                                       canonical_system="wikidata",
                                       canonical_number="Q207666")]),
    ], db=None)
    c = r.resolve("Nagarjunas Middle Way: The Mulamadhyamakakarika")
    assert c.verdict == "candidate"
    assert c.authors == ["Nāgārjuna"]
    assert c.canonical_number is None      # containment stays candidate-tier


def test_resolve_does_not_rescue_generic_title():
    """A low full-string score on a generic title is NOT rescued by containment —
    the shared words ('buddhist', 'ethics') aren't distinctive, so an unrelated
    same-named book is correctly left unmatched."""
    r = WA.WorkAuthorityResolver(sources=[
        _StubSource("wikidata", [_rec(
            "wikidata", "Buddhist Ethics: A Comprehensive Guide to the Whole Field",
            authors=("Some Scholar",), canonical_system="wikidata",
            canonical_number="Q1")]),
    ], db=None)
    assert r.resolve("Buddhist Ethics").verdict == "none"


def test_no_title_match_is_none():
    r = WA.WorkAuthorityResolver(sources=[
        _StubSource("a", [_rec("a", "Totally Different Book", authors=("X",))]),
    ], db=None)
    assert r.resolve("Bodhicaryāvatāra").verdict == "none"


def test_disagreeing_single_sources_do_not_verify():
    # Two sources, same title, DIFFERENT authors → neither author reaches agreement.
    r = WA.WorkAuthorityResolver(sources=[
        _StubSource("a", [_rec("a", "Madhyamakavatara", authors=("Candrakirti",))]),
        _StubSource("b", [_rec("b", "Madhyamakāvatāra", authors=("Someone Else",))]),
    ], db=None)
    c = r.resolve("Madhyamakāvatāra")
    assert c.verdict == "candidate"            # no cross-source agreement
    assert c.agreement == 1


# ── apply_to_work ──────────────────────────────────────────────────────────────
def test_apply_to_work_writes_canonical_and_contributors(tmp_path):
    db = init_db(tmp_path / "t.db")
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, "Bodhicaryāvatāra", "english")
    eid = db.execute("INSERT INTO edition (title) VALUES ('Bca (2012)')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
               (eid, wid))

    c = WA.WorkAuthorityConsensus(
        verdict="verified", authors=["Śāntideva"], translators=["Some Translator"],
        canonical_system="toh", canonical_number="3871",
        dates={"Śāntideva": "8th cent."})
    out = WA.apply_to_work(db, wid, c)

    assert out["canonical_set"] is True
    assert out["authors_linked"] == 1 and out["translators_linked"] == 1
    row = db.execute("SELECT canonical_system, canonical_number FROM work WHERE id=?",
                     (wid,)).fetchone()
    assert row == ("toh", "3871")
    # author on the work, translator on the work's edition
    assert {r[0] for r in db.execute(
        "SELECT role FROM work_author WHERE work_id=?", (wid,))} == {"author"}
    assert db.execute("SELECT COUNT(*) FROM edition_translator WHERE edition_id=?",
                      (eid,)).fetchone()[0] == 1
    # dates were split off the author name into person.dates
    d = db.execute(
        "SELECT dates FROM person WHERE primary_name = 'Śāntideva'").fetchone()
    assert d and d[0] == "8th cent."


def test_apply_skips_unverified_by_default(tmp_path):
    db = init_db(tmp_path / "t.db")
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    c = WA.WorkAuthorityConsensus(verdict="candidate", authors=["X"])
    out = WA.apply_to_work(db, wid, c)
    assert out["skipped"] is True
    assert db.execute("SELECT COUNT(*) FROM work_author").fetchone()[0] == 0


# ── 84000 source (local TEI, no network) ───────────────────────────────────────
_TEI = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
 <teiHeader><fileDesc>
  <titleStmt>
    <title type="mainTitle" xml:lang="en">The Sutra of Limitless Life</title>
    <author>Nagarjuna</author>
    <respStmt><resp>Translated by</resp><name>Erik Pema Kunsang</name></respStmt>
  </titleStmt>
  <publicationStmt><idno type="toh">674</idno></publicationStmt>
 </fileDesc></teiHeader>
 <text><body><p/></body></text>
</TEI>"""


def test_84000_source_reads_local_tei(tmp_path):
    from catalogue.services.work_canonical_resolver import EightyFourThousandIndex
    snap = tmp_path / "84000-tei"
    snap.mkdir()
    (snap / "toh674.xml").write_text(_TEI, encoding="utf-8")
    idx = EightyFourThousandIndex(snapshot_dir=snap)
    idx.rebuild()

    src = WA.EightyFourThousandSource(index=idx)
    recs = src.lookup("The Sutra of Limitless Life")
    assert len(recs) == 1
    r = recs[0]
    assert r.canonical_system == "toh" and r.canonical_number == "674"
    assert "Nagarjuna" in r.authors
    assert "Erik Pema Kunsang" in r.translators


def test_84000_source_noops_without_snapshot(tmp_path):
    from catalogue.services.work_canonical_resolver import EightyFourThousandIndex
    src = WA.EightyFourThousandSource(
        index=EightyFourThousandIndex(snapshot_dir=tmp_path / "absent"))
    assert src.lookup("Anything") == []


# ── registry ───────────────────────────────────────────────────────────────────
def test_registry_builds_named_sources():
    assert "84000" in WA._SOURCES and "bdrc" in WA._SOURCES
    built = WA.build_sources(["84000"])
    assert len(built) == 1 and built[0].name == "84000"


# ── The walk over existing works (resolve_all_works / resolve_work_authorship) ───
def test_walk_applies_verified_and_queues_candidate(tmp_path):
    db = init_db(tmp_path / "t.db")
    w_verified = _work_with_title(db, "Bodhicaryavatara")
    w_candidate = _work_with_title(db, "Some Lone Treatise")
    w_nomatch = _work_with_title(db, "Totally Unmatchable Thing")

    # Two sources agree on the first work → verified; one source on the second →
    # candidate; the third title matches nothing.
    resolver = _stub_resolver(
        _StubSource("a", [_rec("a", "Bodhicaryavatara", authors=("Śāntideva",)),
                          _rec("a", "Some Lone Treatise", authors=("Lone One",))]),
        _StubSource("b", [_rec("b", "Bodhicaryāvatāra", authors=("Shantideva",))]),
    )
    tally = WA.resolve_all_works(db, resolver)
    assert tally["matched"] == 1
    assert tally["candidate"] == 1
    assert tally["unmatched"] == 1

    # verified → author linked on the work
    roles = [r[0] for r in db.execute(
        "SELECT role FROM work_author WHERE work_id=?", (w_verified,))]
    assert roles == ["author"]
    # candidate → a review_queue item, NOT applied
    assert db.execute(
        "SELECT COUNT(*) FROM work_author WHERE work_id=?",
        (w_candidate,)).fetchone()[0] == 0
    q = db.execute("SELECT payload_json FROM review_queue "
                   "WHERE item_type='work_authorship'").fetchall()
    assert len(q) == 1 and f'"work_id": {w_candidate}' in q[0][0]
    # nomatch → nothing
    assert db.execute("SELECT COUNT(*) FROM work_author WHERE work_id=?",
                      (w_nomatch,)).fetchone()[0] == 0


def test_walk_skips_works_that_already_have_an_author(tmp_path):
    db = init_db(tmp_path / "t.db")
    wid = _work_with_title(db, "Bodhicaryavatara")
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Existing')").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id, role) "
               "VALUES (?, ?, 'author')", (wid, pid))
    db.commit()
    resolver = _stub_resolver(
        _StubSource("a", [_rec("a", "Bodhicaryavatara", authors=("X",))]),
        _StubSource("b", [_rec("b", "Bodhicaryavatara", authors=("X",))]))
    tally = WA.resolve_all_works(db, resolver)
    assert tally == {"matched": 0, "candidate": 0, "unmatched": 0, "already": 0}
    # untouched: still just the original author
    assert db.execute("SELECT COUNT(*) FROM work_author WHERE work_id=?",
                      (wid,)).fetchone()[0] == 1


def test_walk_does_not_requeue_pending_candidate(tmp_path):
    db = init_db(tmp_path / "t.db")
    _work_with_title(db, "Lone Treatise")
    resolver = _stub_resolver(
        _StubSource("a", [_rec("a", "Lone Treatise", authors=("Solo Author",))]))
    WA.resolve_all_works(db, resolver)
    WA.resolve_all_works(db, resolver)        # second pass
    assert db.execute("SELECT COUNT(*) FROM review_queue "
                      "WHERE item_type='work_authorship'").fetchone()[0] == 1


def test_offline_cache_only_makes_no_compute(tmp_path):
    db = init_db(tmp_path / "t.db")
    _work_with_title(db, "Bodhicaryavatara")

    calls = {"n": 0}

    class _Counting(WA.WorkAuthoritySource):
        name = "counting"
        def lookup(self, title, *, language=None, aliases=()):
            calls["n"] += 1
            return [_rec("counting", title, authors=("Someone",))]

    resolver = WA.WorkAuthorityResolver(sources=[_Counting()], db=db, offline=True)
    WA.resolve_all_works(db, resolver, offline=True)
    assert calls["n"] == 0                     # offline never calls the source
    assert db.execute("SELECT COUNT(*) FROM resolver_cache").fetchone()[0] == 0


# ── author_ids preservation: WikidataWorkSource keeps the author's Q-id ──────────
def test_wikidata_work_source_preserves_author_ids():
    """The joint resolver needs the author's Q-id + cross-links, not just the name —
    WikidataWorkSource must populate author_ids (it used to discard the Q-id)."""
    work_ent = {"labels": {"en": {"value": "Mulamadhyamakakarika"}},
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
                    "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q171195"}}}}],
                }}
    author_ent = {"labels": {"en": {"value": "Nagarjuna"}},
                  "claims": {
                      "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
                      "P2477": [{"mainsnak": {"datavalue": {"value": "P4954"}}}],  # BDRC
                  }}

    class _FakeClient:
        def search(self, text, *, language="en"):
            return [("Q1", "Mulamadhyamakakarika", "")]
        def entity(self, qid):
            return work_ent if qid == "Q1" else author_ent

    src = WA.WikidataWorkSource(client=_FakeClient())
    recs = src.lookup("Mulamadhyamakakarika")
    assert len(recs) == 1
    r = recs[0]
    assert r.authors == ("Nagarjuna",)                       # name still kept
    assert len(r.author_ids) == 1
    a = r.author_ids[0]
    assert a["name"] == "Nagarjuna"
    assert a["external_id"] == "wikidata:Q171195"            # Q-id preserved
    assert a["extra_ids"]["bdrc"] == "bdr:P4954"             # cross-link harvested


def test_consensus_unions_author_ids():
    """WorkAuthorityConsensus.author_ids unions identities across matched records,
    deduped by external_id — what resolve_person_via_works reads."""
    rec = WA.WorkAuthorityRecord(
        source="wikidata", title="MMK", score=1.0, authors=("Nagarjuna",),
        author_ids=({"name": "Nagarjuna", "external_id": "wikidata:Q171195",
                     "extra_ids": {}},))
    c = WA._consensus([rec])
    assert [a["external_id"] for a in c.author_ids] == ["wikidata:Q171195"]
