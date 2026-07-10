"""Locator regression tests — the three section-location cases (§4.7):

  1. EPUB              → nav/NCX hyperlinks
  2. PDF with links    → bookmark outline
  3. PDF without links → printed-folio + heading text (incl. folio-drop fallback)

Plus the deterministic signals the peek depends on: verse_score must fire on real
verse (homage + consecutive stanzas) but NOT on prose or short-line source-lists
(the book-60 over-collection regression), and find_attribution must read the author.
Fixtures are built in-test so the suite never touches the kDrive corpus.
"""
from __future__ import annotations

import zipfile

import pytest

from catalogue.services.locator import (
    extract_sections, verse_score, find_attribution, _running_header_folio,
    _nav_entries, _ncx_entries, Section,
)
from catalogue.services.book_analysis import peek_section
from catalogue.services.toc import TOCEntry


# ── deterministic signals ───────────────────────────────────────────────────
def test_verse_score_fires_on_real_verse():
    verse = ("Homage to the Three Jewels.\n1 first stanza line\n2 second stanza\n"
             "3 third stanza\n4 fourth stanza\n5 fifth stanza\n6 sixth stanza")
    assert verse_score(verse) >= 0.5


def test_verse_score_rejects_prose():
    prose = "\n".join(
        "This is an ordinary line of scholarly prose discussing history at length."
        for _ in range(10))
    assert verse_score(prose) < 0.5


def test_verse_score_rejects_short_line_source_list():
    # book-60 regression: a hagiographic source list is short-line but NOT verse
    # (no homage, no consecutive 1,2,3,4 stanza run).
    src = "\n".join(["rGyal thang pa", "U rgyan pa", "Mon rtse pa",
                     "gTsang smyon He ru ka", "dBang phyug rgyal mtshan",
                     "lHa btsun", "Kun dga' rin chen", "rDo rje mdzes 'od"])
    assert verse_score(src) < 0.5


def test_find_attribution():
    assert find_attribution("Peacock in the Poison Grove Attributed to Dharmarakṣita") \
        == "Dharmarakṣita"
    assert find_attribution("just a normal sentence with no attribution") is None


def test_find_attribution_broadened_verbs():
    assert find_attribution("ascribed to Saraha") == "Saraha"
    assert find_attribution("This sādhana was revealed by Sera Khandro") == "Sera Khandro"


def test_find_attribution_ocr_garbled_attributed():
    # book 45 #3: "Attributed to Atiśa" OCR'd as "Ailrib'uted to Atifa" on its own line.
    assert find_attribution("·3:.The Story\n.Ailrib'uted to Atifa\nI") == "Atifa"


def test_find_attribution_rejects_lowercase_fragment():
    # regression: a verb followed by a lowercase fragment is NOT an author. The name
    # must start with a capital (book 51/69/211: "composed by the great and …").
    assert find_attribution("…so it is composed by the great and noble intention…") is None
    assert find_attribution("the practice was spoken by the master to a number of") is None
    # a real capitalized name after the same verb still matches
    assert find_attribution("composed by the master Gyaltsab Je") == "Gyaltsab Je"


def test_find_attribution_ocr_does_not_match_prose_to():
    # a sentence containing "… to <lowercase>" or "contributed to the field" is not an
    # attribution (the name must be a capitalised standalone trailing token).
    assert find_attribution("He contributed to the field of medicine and so on") is None
    assert find_attribution("Homage to the Three Jewels") is None


# ── 1. EPUB (nav/NCX) ────────────────────────────────────────────────────────
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


def test_epub_nav_locator(tmp_path):
    p = tmp_path / "b.epub"
    _write_epub(p)
    secs = extract_sections(p)
    assert secs is not None and [s.title for s in secs] == ["The Root Text", "Modern Essay"]
    assert all(s.source == "epub-nav" for s in secs)
    root, essay = secs
    assert "first line" in root.text and root.verse >= 0.5
    assert root.attribution == "Master Naga"
    assert essay.verse < 0.5
    # peek (deterministic, no ladder): reproduced text vs prose. The prose→"other"
    # verdict is the verse-gate (auto-detection) behaviour → enable_verse_gate=True;
    # with the gate off (default) a non-front/back section is a work candidate.
    assert peek_section(root).kind == "root"
    assert peek_section(root).author == "Master Naga"
    assert peek_section(essay, enable_verse_gate=True).kind == "other"


# ── TOC nesting depth (the toc_hierarchy signal) ─────────────────────────────
def test_ncx_entries_capture_navpoint_depth():
    ncx = """<ncx><docTitle><text>Ignored Book Title</text></docTitle><navMap>
     <navPoint><navLabel><text>Front</text></navLabel><content src="f.xhtml"/></navPoint>
     <navPoint><navLabel><text>Part 1</text></navLabel><content src="p1.xhtml"/>
       <navPoint><navLabel><text>Chapter A</text></navLabel><content src="a.xhtml"/></navPoint>
       <navPoint><navLabel><text>Chapter B</text></navLabel><content src="b.xhtml"/></navPoint>
     </navPoint>
     <navPoint><navLabel><text>Part 2</text></navLabel><content src="p2.xhtml"/></navPoint>
    </navMap></ncx>"""
    got = {t: lvl for t, _h, lvl in _ncx_entries(ncx)}
    assert got == {"Front": 0, "Part 1": 0, "Chapter A": 1, "Chapter B": 1, "Part 2": 0}


