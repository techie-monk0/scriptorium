"""Step-4 book analysis — CONTAINER MODEL (a book is 1+ works).

Pins the conservative work-detection wired into the live pipeline:
  - peek a located section: verse-gate root/commentary + opening attribution.
  - a WORK ANCHOR needs a genuinely distinct work title AND a title-page onset
    (verse/attribution); page labels, numbered/roman chapters, running headers
    do NOT anchor (the real corpus' over-segmentation source).
  - ≥1 anchor → those distinct works; 0 anchors → the whole book is ONE work
    (process.py supplies the book-level author/translator); many reproduced
    sections but no usable titles → `collection_unsegmented` (flagged, 1 work).
  - root vs commentary is deterministic (the LLM peek leaned 'commentary').

Hermetic: section fixtures are built in-test; the LLM is a fake transport.
"""
from __future__ import annotations

import json
import zipfile

import pytest

from catalogue.services.book_analysis import (
    analyze_book_sections, book_analysis_from_dict, peek_section,
    _detect_works, _is_distinct_work_title,
)
from catalogue.services.classify import Rung
from catalogue.db_store import init_db
from catalogue.services.llm import LLMClient
from catalogue.services.locator import Section, opens_with_verse
from catalogue.services.process import (
    ProcessConfig, process_holding, load_section_analysis,
)


