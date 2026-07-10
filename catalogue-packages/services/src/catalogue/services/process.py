"""Step-4 orchestrator: per-holding extraction cascade → validation →
classification → resolver stub.

The function `process_holding(conn, holding_id, cfg)` runs the full
cascade for one holding and is idempotent against the per-stage caches.
"""
from __future__ import annotations

import json
import signal
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


class TOCExtractTimeout(Exception):
    pass


@contextmanager
def _timeout(seconds: int):
    """SIGALRM-based timeout. Main thread only (fine for the serial Step-4 loop)."""
    if seconds <= 0:
        yield
        return

    def _handler(signum, frame):
        raise TOCExtractTimeout(f"extraction exceeded {seconds}s")

    prev = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)

from .book_analysis import BookAnalysis, analyze_book_sections, book_analysis_from_dict


def _acc(conn):
    """A system Access over this connection — engine-routed holding/edition reads, the section-analysis
    cache (`acc.section_cache`), the raw-extract cache + the review queue. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(conn)
from .classify import ClassifyResult, classify_entry, parse_toc_region, Rung
from .contributors import parse_title_contributors
from .extract import book_metadata, title_page_text
from .locator import extract_sections
from .work_canonical_resolver import LiveResolver, ResolverStub
from .toc import (
    TOCEntry, ValidationReport, cache_parsed_toc, extract_structured_outline,
    is_degenerate_outline, is_toc_fragment, load_cached_toc, locate_toc_region,
    parse_contents_index, validate_toc, vision_toc_unavailable, VisionTOCFn,
)


def _default_resolver():
    """`CATALOGUE_RESOLVER=live` switches in the live BDRC/84000 resolver
    (§4.3, §9). Default stays the stub so tests + offline runs stay
    deterministic and free."""
    import os as _os
    if _os.environ.get("CATALOGUE_RESOLVER", "").lower() == "live":
        return LiveResolver()
    return ResolverStub()


@dataclass
class ProcessConfig:
    parse_version: int = 1
    classify_version: int = 2     # bumped (§6): tightened front/back-matter prompt
    section_version: int = 3      # bumped: anchor verse bar + title-rule hardening (de-seg v2)
    confidence_threshold: float = 0.7
    ladder: Optional[list[Rung]] = None
    vision_toc: VisionTOCFn = field(default=vision_toc_unavailable)
    resolver: object = field(default_factory=_default_resolver)
    outline_extractor: Callable[[Path], Optional[list[TOCEntry]]] = field(
        default=extract_structured_outline
    )
    toc_extract_timeout_s: int = 60
    progress_cb: Optional[Callable[[str, int, int], None]] = None
    # [v14] opt-in (runners enable; off by default so existing callers/tests
    # see no behaviour change). use_text_layer_toc: parse a printed Contents
    # page from raw_extract_cache when there's no bookmark/nav outline.
    # analyze_book: book-level structure + contained-text/author extraction →
    # review-queue proposals.
    use_text_layer_toc: bool = False
    analyze_book: bool = False
    # enable_verse_gate: the conservative auto-detection mode (verse gate + strong
    # onset + collapse-to-collection guards). OFF by default — an audit showed it
    # mislabels anthologies and single works alike (multi_work_segmentation.md), so
    # the pipeline default-includes; `--enable-verse-gate` restores the old guessing.
    enable_verse_gate: bool = False
    # toc_hierarchy: group nested TOC chapters under their top-level part (one Work
    # per part) instead of one Work per chapter. OFF by default; apply per-book to
    # collections whose TOC nests chapters under parts (see book_analysis.py).
    toc_hierarchy: bool = False
    # title_by_author / title_with_possessive: extract a Skt/Tib author embedded in a
    # section title ("<title> by X" / "X's <title>") and clean the title. ON by default.
    title_by_author: bool = True
    title_with_possessive: bool = True


def apply_volume_preset(cfg, *, single_author_multi_work: bool = False,
                        multi_author: bool = False):
    """Apply a mutually-exclusive volume-type PRESET over the granular
    segmentation flags. A preset is an operator assertion about a known book and
    overrides the granular defaults; passing neither leaves `cfg` untouched.

    - ``single_author_multi_work`` — one author, chapters grouped into parts:
      ``toc_hierarchy`` on, per-work author parsing (``title_by_author`` /
      ``title_with_possessive``) off.
    - ``multi_author`` — flat anthology, each section names its own author
      (implies multi-work): ``title_by_author`` / ``title_with_possessive`` on,
      ``toc_hierarchy`` off.

    Duck-typed: `cfg` may be a `ProcessConfig` or any object with the three
    attributes (so a runner can resolve the preset without building a full
    config). Returns `cfg`.
    """
    if single_author_multi_work and multi_author:
        raise ValueError(
            "single_author_multi_work and multi_author are mutually exclusive")
    if single_author_multi_work:
        cfg.toc_hierarchy = True
        cfg.title_by_author = False
        cfg.title_with_possessive = False
    elif multi_author:
        cfg.title_by_author = True
        cfg.title_with_possessive = True
        cfg.toc_hierarchy = False
    return cfg


@dataclass
class ProcessReport:
    holding_id: int
    cached_toc: bool = False
    extracted_entries: int = 0
    validation: Optional[ValidationReport] = None
    classifications: list[ClassifyResult] = field(default_factory=list)
    queued_for_digitization: bool = False
    queued_low_confidence: bool = False
    book_structure: Optional[str] = None       # [v14] root_plus_commentary / modern_study / …
    book_authors: list = field(default_factory=list)        # § 9 book-level contributors
    book_translators: list = field(default_factory=list)
    n_works: int = 0                                        # works in the container (≥1)
    works: list = field(default_factory=list)              # the container's work dicts
                                                            # (title/authors/…) for display


def _pdf_page_count(file_path: Optional[str]) -> Optional[int]:
    """Page count for a PDF (for the per-page-density junk-outline check), or None."""
    if not file_path or not str(file_path).lower().endswith(".pdf"):
        return None
    try:
        import fitz
        doc = fitz.open(file_path)
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        return None


def _read_raw_text(conn, file_hash: Optional[str]) -> str:
    """Cached full body text for this file (populated by the sweep into
    raw_extract_cache), or ''. Source for the text-layer TOC fallback (§4.7)
    and book-level analysis (§4.6) — no re-extraction."""
    if not file_hash:
        return ""
    return _acc(conn).editions.reads.raw_text_for_hash(file_hash) or ""


# ── Section-analysis cache (§5, [v15]) ──────────────────────────────────────
# We cache the ANALYSIS (structure + contained texts), not the heavy section text —
# enough to re-emit the review proposal idempotently and skip the expensive
# re-extract + per-section peek on a re-run. The cache lives behind
# `acc.section_cache` (self-bootstrapping: `store` issues the CREATE, `load`
# tolerates the table's absence as a miss — see that repo for the staging-shim
# journaling rationale).
def load_section_analysis(conn, file_hash: Optional[str], section_version: int):
    """Cached BookAnalysis for this file, or None (incl. when the table does not
    exist yet — the first run before any store/load created it)."""
    if not file_hash:
        return None
    raw = _acc(conn).section_cache.reads.load(file_hash, section_version)
    return book_analysis_from_dict(json.loads(raw)) if raw else None


def store_section_analysis(conn, file_hash: Optional[str],
                           section_version: int, analysis) -> None:
    if not file_hash:
        return
    _acc(conn).section_cache.writes.store(
        file_hash, section_version, json.dumps(analysis.to_dict()))


def process_holding(conn, holding_id: int,
                    cfg: Optional[ProcessConfig] = None) -> ProcessReport:
    cfg = cfg or ProcessConfig()

    def _p(stage: str, cur: int = 0, total: int = 0):
        if cfg.progress_cb:
            try:
                cfg.progress_cb(stage, cur, total)
            except Exception:
                pass

    row = _acc(conn).holdings.reads.process_fields(holding_id)
    if not row:
        raise ValueError(f"no holding {holding_id}")
    edition_id, file_path, file_hash, text_status = row

    edition_title = _acc(conn).editions.reads.get(edition_id).title

    rep = ProcessReport(holding_id=holding_id)

    # § Image-only / no text layer → queue for digitization; do NOT run
    # the cascade. Step 6 picks it up via text_status='image_only'.
    if text_status in ("image_only", "none"):
        _queue(conn, "low_confidence_extraction", {
            "holding_id": holding_id,
            "reason": f"text_status={text_status}",
            "stage": "step4_pre_extract",
        })
        rep.queued_for_digitization = True
        conn.commit()
        return rep

    # § 5 + § 12.3: cache before extraction.
    _p("load-cache")
    pdf_pages = _pdf_page_count(file_path)
    entries = load_cached_toc(conn, file_hash=file_hash,
                              parse_version=cfg.parse_version)
    if entries is not None and is_degenerate_outline(
            [e.title for e in entries], page_count=pdf_pages):
        # A previously-cached junk outline (book 45 page labels, book 22 one-per-page)
        # is poisoned: it was the bad bookmark outline, not the real Contents. Treat as
        # a cache miss so the printed-Contents text-layer parser below gets a chance.
        entries = None
    if entries is not None:
        rep.cached_toc = True
    else:
        _p("extract-toc")
        try:
            with _timeout(cfg.toc_extract_timeout_s):
                entries = cfg.outline_extractor(Path(file_path)) if file_path else None
        except TOCExtractTimeout:
            _queue(conn, "low_confidence_extraction", {
                "holding_id": holding_id,
                "reason": f"outline_extractor_timeout_{cfg.toc_extract_timeout_s}s",
                "stage": "step4_extract",
            })
            rep.queued_for_digitization = True
            conn.commit()
            return rep
        if entries and is_degenerate_outline(
                [e.title for e in entries], page_count=pdf_pages):
            # Outline is present but junk — page-label (book 45) or one-bookmark-per-
            # page (book 22). No work signal. Drop it so the printed-Contents parser
            # runs instead of caching the junk outline as section titles.
            entries = None
        if not entries and cfg.use_text_layer_toc and file_hash:
            # §4.7 rung between structured outline and vision: a printed
            # Contents page in the text layer (scanned PDFs with no bookmarks).
            _p("text-layer-toc")
            raw = _read_raw_text(conn, file_hash)
            if raw and not is_toc_fragment(raw, edition_title or ""):
                # Deterministic numbered-Contents parser first (no LLM; self-locating,
                # captures per-work authors — book 45). Only if it finds no numbered
                # index fall back to the LLM region parse.
                entries = parse_contents_index(raw)
                if not entries:
                    region = locate_toc_region(raw)
                    if region:
                        entries = parse_toc_region(region, ladder=cfg.ladder)
        if not entries:
            _p("vision-toc")
            try:
                with _timeout(cfg.toc_extract_timeout_s):
                    entries = cfg.vision_toc(Path(file_path)) if file_path else None
            except TOCExtractTimeout:
                _queue(conn, "low_confidence_extraction", {
                    "holding_id": holding_id,
                    "reason": f"vision_toc_timeout_{cfg.toc_extract_timeout_s}s",
                    "stage": "step4_extract",
                })
                rep.queued_for_digitization = True
                conn.commit()
                return rep
        if not entries:
            # No extractable TOC (no outline, no parseable printed Contents,
            # vision stubbed). The book still HAS text, so per the container model
            # it's the degenerate single-work case: emit a whole-book work with the
            # title-page-resolved author/translator instead of bailing empty. Flag
            # it as an advisory NOTE (verify it isn't actually a multi-text
            # collection we couldn't segment), not a real extraction failure.
            if cfg.analyze_book:
                _p("book-analysis")
                empty = BookAnalysis("single_work", [], 0, "no-toc", 0)
                _emit_book_proposal(conn, holding_id=holding_id,
                                    edition_title=edition_title, file_path=file_path,
                                    file_hash=file_hash, analysis=empty, cfg=cfg,
                                    rep=rep, no_toc=True)
                _queue(conn, "extraction_note", {
                    "holding_id": holding_id,
                    "reason": "no_toc_whole_book",
                    "note": "no TOC found; catalogued as a single whole-book work — "
                            "verify it is not a multi-text collection",
                })
            else:
                _queue(conn, "low_confidence_extraction", {
                    "holding_id": holding_id,
                    "reason": "no_outline_and_no_vision_toc",
                    "stage": "step4_extract",
                })
                rep.queued_for_digitization = True
            conn.commit()
            return rep
        # Persist the parsed TOC keyed by (file_hash, parse_version) (§5).
        cache_parsed_toc(conn,
                         file_hash=file_hash,
                         parse_version=cfg.parse_version,
                         entries=entries)

    rep.extracted_entries = len(entries)
    _p("validate", 0, len(entries))

    # § 6: validate. These checks are ADVISORY — processing always continues and
    # the book still gets a full proposal, so a failure is an `extraction_note`
    # (benign "heads up", e.g. a thin/huge/short-title TOC), NOT a real extraction
    # failure. (Genuine failures — image-only, timeouts — stay low_confidence_
    # extraction above.)
    validation = validate_toc(entries)
    rep.validation = validation
    if not validation.ok:
        _queue(conn, "extraction_note", {
            "holding_id": holding_id,
            "issues": validation.issues,
            "entry_count": validation.entry_count,
            "stage": "step4_validate",
        })
        rep.queued_low_confidence = True

    # § 4.9: per-entry escalation. Each entry's classification cache hit
    # short-circuits the ladder — settled entries never re-climb.
    total_entries = len(entries)
    for idx, entry in enumerate(entries, 1):
        _p("classify", idx, total_entries)
        result = classify_entry(
            conn, entry.title,
            edition_title=edition_title,
            classify_version=cfg.classify_version,
            threshold=cfg.confidence_threshold,
            ladder=cfg.ladder,
        )
        rep.classifications.append(result)
        if result.confidence < cfg.confidence_threshold:
            _queue(conn, "toc_classification", {
                "holding_id": holding_id,
                "title": entry.title,
                "kind": result.kind,
                "confidence": result.confidence,
                "rung": result.rung,
            })

    # § 9: resolver stub — call so the cache marker is laid down (and so
    # bumping resolver_version later invalidates cleanly).
    for idx, entry in enumerate(entries, 1):
        _p("resolve", idx, total_entries)
        cfg.resolver.resolve_work(conn, entry.title)

    # § 4.6: book-level analysis over LOCATED SECTIONS — [v15]. Supersedes the
    # v14 flattened-raw_text path (classify_book_structure / extract_book_metadata
    # on per-entry TOC-title kinds, which was non-deterministic — book 60 gave
    # 3 vs 50 'root' across runs). We locate each section's real content
    # (locator.extract_sections), peek it (verse-gated root/commentary + author),
    # merge over-segmented chapter runs, and drop quoted-but-not-reproduced verse.
    # These are REVIEW-QUEUE PROPOSALS (BDRC-verify before trusting, §4.3) —
    # never written as authoritative work/person rows here.
    if cfg.analyze_book and entries:
        _p("book-analysis")
        analysis = load_section_analysis(conn, file_hash, cfg.section_version)
        if analysis is None:
            sections = (extract_sections(Path(file_path), toc_entries=entries)
                        if file_path else None)
            analysis = analyze_book_sections(
                sections or [], edition_title=edition_title, ladder=cfg.ladder,
                enable_verse_gate=cfg.enable_verse_gate,
                toc_hierarchy=cfg.toc_hierarchy,
                title_by_author=cfg.title_by_author,
                title_with_possessive=cfg.title_with_possessive)
            store_section_analysis(conn, file_hash, cfg.section_version, analysis)
        _emit_book_proposal(conn, holding_id=holding_id,
                            edition_title=edition_title, file_path=file_path,
                            file_hash=file_hash, analysis=analysis, cfg=cfg, rep=rep)

    _p("commit")
    conn.commit()
    _p("done")
    return rep


def _emit_book_proposal(conn, *, holding_id, edition_title, file_path, file_hash,
                        analysis, cfg, rep, no_toc: bool = False) -> None:
    """§4.6/§9: resolve book-level author(s)/translator(s) and emit the container's
    work list as a `book_toc_pattern` proposal. The TITLE PAGE is the authority —
    embedded metadata + the filename are only hints the LLM reconciles against it
    (local Ollama, cached). The title page is read in READING order
    (title_page_text), not the scrambled-zip-order raw_extract_cache head; fall
    back to the raw head only if the file is unreadable. Shared by the normal
    section-analysis path and the no-TOC whole-book fallback. Proposals are
    review-queue items (BDRC-verify before trusting, §4.3)."""
    rep.book_structure = analysis.structure
    front_matter = (title_page_text(Path(file_path)) if file_path else "") \
        or _read_raw_text(conn, file_hash)
    meta = book_metadata(Path(file_path)) if file_path else None
    contrib = cfg.resolver.resolve_contributors(
        conn,
        cache_key=file_hash or edition_title or str(holding_id),
        edition_title=edition_title,
        front_matter=front_matter,
        meta=meta,
        ladder=cfg.ladder,
    )
    rep.book_authors = list(contrib.authors)
    rep.book_translators = list(contrib.translators)

    # Container model: a book is 1+ works. A multi-work container emits its
    # distinct works (each inheriting the book's author/translator where it names
    # none); otherwise the WHOLE BOOK is one work whose author/translator ARE the
    # book's. Always ≥1 work.
    works = _build_works(analysis, contrib, edition_title)
    rep.n_works = len(works)
    rep.works = works
    _queue(conn, "book_toc_pattern", {
        "holding_id": holding_id,
        "structure": analysis.structure,
        "source": analysis.source,
        "n_sections": analysis.n_sections,
        "unsegmented": analysis.structure == "collection_unsegmented",
        "no_toc": no_toc,
        "book_authors": contrib.authors,
        "book_translators": contrib.translators,
        "contributors_source": contrib.source,
        "contributors_verified": contrib.verified,
        "contributors_confidence": round(contrib.confidence, 2),
        "works": works,
        "contained_texts": works,        # legacy key — same list
    })


def _build_works(analysis, contrib, edition_title: Optional[str]) -> list:
    """Container model → the proposal's work list. Multi-work container: the
    distinct works, each inheriting the book's author/translator where it names
    none. Otherwise (single work / unsegmentable collection): ONE whole-book work
    whose author/translator ARE the book's. Always returns ≥1 work.

    Book-level AUTHOR is promoted to a contained work ONLY when the whole collection
    is uniformly the book author's — i.e. NO contained work names its own author (its
    own TOC entry or first-page attribution, `c.authors`). If ANY work names a specific
    author, the authorless ones are left ANONYMOUS (empty → review), not silently the
    book's: a compiler/translator is not the author of every text in an anthology
    (book 45 — many texts name their author, so none inherits the compiler). [Per-PART
    sectioning of this rule pairs with the container rule, not yet built; today the
    collection is treated as one section.] Translator still inherits freely (a single
    translator typically renders the whole book)."""
    book_authors = list(contrib.authors)
    book_translators = list(contrib.translators)
    if analysis.contained_texts:                       # ≥2 distinct works
        any_specific_author = any(c.authors for c in analysis.contained_texts)
        out = []
        for c in analysis.contained_texts:
            if c.authors:
                authors, author_inherited = list(c.authors), False
            elif not any_specific_author:              # uniform-authorship collection
                authors, author_inherited = book_authors, bool(book_authors)
            else:                                      # mixed → this work is anonymous
                authors, author_inherited = [], False
            out.append({
                "title": c.title,
                "authors": authors,
                "translators": list(c.translators) or book_translators,
                "author_inherited": author_inherited,
                "translator_inherited": not c.translators,
                "kind": c.kind,
                "locator": c.locator,
                "section_titles": c.section_titles,
                "whole_book": False,
            })
        return out
    # Degenerate: the whole book is one work.
    _cands, clean_title = parse_title_contributors(edition_title)
    return [{
        "title": clean_title or (edition_title or "").strip() or "(untitled)",
        "authors": book_authors,
        "translators": book_translators,
        "author_inherited": False,
        "translator_inherited": False,
        "kind": "work",
        "locator": "",
        "section_titles": [],
        "whole_book": True,
        "unsegmented": analysis.structure == "collection_unsegmented",
    }]


def _queue(conn, item_type: str, payload: dict) -> None:
    _acc(conn).review.writes.enqueue(item_type, payload)