def test_nav_entries_capture_ol_depth():
    nav = """<nav epub:type="toc"><ol>
      <li><a href="p1.xhtml">Part 1</a>
        <ol>
          <li><a href="a.xhtml">Chapter A</a></li>
          <li><a href="b.xhtml">Chapter B</a></li>
        </ol>
      </li>
      <li><a href="p2.xhtml">Part 2</a></li>
    </ol></nav>"""
    assert _nav_entries(nav) == [
        ("Part 1", "p1.xhtml", 0), ("Chapter A", "a.xhtml", 1),
        ("Chapter B", "b.xhtml", 1), ("Part 2", "p2.xhtml", 0)]


# ── 2 & 3. PDF ───────────────────────────────────────────────────────────────
def _fitz():
    return pytest.importorskip("fitz")


def _page(doc, header, body):
    pg = doc.new_page()
    if header:
        pg.insert_text((72, 40), header, fontsize=12)
    pg.insert_textbox(__import__("fitz").Rect(72, 90, 520, 740), body, fontsize=11)
    return pg


VERSE_BODY = ("FIRST CHAPTER\nHomage to the gurus.\n1 first stanza\n2 second stanza\n"
              "3 third stanza\n4 fourth stanza\n5 fifth stanza")
PROSE_BODY = ("SECOND CHAPTER\nAttributed to Master Foo\n"
              + "This is ordinary prose discussion. " * 12)


def test_pdf_bookmark_locator(tmp_path):
    fitz = _fitz()
    doc = fitz.open()
    _page(doc, "1 MY BOOK", VERSE_BODY)          # p1  The Root Text
    _page(doc, "2 MY BOOK", "more verse context here")
    _page(doc, "3 MY BOOK", PROSE_BODY)          # p3  Modern Essay
    _page(doc, "4 MY BOOK", "more prose context here")
    doc.set_toc([[1, "The Root Text", 1], [1, "Modern Essay", 3]])
    p = tmp_path / "linked.pdf"
    doc.save(p)
    secs = extract_sections(p)
    assert secs is not None and [s.title for s in secs] == ["The Root Text", "Modern Essay"]
    assert all(s.source == "pdf-bookmark" for s in secs)
    assert secs[0].verse >= 0.5            # verse pages
    assert secs[1].verse < 0.5             # prose pages
    assert peek_section(secs[0]).kind == "root"


def test_pdf_degenerate_outline_is_skipped(tmp_path):
    # book 45 shape: a page-label-only bookmark outline carries no work signal.
    # extract_sections must NOT return those page labels as sections; with no real
    # toc_entries for the printed-Contents locator it returns None (not page works).
    fitz = _fitz()
    doc = fitz.open()
    for i in range(1, 9):
        _page(doc, f"{i} MY BOOK", PROSE_BODY)
    doc.set_toc([[1, f"page{i:04d}", i] for i in range(1, 9)])
    p = tmp_path / "pagelabels.pdf"
    doc.save(p)
    secs = extract_sections(p)
    # not the junk bookmark sections — either None or, if folios resolved, not page labels
    assert secs is None or all(not s.title.lower().startswith("page0") for s in secs)


def test_pdf_textlayer_locator_with_folio_drop(tmp_path):
    fitz = _fitz()
    doc = fitz.open()
    _page(doc, "TITLE PAGE", "My Book")                       # phys0 front matter
    _page(doc, "1 MY BOOK", VERSE_BODY)                       # phys1 folio 1
    _page(doc, "2 MY BOOK", "continued verse context")        # phys2 folio 2
    _page(doc, "MY BOOK", PROSE_BODY)                         # phys3 folio DROPPED
    _page(doc, "4 MY BOOK", "more prose context")             # phys4 folio 4
    p = tmp_path / "scan.pdf"
    doc.save(p)
    # no outline → extract_sections needs the parsed TOC (title + printed page)
    assert extract_sections(p) is None
    toc = [TOCEntry("First Chapter", 1), TOCEntry("Second Chapter", 3)]
    secs = extract_sections(p, toc_entries=toc)
    assert secs is not None and [s.title for s in secs] == ["First Chapter", "Second Chapter"]
    assert all(s.source == "pdf-textlayer" for s in secs)
    # "Second Chapter" printed p3 with a dropped folio must still be located via
    # the before/after neighbour fallback (folio 2 → +1), starting at phys 3.
    assert secs[1].locator.startswith("pages 4")
    assert secs[0].verse >= 0.5 and secs[1].verse < 0.5
    assert secs[1].attribution == "Master Foo"


def test_running_header_folio_rejects_verse_numbers():
    fitz = _fitz()
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 40), "307 If you do not make contributions of the wealth", fontsize=12)
    assert _running_header_folio(pg) is None
    pg2 = doc.new_page()
    pg2.insert_text((72, 40), "100 PRECIOUS GARLAND OF ADVICE", fontsize=12)
    assert _running_header_folio(pg2) == 100