# ── helpers ──────────────────────────────────────────────────────────────
def _ladder_returning(content: str):
    def _t(url, body, timeout):
        return {"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    return [Rung("fake", LLMClient(model="fake", transport=_t))]


def _verse_text(extra: str = "") -> str:
    return ("Homage to the Three Jewels.\n1 first stanza line here\n"
            "2 second stanza line\n3 third stanza line\n4 fourth stanza line\n"
            "5 fifth stanza\n6 sixth stanza\n" + extra)


def _sec(title, *, text=None, verse=0.7, source="pdf-textlayer",
         locator="", attribution=None, level=0):
    return Section(title=title, text=text if text is not None else _verse_text(),
                   verse=verse, source=source, locator=locator,
                   attribution=attribution, level=level)


def _verdict(section, kind, author=None):
    from catalogue.services.book_analysis import PeekVerdict
    return PeekVerdict(section.title, kind, author, section.verse, "test")


# ── title-page onset signal ─────────────────────────────────────────────────
def test_opens_with_verse_homage_first_is_true():
    assert opens_with_verse("Homage to the guru.\n1 first\n2 second") is True


def test_opens_with_verse_prose_then_quote_is_false():
    prose = "\n".join("Ordinary biographical prose sentence here." for _ in range(20))
    assert opens_with_verse(prose + "\n1 a quoted verse line\n2 another line") is False


def test_opens_with_verse_ignores_running_header_number():
    assert opens_with_verse("123 LIFE OF TILOPA\nA prose paragraph follows.\nMore.") is False


# ── root vs commentary determinism ──────────────────────────────────────────
def test_peek_stays_root_when_llm_says_commentary():
    s = _sec("Precious Garland", verse=0.8)
    v = peek_section(s, ladder=_ladder_returning(
        '{"kind":"commentary","author":"Nāgārjuna","confidence":0.9}'))
    assert v.kind == "root"
    assert v.author == "Nāgārjuna"


def test_peek_commentary_when_keyword_in_title():
    s = _sec("A Commentary on the Precious Garland", verse=0.8)
    v = peek_section(s, ladder=_ladder_returning(
        '{"kind":"root","author":null,"confidence":0.9}'))
    assert v.kind == "commentary"


# ── distinct-work-title test (the de-segmentation lever) ────────────────────
@pytest.mark.parametrize("title, expect", [
    ("The Wheel-Weapon Mind Training", True),
    ("Precious Garland of Advice for a King", True),
    ("Part Two: Precious Garland of Advice for a King", True),
    ("page0037", False),
    ("p. 41", False),
    ("1 – Devatāsaṃyutta: Connected Discourses", False),
    ("I. A Reed", False),
    ("III. The Second Subchapter", False),
    ("Chapter Two", False),
    ("Division I – The Root Fifty", False),
    ("Appendix", False),
    ("Introduction", False),
    ("CONTENTS", False),
    ("str_20160405_0002_2R", False),               # scan id (4+ digit run)
    ("The First Fifty\x00\x00\x00\x00", False),     # OCR null bytes
    ("Technical Note 6", False),                   # editorial apparatus
    ("ONE - Action Tantra", False),                # spelled-out ordinal chapter
    ("TWO. Performance Tantra", False),
    ("Appendices", False),                         # append* (plural)
    ("Homage", False),                             # exegetical sub-heading
    ("Summary", False),
    ("Meaning of the Words", False),
    ("The Title of the Chapter", False),
    ("One Hundred Verses on Wisdom", True),        # ordinal word but a real title
])
def test_is_distinct_work_title(title, expect):
    assert _is_distinct_work_title(title) is expect


def test_prose_chapter_quoting_verse_does_not_anchor():
    # h444/h261 shape: distinct-looking title, opens by quoting a stanza, but only
    # moderate verse (no homage) and no attribution → NOT a work anchor.
    from catalogue.services.book_analysis import _is_anchor
    quote_open = "1 a quoted stanza line\n2 another\n3 more\n4 yet more\n" + \
        "\n".join("Then the text discusses this at prose length here." for _ in range(20))
    s = _sec("What Is Emptiness?", text=quote_open, verse=0.68)
    assert _is_anchor(s, _verdict(s, "root"), enable_verse_gate=True) is False


def test_sustained_verse_or_attribution_still_anchors():
    from catalogue.services.book_analysis import _is_anchor
    s_hi = _sec("The Wheel-Weapon Mind Training", verse=1.0)      # sustained verse
    assert _is_anchor(s_hi, _verdict(s_hi, "root")) is True
    s_attr = _sec("The Peacock Mind Training", verse=0.72,
                  text="Attributed to Dharmarakṣita.\n1 first stanza\n2 second stanza")
    assert _is_anchor(s_attr, _verdict(s_attr, "root", "Dharmarakṣita")) is True


def test_detect_works_dedupes_repeated_titles():
    # book 22 shape: six identical "Appendices"-style anchors → one work, not six.
    # (use a non-structural repeated title so the title filter doesn't pre-drop it)
    pairs = [(s := _sec("Songs of Realization", attribution="Milarepa"),
              _verdict(s, "root", "Milarepa")) for _ in range(6)]
    out = _detect_works(pairs, ladder=None)
    assert len(out) == 1
    assert out[0].title == "Songs of Realization"


def test_too_many_works_flagged_as_collection():
    # >_MAX_WORKS distinct-looking anchors → mis-segmented bookmarks → flagged
    secs = [_sec(f"The Distinct Work Number {w}", attribution="Someone")
            for w in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta",
                      "Eta", "Theta", "Iota", "Kappa")]
    a = analyze_book_sections(secs, ladder=None, enable_verse_gate=True)
    assert a.structure == "collection_unsegmented"
    assert a.contained_texts == []


# ── work detection ──────────────────────────────────────────────────────────
def test_two_distinct_titled_works_split():
    # book 39 shape: two distinct titled reproduced texts → two works.
    pairs = [
        (s1 := _sec("The Wheel-Weapon Mind Training", attribution="Dharmarakṣita"),
         _verdict(s1, "root", "Dharmarakṣita")),
        (s2 := _sec("Peacock in the Poison Grove", attribution="Dharmarakṣita"),
         _verdict(s2, "root", "Dharmarakṣita")),
    ]
    out = _detect_works(pairs, ladder=None)
    assert [c.title for c in out] == [
        "The Wheel-Weapon Mind Training", "Peacock in the Poison Grove"]
    assert all(c.authors == ["Dharmarakṣita"] for c in out)


