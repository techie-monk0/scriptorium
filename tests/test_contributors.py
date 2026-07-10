"""Step-4 book-contributor resolution (§9): filename hint parsing, title-page
reconciliation via a (mocked) local LLM, and the resolver cache contract.

Hermetic — the LLM ladder and embedded metadata are faked; no network, no Ollama.
"""
import json
import sqlite3
import zipfile

import pytest

from catalogue.services.classify import Rung
from catalogue.services.contributors import (
    ContributorResolver, ContributorResult, parse_title_contributors,
)
from catalogue.services.extract import book_metadata, title_page_text
from catalogue.services.work_canonical_resolver import LiveResolver, ResolverStub


# ── fake LLM ladder ─────────────────────────────────────────────────────────
class _FakeClient:
    def __init__(self, content, spy=None):
        self._content = content
        self._spy = spy

    def chat(self, messages, max_tokens=400):
        if self._spy is not None:
            self._spy.append(messages)
        return {"content": self._content}


def _ladder(content, spy=None):
    return [Rung("fake", _FakeClient(content, spy))]


# ── filename / title-string parsing ─────────────────────────────────────────
@pytest.mark.parametrize("title, expect_names, expect_clean", [
    ("G Sopa - Peacock in the Poison Grove",
     ["G Sopa"], "Peacock in the Poison Grove"),
    ("Nagarjuna - Precious Garland",
     ["Nagarjuna"], "Precious Garland"),
    ("How to Meditate on the Stages of the Path -- Kathleen McDonald -- "
     "PS, 2024 -- Wisdom Publications -- 9781614298939 -- abcdef0123456789 -- Anna’s Archive",
     ["Kathleen McDonald"], "How to Meditate on the Stages of the Path"),
    ("Practice of the Six Yogas of Naropa — Glenn H. Mullin",
     ["Glenn H. Mullin"], "Practice of the Six Yogas of Naropa"),
    ("The-Torch-for-the-Definitive-Meaning", [], "The-Torch-for-the-Definitive-Meaning"),
    ("Dalai Lamas on Tantra", [], "Dalai Lamas on Tantra"),
])
def test_parse_title_contributors(title, expect_names, expect_clean):
    names, clean = parse_title_contributors(title)
    assert names == expect_names
    assert clean == expect_clean


def test_parse_drops_publisher_and_isbn_noise():
    # the author column has a multi-name field; org/isbn fields must not leak in
    names, _ = parse_title_contributors(
        "Manual of Ritual Fire Offerings -- Sharpa Tulku; Michael Perrott; "
        "Library of Tibetan Works & -- New Ed, 1998 -- Library of -- 9788185102665")
    assert "Sharpa Tulku" in names and "Michael Perrott" in names
    assert not any("Library" in n for n in names)


# ── title-page reconciliation ───────────────────────────────────────────────
def test_verify_corrects_and_splits_roles():
    spy = []
    ladder = _ladder(json.dumps({
        "authors": ["Nāgārjuna"], "translators": ["Jeffrey Hopkins"],
        "confidence": 0.92, "evidence": "translated by Jeffrey Hopkins"}), spy)
    r = ContributorResolver().resolve(
        edition_title="Nagarjuna - Precious Garland",
        front_matter="THE PRECIOUS GARLAND\nby Nāgārjuna\ntranslated by Jeffrey Hopkins\n",
        ladder=ladder)
    assert r.verified is True and r.source == "title-page"
    assert r.authors == ["Nāgārjuna"]
    assert r.translators == ["Jeffrey Hopkins"]
    assert r.confidence == pytest.approx(0.92)
    # the filename candidate was passed to the model as a hint
    assert "Nagarjuna" in spy[0][1]["content"]


def test_no_ladder_falls_back_to_hints_unverified():
    r = ContributorResolver().resolve(
        edition_title="G Sopa - Peacock in the Poison Grove",
        front_matter="(title page text present)", ladder=None)
    assert r.verified is False
    assert r.source == "title-string"
    assert r.authors == ["G Sopa"]