def test_numbered_chapters_do_not_anchor():
    # book 128 shape: numbered/roman subchapters of ONE work → no anchors → []
    pairs = [
        (s := _sec(t), _verdict(s, "root", None))
        for t in ("1 – Devatāsaṃyutta", "I. A Reed", "III. A Sword",
                  "I. The First Subchapter", "9 – Vanasaṃyutta")
    ]
    assert _detect_works(pairs, ladder=None) == []


def test_page_label_bookmarks_do_not_anchor():
    # book 45 shape: page-label bookmarks for a real anthology → no anchors
    pairs = [
        (s := _sec(t, verse=0.8), _verdict(s, "root", None))
        for t in ("page0037", "page0038", "page0041", "page0087", "page0130")
    ]
    assert _detect_works(pairs, ladder=None) == []


def test_single_anchor_kept_with_its_author():
    s = _sec("The Wheel-Weapon Mind Training", attribution="Dharmarakṣita")
    out = _detect_works([(s, _verdict(s, "root", "Dharmarakṣita"))], ladder=None)
    assert len(out) == 1
    assert out[0].title == "The Wheel-Weapon Mind Training"
    assert out[0].authors == ["Dharmarakṣita"]


def test_toc_author_used_when_no_first_page_attribution():
    # Section has NO first-page attribution; the printed-Contents author (Section.
    # toc_author) is the fallback.
    s = _sec("Eight Verses on Mind Training", text="prose", verse=0.0)
    s.toc_author = "Langri Thangpa"
    out = _detect_works([(s, _verdict(s, "root", None))], ladder=None)
    assert out and out[0].authors == ["Langri Thangpa"]


def test_first_page_attribution_wins_over_toc_author():
    # The located section's own first-page attribution takes precedence over the
    # Contents author (the first page is the authority).
    s = _sec("The Peacock's Neutralizing of Poison", text="prose", verse=0.0)
    s.toc_author = "Someone Else"
    out = _detect_works([(s, _verdict(s, "root", "Dharmarakṣita"))], ladder=None)
    assert out and out[0].authors == ["Dharmarakṣita"]


def test_no_llm_backfill_even_with_ladder():
    # No first-page attribution, no toc_author, ladder provided → author stays empty.
    # The per-work LLM author backfill was removed; ladder must NOT be consulted here.
    s = _sec("Some Reproduced Text", text="prose", verse=0.0)
    out = _detect_works([(s, _verdict(s, "root", None))],
                        ladder=_ladder_returning('{"author":"Hallucinated"}'))
    assert out and out[0].authors == []


def _analysis_with(texts):
    from types import SimpleNamespace
    from catalogue.services.book_analysis import ContainedText
    cts = [ContainedText(t, a, [], "root", 0.0, "", []) for t, a in texts]
    return SimpleNamespace(contained_texts=cts)


def test_split_title_by_author():
    from catalogue.services.book_analysis import split_title_author as s
    assert s("Ganachakra Offering for Chittamani Tara by Trijang Rinpoche") == \
        ("Ganachakra Offering for Chittamani Tara", "Trijang Rinpoche")
    # 'by' not torn out of an ordinary title (author not a transliteration name)
    assert s("Liberation by Hearing in the Bardo") == \
        ("Liberation by Hearing in the Bardo", None)


def test_split_title_with_possessive():
    from catalogue.services.book_analysis import split_title_author as s
    assert s("Sumpa Lotsawa's Ear-Whispered Mind Training") == \
        ("Ear-Whispered Mind Training", "Sumpa Lotsawa")
    assert s("Yangonpa's Instruction on Training the Mind") == \
        ("Instruction on Training the Mind", "Yangonpa")
    # leading epithet stripped from the author
    assert s("Bodhisattva Samantabhadra's Mind Training") == ("Mind Training", "Samantabhadra")
    # diacritic-less names the script test misses, but which are NOT English words,
    # still split (book 45: "Atisa's …", "Kusulu's …").
    assert s("Atisa's Seven-Point Mind Training") == ("Seven-Point Mind Training", "Atisa")
    assert s("Kusulu's Accumulation Mind Training") == ("Accumulation Mind Training", "Kusulu")
    # 'Peacock'/'Poison'/'Story' ARE English words → no split
    assert s("The Peacock's Neutralizing of Poison") == \
        ("The Peacock's Neutralizing of Poison", None)
    # a mid-title possessive must not split (anchored at the start only)
    assert s("The Story of Atisa's Voyage to Sumatra") == \
        ("The Story of Atisa's Voyage to Sumatra", None)


def test_split_title_author_flags_off():
    from catalogue.services.book_analysis import split_title_author as s
    assert s("X by Trijang Rinpoche", by=False) == ("X by Trijang Rinpoche", None)
    assert s("Sumpa Lotsawa's Text", possessive=False) == ("Sumpa Lotsawa's Text", None)


def test_book_author_not_inherited_when_a_sibling_names_its_own():
    # Mixed collection: X has its own author, Y has none → Y must NOT get the book's
    # author (it's anonymous), since the book author is a compiler, not Y's author.
    from types import SimpleNamespace
    from catalogue.services.process import _build_works
    analysis = _analysis_with([("X", ["Author A"]), ("Y", [])])
    contrib = SimpleNamespace(authors=["Book Compiler"], translators=["Tr"])
    works = {w["title"]: w for w in _build_works(analysis, contrib, "Book")}
    assert works["X"]["authors"] == ["Author A"]
    assert works["Y"]["authors"] == [] and works["Y"]["author_inherited"] is False
    assert works["Y"]["translators"] == ["Tr"]      # translator still inherits


def test_book_author_inherited_when_no_work_names_its_own():
    # Uniform-authorship collection: no contained work names an author → all inherit
    # the book-level author.
    from types import SimpleNamespace
    from catalogue.services.process import _build_works
    analysis = _analysis_with([("X", []), ("Y", [])])
    contrib = SimpleNamespace(authors=["Single Author"], translators=[])
    works = {w["title"]: w for w in _build_works(analysis, contrib, "Book")}
    assert works["X"]["authors"] == ["Single Author"]
    assert works["X"]["author_inherited"] is True


def test_quoted_verse_without_title_page_does_not_anchor():
    prose = "\n".join("Tilopa was born in a Bengali village and studied." for _ in range(20))
    quoting = prose + "\nThen he sang:\n1 first quoted\n2 second\n3 third\n4 fourth"
    s = _sec("A Distinct Looking Title", text=quoting, verse=0.7)
    assert _detect_works([(s, _verdict(s, "root", None))], ladder=None,
                         enable_verse_gate=True) == []


# ── structure classification ────────────────────────────────────────────────
def test_empty_is_single_work():
    a = analyze_book_sections([], ladder=None)
    assert a.structure == "single_work" and a.contained_texts == [] and a.n_sections == 0


def test_collection_unsegmented_when_many_reproduced_but_no_titles():
    # ≥5 verse sections, all page-label titles → flagged, no works emitted here
    secs = [_sec(f"page{37+i:04d}", verse=0.8) for i in range(6)]
    a = analyze_book_sections(secs, ladder=None, enable_verse_gate=True)
    assert a.structure == "collection_unsegmented"
    assert a.contained_texts == []
    assert a.n_reproduced == 6


def test_multi_work_when_distinct_titles():
    secs = [
        _sec("The Wheel-Weapon Mind Training", attribution="Dharmarakṣita"),
        _sec("Peacock in the Poison Grove", attribution="Dharmarakṣita"),
    ]
    a = analyze_book_sections(secs, ladder=None)
    assert a.structure == "multi_work"
    assert len(a.contained_texts) == 2


# ── verse gate OFF (the default) — labeled-segmentation behaviour ────────────
def test_gate_off_prose_section_anchors_but_gate_on_rejects():
    # zero verse, no attribution, distinct title: a work without the gate (default),
    # NOT a work with it. This is the inversion the gate flag controls.
    from catalogue.services.book_analysis import _is_anchor
    s = _sec("A Song by Tantipa the Weaver", text="ordinary prose here", verse=0.0)
    assert _is_anchor(s, _verdict(s, "root")) is True                       # gate off
    assert _is_anchor(s, _verdict(s, "root"), enable_verse_gate=True) is False