def test_no_front_matter_does_not_call_llm():
    spy = []
    ladder = _ladder(json.dumps({"authors": ["X"]}), spy)
    r = ContributorResolver().resolve(
        edition_title="A - B", front_matter="", ladder=ladder)
    assert spy == []                      # never invoked the model
    assert r.verified is False


def test_metadata_hint_preferred_over_filename_when_unverified():
    r = ContributorResolver().resolve(
        edition_title="Some Cryptic Filename",
        front_matter="", meta={"authors": ["Lama Zopa Rinpoche"], "translators": []},
        ladder=None)
    assert r.authors == ["Lama Zopa Rinpoche"]
    assert r.source == "metadata"


def test_empty_llm_result_falls_back_to_hints():
    ladder = _ladder(json.dumps({"authors": [], "translators": [], "confidence": 0.1}))
    r = ContributorResolver().resolve(
        edition_title="Nagarjuna - Precious Garland",
        front_matter="opaque front matter naming no one", ladder=ladder)
    assert r.verified is False              # LLM found nobody → keep the hint
    assert r.authors == ["Nagarjuna"]


# ── resolver cache contract ─────────────────────────────────────────────────
def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE resolver_cache (query_hash TEXT, resolver_version INTEGER, "
              "source TEXT, raw_json TEXT, parsed_json TEXT, "
              "PRIMARY KEY (query_hash, resolver_version))")
    return c


def test_stub_resolve_contributors_offline_and_cached():
    conn = _conn()
    stub = ResolverStub()
    r1 = stub.resolve_contributors(
        conn, cache_key="hashA", edition_title="G Sopa - Peacock in the Poison Grove",
        front_matter="ignored by stub", ladder=_ladder("should not be used"))
    assert r1.authors == ["G Sopa"] and r1.verified is False
    rows = conn.execute("SELECT COUNT(*) FROM resolver_cache").fetchone()[0]
    assert rows == 1
    # second call hits the cache (same row count)
    r2 = stub.resolve_contributors(
        conn, cache_key="hashA", edition_title="G Sopa - Peacock in the Poison Grove")
    assert r2.authors == ["G Sopa"]
    assert conn.execute("SELECT COUNT(*) FROM resolver_cache").fetchone()[0] == 1


def test_live_resolve_contributors_verifies_then_caches():
    conn = _conn()
    spy = []
    live = LiveResolver()
    ladder = _ladder(json.dumps({
        "authors": ["Nāgārjuna"], "translators": ["Jeffrey Hopkins"],
        "confidence": 0.9, "evidence": "trans. Hopkins"}), spy)
    r1 = live.resolve_contributors(
        conn, cache_key="hashB", edition_title="Nagarjuna - Precious Garland",
        front_matter="by Nāgārjuna, translated by Jeffrey Hopkins", ladder=ladder)
    assert r1.verified and r1.translators == ["Jeffrey Hopkins"]
    assert len(spy) == 1
    # cached under live.version → second call does NOT re-call the LLM
    r2 = live.resolve_contributors(
        conn, cache_key="hashB", edition_title="Nagarjuna - Precious Garland",
        front_matter="by Nāgārjuna, translated by Jeffrey Hopkins", ladder=ladder)
    assert r2.translators == ["Jeffrey Hopkins"]
    assert len(spy) == 1                    # no second model call
    # contributor rows are keyed by CONTRIBUTOR_VERSION (independent of the
    # resolver's work/person version, so contributor changes invalidate alone)
    from catalogue.services.work_canonical_resolver import CONTRIBUTOR_VERSION
    assert conn.execute(
        "SELECT resolver_version FROM resolver_cache").fetchone()[0] == CONTRIBUTOR_VERSION