def test_gate_off_segments_prose_anthology():
    # distinct-titled prose sections, no verse/attribution → gate OFF splits them;
    # gate ON sees them as "other" (below the verse gate) → one whole-book work.
    secs = [_sec("A Song by Tantipa", text="prose", verse=0.0),
            _sec("A Song by Saraha", text="prose", verse=0.0)]
    assert analyze_book_sections(secs, ladder=None).structure == "multi_work"
    on = analyze_book_sections(secs, ladder=None, enable_verse_gate=True)
    assert on.structure == "single_work" and on.contained_texts == []


def test_gate_off_does_not_collapse_large_anthology():
    # >_MAX_WORKS distinct prose works: gate OFF keeps them all (multi_work) instead
    # of the gate-ON collapse-to-collection safety valve.
    secs = [_sec(f"A Song by Siddha {w}", text="prose", verse=0.0)
            for w in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta",
                      "Eta", "Theta", "Iota", "Kappa")]
    off = analyze_book_sections(secs, ladder=None)
    assert off.structure == "multi_work" and len(off.contained_texts) == 10


def _nested_parts_book():
    # Two top-level parts (level 0), each with distinct-titled chapters beneath
    # (level 1) — the Chittamani Tara shape. Front/back at level 0 too.
    return [
        _sec("Cover Page", text="prose", verse=0.0, level=0),
        _sec("Part 1: Commentary on the Two Stages", text="prose", verse=0.0, level=0),
        _sec("The Generation Stage", level=1),
        _sec("The Completion Stage", level=1),
        _sec("Part 2: Ritual Texts", text="prose", verse=0.0, level=0),
        _sec("Self-Generation Sadhana", level=1),
        _sec("Praise to Venerable Tara", level=1),
        _sec("Ganachakra Offering", level=1),
    ]


def test_toc_hierarchy_groups_chapters_under_their_part():
    secs = _nested_parts_book()
    # OFF: every distinct-titled section (parts + chapters) is its own work.
    off = analyze_book_sections(secs, ladder=None, toc_hierarchy=False)
    assert off.structure == "multi_work" and len(off.contained_texts) > 2
    # ON: one work per top-level part; chapters fold in as members sharing the part.
    on = analyze_book_sections(secs, ladder=None, toc_hierarchy=True)
    assert on.structure == "multi_work"
    titles = [w.title for w in on.contained_texts]
    assert titles == ["Commentary on the Two Stages", "Ritual Texts"]
    members = {w.title: w.section_titles for w in on.contained_texts}
    assert "The Generation Stage" in members["Commentary on the Two Stages"]
    assert "Self-Generation Sadhana" in members["Ritual Texts"]


def test_toc_hierarchy_noop_on_flat_toc():
    # A flat anthology (all level 0, the default): hierarchy must not change the split
    # — there is no nesting to exploit (book 51's one-level commentary-outline dump).
    secs = [_sec(f"A Song by Siddha {w}", text="prose", verse=0.0)
            for w in ("Alpha", "Beta", "Gamma")]
    off = analyze_book_sections(secs, ladder=None, toc_hierarchy=False)
    on = analyze_book_sections(secs, ladder=None, toc_hierarchy=True)
    assert len(on.contained_texts) == len(off.contained_texts) == 3


def test_book_analysis_roundtrips_through_dict():
    a = analyze_book_sections(
        [_sec("The Root Text", attribution="Master Naga")], ladder=None)
    b = book_analysis_from_dict(json.loads(json.dumps(a.to_dict())))
    assert b.structure == a.structure
    assert [c.title for c in b.contained_texts] == [c.title for c in a.contained_texts]
    assert b.contained_texts[0].authors == ["Master Naga"]


# ── process_holding integration (real epub, fake LLM) ───────────────────────
def _write_epub(path):
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="i">
 <manifest>
  <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
  <item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine toc="ncx"><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""
    ncx = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>
 <navPoint id="n1"><navLabel><text>The Root Text</text></navLabel><content src="ch1.xhtml"/></navPoint>
 <navPoint id="n2"><navLabel><text>Modern Essay</text></navLabel><content src="ch2.xhtml"/></navPoint>
</navMap></ncx>"""
    ch1 = ("<html><body><h1>The Root Text</h1><p>Attributed to Master Naga</p>"
           "<p>Homage to the gurus.</p><p>1 first line</p><p>2 second line</p>"
           "<p>3 third line</p><p>4 fourth line</p><p>5 fifth line</p></body></html>")
    ch2 = ("<html><body><h1>Modern Essay</h1>"
           + "<p>This chapter analyses the history at length.</p>" * 8
           + "</body></html>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/ch1.xhtml", ch1)
        z.writestr("OEBPS/ch2.xhtml", ch2)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "v15.db")
    yield conn
    conn.close()


def _seed(db, *, file_path, file_hash="h", text_status="ocr_good"):
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'Sample Edition')")
    db.execute(
        "INSERT INTO holding (id, edition_id, form, file_path, file_hash, text_status) "
        "VALUES (1, 1, 'electronic', ?, ?, ?)", (file_path, file_hash, text_status))
    db.commit()


def test_process_holding_section_path_and_cache(db, tmp_path):
    epub = tmp_path / "b.epub"
    _write_epub(epub)
    _seed(db, file_path=str(epub))
    cfg = ProcessConfig(ladder=_ladder_returning('{"kind":"other","confidence":0.9}'),
                        analyze_book=True, enable_verse_gate=True)
    rep = process_holding(db, 1, cfg)

    # the section path located the verse root text (its own anchor) and dropped
    # the prose essay; container is single_work with that one work.
    assert rep.book_structure == "single_work"
    assert rep.n_works == 1
    rows = db.execute(
        "SELECT payload_json FROM review_queue WHERE item_type='book_toc_pattern'"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["source"] == "epub-nav"
    assert payload["n_sections"] == 2
    works = payload["works"]
    assert [w["title"] for w in works] == ["The Root Text"]       # essay dropped
    assert works[0]["authors"] == ["Master Naga"]                 # own attribution
    assert works[0]["kind"] == "root"
    assert works[0]["whole_book"] is False
    assert payload["contained_texts"] == works                    # legacy key alias

    cached = load_section_analysis(db, "h", cfg.section_version)
    assert cached is not None and cached.structure == "single_work"


def test_process_holding_whole_book_work_when_no_anchor(db, tmp_path):
    # a prose-only book → no anchors → ONE whole-book work titled from the edition
    epub = tmp_path / "study.epub"
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
           'version="2.0"><manifest><item id="ncx" href="toc.ncx" '
           'media-type="application/x-dtbncx+xml"/><item id="c1" href="c1.xhtml" '
           'media-type="application/xhtml+xml"/></manifest>'
           '<spine toc="ncx"><itemref idref="c1"/></spine></package>')
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
           'version="2005-1"><navMap><navPoint id="n1"><navLabel><text>Chapter One'
           '</text></navLabel><content src="c1.xhtml"/></navPoint></navMap></ncx>')
    c1 = "<html><body><h1>Chapter One</h1>" + "<p>Prose discussion sentence.</p>" * 12 + "</body></html>"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/c1.xhtml", c1)
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'Some Author - A Modern Study')")
    db.execute("INSERT INTO holding (id, edition_id, form, file_path, file_hash, text_status) "
               "VALUES (1, 1, 'electronic', ?, 'h2', 'ocr_good')", (str(epub),))
    db.commit()
    cfg = ProcessConfig(ladder=_ladder_returning('{"kind":"other","confidence":0.9}'),
                        analyze_book=True)
    rep = process_holding(db, 1, cfg)
    assert rep.book_structure == "single_work" and rep.n_works == 1
    payload = json.loads(db.execute(
        "SELECT payload_json FROM review_queue WHERE item_type='book_toc_pattern'"
    ).fetchone()[0])
    w = payload["works"][0]
    assert w["whole_book"] is True
    assert w["title"] == "A Modern Study"          # clean title (author stripped off)


def test_no_toc_emits_whole_book_proposal_not_failure(db):
    # No extractable TOC (outline returns None, text-layer off, vision stubbed) but
    # the book has text → it's the degenerate single-work container: emit a
    # whole-book proposal + an advisory extraction_note, NOT a real failure.
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'Some Author - A Poetry Collection')")
    db.execute("INSERT INTO holding (id, edition_id, form, file_path, file_hash, text_status) "
               "VALUES (1, 1, 'electronic', NULL, 'h', 'ocr_good')")
    db.execute("INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
               "VALUES ('h', 1, 'A Poetry Collection, by Some Author. Verses follow.')")
    db.commit()
    cfg = ProcessConfig(outline_extractor=lambda p: None, analyze_book=True,
                        ladder=_ladder_returning('{"kind":"other","confidence":0.9}'))
    rep = process_holding(db, 1, cfg)

    rows = {}
    for it, pj in db.execute("SELECT item_type, payload_json FROM review_queue").fetchall():
        rows.setdefault(it, []).append(json.loads(pj))
    assert "low_confidence_extraction" not in rows          # not a failure
    assert "extraction_note" in rows
    assert rows["extraction_note"][0]["reason"] == "no_toc_whole_book"
    assert "book_toc_pattern" in rows                        # got a real proposal
    btp = rows["book_toc_pattern"][0]
    assert btp["no_toc"] is True
    assert len(btp["works"]) == 1 and btp["works"][0]["whole_book"] is True
    assert btp["works"][0]["title"] == "A Poetry Collection"
    assert rep.n_works == 1


def test_process_holding_second_run_hits_section_cache(db, tmp_path):
    epub = tmp_path / "b.epub"
    _write_epub(epub)
    _seed(db, file_path=str(epub))
    cfg = ProcessConfig(ladder=_ladder_returning('{"kind":"other","confidence":0.9}'),
                        analyze_book=True, enable_verse_gate=True)
    process_holding(db, 1, cfg)
    epub.unlink()
    rep2 = process_holding(db, 1, cfg)
    assert rep2.book_structure == "single_work"
    rows = db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE item_type='book_toc_pattern'"
    ).fetchone()
    assert rows[0] == 2          # one proposal per run, both from the same analysis


def test_staging_round_trip_self_bootstraps_section_cache(db, tmp_path):
    """The batch path (run_resolve → run_load): a worker resolves through a
    read-only StagingConn whose SELECTs hit a live DB that does NOT yet have
    section_cache, then the journal is replayed by load_artifacts."""
    from catalogue.services.staging import StagingConn, write_artifact, load_artifacts

    db.execute("DROP TABLE IF EXISTS section_cache")
    db.commit()

    epub = tmp_path / "b.epub"
    _write_epub(epub)
    _seed(db, file_path=str(epub))
    cfg = ProcessConfig(ladder=_ladder_returning('{"kind":"other","confidence":0.9}'),
                        analyze_book=True, enable_verse_gate=True)

    assert load_section_analysis(db, "h", cfg.section_version) is None

    sc = StagingConn(db)
    rep = process_holding(sc, 1, cfg)
    assert rep.book_structure == "single_work"
    sqls = [w["sql"] for w in sc.writes]
    assert any("CREATE TABLE IF NOT EXISTS section_cache" in s for s in sqls)

    write_artifact(str(tmp_path / "staging"), 1, sc.writes)
    result = load_artifacts(db, str(tmp_path / "staging"))
    assert result["errors"] == 0 and result["loaded"] == 1

    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE item_type='book_toc_pattern'"
    ).fetchone()[0] == 1
    cached = load_section_analysis(db, "h", cfg.section_version)
    assert cached is not None and cached.structure == "single_work"