# ── embedded EPUB metadata (opf roles) ───────────────────────────────────────
def _make_epub(tmp_path, opf_body):
    p = tmp_path / "b.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container><rootfiles>'
                   '<rootfile full-path="content.opf"/></rootfiles></container>')
        z.writestr("content.opf", opf_body)
    return p


def test_epub_metadata_epub2_roles(tmp_path):
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">'
        '<dc:title>The Precious Garland</dc:title>'
        '<dc:creator opf:role="aut">Nagarjuna</dc:creator>'
        '<dc:contributor opf:role="trl">Jeffrey Hopkins</dc:contributor>'
        '</metadata></package>')
    md = book_metadata(_make_epub(tmp_path, opf))
    assert md["authors"] == ["Nagarjuna"]
    assert md["translators"] == ["Jeffrey Hopkins"]
    assert md["title"] == "The Precious Garland"
    assert md["source"] == "epub-opf"


def test_epub_metadata_epub3_refines(tmp_path):
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:creator id="c1">Glenn H. Mullin</dc:creator>'
        '<meta refines="#c1" property="role" scheme="marc:relators">trl</meta>'
        '</metadata></package>')
    md = book_metadata(_make_epub(tmp_path, opf))
    assert md["translators"] == ["Glenn H. Mullin"]
    assert md["authors"] == []


def test_book_metadata_unreadable_returns_empty(tmp_path):
    bad = tmp_path / "x.epub"
    bad.write_bytes(b"not a zip")
    md = book_metadata(bad)
    assert md == {"authors": [], "translators": [], "title": None, "source": ""}


# ── reading-order title page (the h1 zip-order bug) ─────────────────────────
def test_title_page_text_uses_spine_order_not_zip_order(tmp_path):
    # zip-directory order puts a body chapter FIRST; spine (reading order) puts
    # the title page first. title_page_text must return the title page.
    opf = ('<?xml version="1.0"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
           '<manifest>'
           '<item id="ch7" href="chap07.html" media-type="application/xhtml+xml"/>'
           '<item id="title" href="title.html" media-type="application/xhtml+xml"/>'
           '</manifest>'
           '<spine><itemref idref="title"/><itemref idref="ch7"/></spine>'
           '</package>')
    p = tmp_path / "b.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container><rootfiles>'
                   '<rootfile full-path="content.opf"/></rootfiles></container>')
        # chap07 physically first in the zip (scrambled), opens with an epigraph
        z.writestr("chap07.html",
                   "<html><body><p>Death and Impermanence. … the practice of "
                   "bodhisattvas. TOGME SANGPO, The Thirty-Seven Practices.</p>"
                   "</body></html>")
        z.writestr("title.html",
                   "<html><body><h1>How to Meditate on the Stages of the Path</h1>"
                   "<p>Kathleen McDonald</p><p>Wisdom Publications</p></body></html>")
        z.writestr("content.opf", opf)
    fm = title_page_text(p)
    assert "Kathleen McDonald" in fm
    assert fm.index("Kathleen McDonald") < fm.index("TOGME SANGPO")  # title page first


def test_agreed_author_retained_over_epigraph_name():
    # filename + metadata agree on McDonald; the LLM (wrongly) returns an epigraph
    # author. The agreed author must survive.
    ladder = _ladder(json.dumps({
        "authors": ["Tokme Zangpo"], "translators": [], "confidence": 0.7,
        "evidence": "Tokme Zangpo, The Thirty-Seven Practices"}))
    r = ContributorResolver().resolve(
        edition_title="How to Meditate -- Kathleen McDonald -- Wisdom -- 9781614298939",
        front_matter="…the practice of bodhisattvas. Tokme Zangpo, Thirty-Seven Practices…",
        meta={"authors": ["Kathleen McDonald"], "translators": []},
        ladder=ladder)
    assert "Kathleen McDonald" in r.authors          # agreed prior kept
    assert r.authors[0] == "Kathleen McDonald"
