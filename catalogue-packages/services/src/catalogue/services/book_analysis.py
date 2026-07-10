"""Book-level analysis over LOCATED SECTIONS (§4.6, §4.7) — container model.

The model (per the user): **a book is a container of one or more works.** Every
book has author(s) and optional translator(s); in the degenerate single-work
case the book *is* the work and the book-level contributors ARE the work's.
Chapters are NOT works. A book becomes a MULTI-work container only when it holds
several genuinely distinct reproduced texts.

So this module is deliberately CONSERVATIVE about splitting:

  1. PEEK each located section (`peek_section`) — a verse-form gate (homage +
     consecutive stanzas) + an opening attribution, deterministic.
  2. A section is a WORK ANCHOR only if it (a) reads as a reproduced text (passes
     the verse gate) AND (b) carries a genuinely distinct WORK title — not a page
     label ('page0037'), a numbered/roman chapter ('1 – …', 'I. A Reed'), a bare
     'Chapter N'/'Appendix', or a running header. (`_is_distinct_work_title`.)
  3. ≥2 anchors → a multi-work container: each anchor's run (anchor + following
     chapters) is one Work, author from its own attribution (LLM peek fills a
     hidden one); per-work translator is rare → inherited from the book downstream.
  4. <2 anchors → ONE work = the whole book (handled in process.py, which has the
     book-level author/translator). When many sections read as verse but titles are
     unusable (book 45 Mind Training: page-label bookmarks for a real 43-text
     anthology), structure is `collection_unsegmented` — one book-work, FLAGGED
     for manual splitting rather than silently exploded into 100 page-works.

This replaced the v15 chapter-merge/author-propagation path, which over-segmented
chapters and subchapters into hundreds of authorless "texts" on the real corpus.
Per-work author/translator INHERITANCE from the book level happens in process.py
(it owns the title-page-resolved book contributors); results cache in
`section_cache (file_hash, section_version)`.

**`enable_verse_gate` (default False).** The conservative refuse-to-segment behaviour
above — the verse-form gate in `peek_section`, the sustained-verse/attribution onset
in `_is_anchor`, and the `_MAX_WORKS`/`_COLLECTION_MIN` collapse guards in
`analyze_book_sections` — is the AUTO-DETECTION mode, kept ONLY for `--enable-verse-gate`.
An audit (see `multi_work_segmentation.md`) showed it cannot tell an anthology from a
single work and mislabels both, so it is OFF by default. With the gate OFF the engine
default-INCLUDES: every surviving (non-front/back) section with a distinct work title
is a work candidate (verse/attribution become author hints, never a filter), and the
collapse guards do not fire. Off is the right default for *labeled* segmentation, where
the caller has already declared the book a collection; on restores the old guessing.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Optional

from .classify import Rung, _is_front_back, _lenient_json, _run_ladder
from .locator import Section, opens_with_verse


# ── Content-peek per section (reads the located section, not the title) ──────
_PEEK_SYS = (
    "You are shown the OPENING of one located book section (title, then its first "
    "lines). Decide whether it REPRODUCES a canonical Buddhist text — a 'root' "
    "scripture/treatise/verse-work or a 'commentary' on one — or is "
    "narrative/biographical/discussion prose ('other'). Reproduced texts open "
    "with verse (a homage/obeisance then numbered short lines) and/or name the "
    "original composer near the title. Output ONLY JSON, like:\n"
    '{"kind": "root", "author": "Nāgārjuna", "confidence": 0.9}\n'
    "kind ∈ root|commentary|other. author = the ORIGINAL composer if named in the "
    "opening (null otherwise); never the modern translator/editor; never invent one."
)

_VERSE_REPRODUCE = 0.50      # ≥ → a reproduced (verse) text; the GATE for root/commentary
_ANCHOR_VERSE = 0.85        # a verse-only anchor must be SUSTAINED verse (homage + stanzas),
                            # not a prose chapter that merely quotes one numbered stanza
                            # (those score 0.6–0.75: numbered lines but no homage)
_COLLECTION_MIN = 5          # ≥ this many reproduced sections w/o anchors → flag unsegmented
_MAX_WORKS = 8              # > this many "distinct" works → almost certainly mis-segmented
                            # bookmarks; flag as a collection rather than explode (safe bias)


@dataclass
class PeekVerdict:
    title: str
    kind: str                 # root | commentary | other | front_back
    author: Optional[str]
    verse: float
    via: str                  # frontback | low-verse | verse+attrib | llm | no-llm


def _clean_author(a: Optional[str]) -> Optional[str]:
    if not a:
        return None
    a = a.split("\n")[0].strip(" .,;:’'\"")
    return a or None


def peek_section(section: Section, *, ladder: Optional[list[Rung]] = None,
                 enable_verse_gate: bool = False) -> PeekVerdict:
    """Classify ONE located section into root/commentary/other + author.

    With `enable_verse_gate` (auto-detection mode), verse form is the GATE: a section
    is a reproduced text ONLY if its verse score clears the threshold (an attribution
    phrase in prose is NOT enough). With the gate OFF (the default), a non-front/back
    section is taken as a reproduced-text candidate regardless of verse — the caller
    has declared the book a collection, so prose works are not filtered out. Either
    way the deterministic opening attribution settles the author; the LLM is used ONLY
    to extract the author of a reproduced text that lacks a clean attribution (155:
    the verses are numbered but the author is named in prose)."""
    t = section.title
    if _is_front_back(t):
        return PeekVerdict(t, "front_back", None, section.verse, "frontback")
    if enable_verse_gate and section.verse < _VERSE_REPRODUCE:
        return PeekVerdict(t, "other", None, section.verse, "low-verse")

    is_comm = "commentary" in (t.lower() + " " + section.opening(160).lower())
    attrib = _clean_author(section.attribution)
    if attrib:
        return PeekVerdict(t, "commentary" if is_comm else "root", attrib,
                           section.verse, "verse+attrib")
    if ladder is None:
        return PeekVerdict(t, "commentary" if is_comm else "root", None,
                           section.verse, "no-llm")
    d = _lenient_json(_run_ladder(
        [{"role": "system", "content": _PEEK_SYS},
         {"role": "user", "content": f"SECTION TITLE: {t}\n\nOPENING:\n{section.opening(1500)}"}],
        ladder, max_tokens=250))
    d = d if isinstance(d, dict) else {}
    # root vs commentary is decided DETERMINISTICALLY by the 'commentary' keyword
    # (the LLM leans 'commentary'); the LLM only supplies the author it couldn't parse.
    author = _clean_author(d.get("author")) or attrib
    return PeekVerdict(t, "commentary" if is_comm else "root", author,
                       section.verse, "llm")


# ── Distinct-work-title test (the de-segmentation lever) ─────────────────────
# "Part Two: <work title>" carries a real work title → distinct. A page label, a
# numbered/roman chapter heading, a bare structural word, or a running header is
# a chapter/fragment, NOT a work.
_PART_PARENT = re.compile(r'^\s*part\s+[\w-]+\s*[:.–—-]+\s*(?P<w>.+\S)\s*$', re.I)
_PAGE_LABEL = re.compile(r'^\s*(?:page|pg|p|folio|fol)\.?\s*\d', re.I)
_NUMBER_START = re.compile(r'^\s*\(?(?:\d{1,4}|[ivxlcdm]+)\s*[.)\s:–—-]', re.I)
_CHAPTER_WORD = re.compile(
    r'^\s*(?:chapter|canto|adhy[aā]ya|pariccheda|lesson|book|section|verse|'
    r'appendi\w*|part|division|volume|fascicle|tome|preface|introduction|'
    r'prologue|epilogue|glossary|bibliography|notes?|index|contents|'
    r'foreword|afterword)\b', re.I)
# A spelled-out ordinal used as a chapter number: 'ONE - Action Tantra',
# 'TWO. …', 'FIFTEEN — …'. The trailing separator distinguishes it from a real
# title that merely starts with the word (e.g. 'One Hundred Verses').
_ORDINAL_CHAPTER = re.compile(
    r'^\s*(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
    r'thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|'
    r'first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)'
    r'\s*[-:.)–—]', re.I)
# Exegetical / outline sub-headings (the traditional sa-bcad structure of a
# commentary) — these are parts of a work, not works (book 51's Gyaltsab BCA
# commentary; book 230's 'The Root Verses' / 'The Auto-commentary').
_SECTION_LABEL = re.compile(
    r'^\s*(?:the\s+)?(?:homage|obeisance|purpose|summary|conclusion|dedication|'
    r'meaning|explanation|the\s+actual|the\s+title|root\s+verses?|'
    r'auto-?commentary|colophon|outline|abstract|remarks?|illustrations?)\b', re.I)


# Editorial/back-matter apparatus the user flagged that `_is_front_back` doesn't
# already cover (it handles contents/foreword/preface/introduction/index/glossary/
# bibliography/notes/appendix/"about the"/"list of"/"translator's"/…). A section
# whose title matches is NOT a contained work. Prefix-anchored where a substring
# would over-fire ("about", "also from"); a contained `Translator`/`Editor`/`Author`
# is back matter ("About the Author", "Note from the Editor").
_APPARATUS_PREFIX = re.compile(
    r'^\s*(?:comments?|how\s+to\s+use|more\s+reading|further\s+reading|e-?mail|'
    r'what\s+to\s+read|also\s+from|about\b|biograph|publisher)', re.I)
_APPARATUS_CONTAINS = re.compile(r'\b(?:translators?|editors?|author)\b', re.I)


def _is_apparatus_title(title: str) -> bool:
    t = title or ""
    return bool(_APPARATUS_PREFIX.match(t) or _APPARATUS_CONTAINS.search(t))


# ── Author embedded in a section title (Skt/Tib transliteration only) ─────────
# "<title> by <Author>" and "<Author>'s <title>" name the work's author IN the TOC
# title. We split it out ONLY when the name reads as a Sanskrit/Tibetan
# transliteration — positive evidence (IAST/phonetic diacritics, a Tibetan name
# marker, or a Skt/Tib name affix) — so "by"/"'s" is not torn out of an ordinary
# English title ("Liberation by Hearing", "The Peacock's Neutralizing of Poison").
_TRANSLIT_MARK = re.compile(r"[āīūṛṝḷṅñṭḍṇśṣṃḥĀĪŪṚṆŚṢṬḌÑṄöüéèÖÜÉ]")
_TIB_NAME_MARKER = re.compile(
    r"\b(?:rinpoche|lots[aā]wa|lama|khenpo|geshe|tulku|lingpa|gyatso|gyaltsen|"
    r"rangdrol|dorje|khandro|sumg[oö]n|tsongkhapa|chenpo|khyentse)\b", re.I)
_TRANSLIT_AFFIX = re.compile(
    r"(?:pa|po|wa|tsen|gyal|drak|bhadra|garbha|rak[sṣ]ita|k[iī]rti|mitra|deva|"
    r"datta|gupta|pada|sena|sengi|[sś]r[iī]|vajra|n[aā]tha|p[aā]la|dhara|siddhi)$",
    re.I)
_EPITHET = re.compile(
    r"^(?:glorious|lord|master|venerable|bodhisattva|[aā]rya|[aā]c[aā]rya|great|"
    r"holy|the|protector|siddha|je|jetsun|jetsün|kyabje|khenchen|khenpo|gyalwang|"
    r"lama|geshe|drubwang|choje|lopon)\s+", re.I)
_BY_RE = re.compile(r"^(?P<title>.+?\S)\s+by\s+(?P<auth>\S.+?)\s*$", re.I)
_POSS_RE = re.compile(r"^(?P<auth>[^\W\d_][\w’'.\- ]*?)[’']s\s+(?P<title>\S.+)$")


def _is_translit_name(s: str) -> bool:
    s = (s or "").strip()
    words = s.split()
    if not s or len(words) > 4:
        return False
    if _TRANSLIT_MARK.search(s) or _TIB_NAME_MARKER.search(s):
        return True
    return any(_TRANSLIT_AFFIX.search(re.sub(r"[^\w]", "", w)) for w in words)


def _strip_epithet(name: str) -> str:
    prev = None
    while prev != name:
        prev, name = name, _EPITHET.sub("", name).strip()
    return name


@functools.lru_cache(maxsize=1)
def _english_words() -> frozenset:
    """The system English wordlist (macOS/Unix), for telling a transliterated name
    from an ordinary English word. Empty if unavailable → callers stay strict."""
    for path in ("/usr/share/dict/words", "/usr/dict/words"):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                return frozenset(w.strip().lower() for w in f if w.strip())
        except OSError:
            continue
    return frozenset()


# Generic Buddhist class-terms used possessively ("Dakini's …", "Yogi's …") — NOT a
# dictionary word, but also NOT a personal author. (Honorific epithets like
# 'bodhisattva'/'master' are handled by `_strip_epithet`; 'buddha' is in the
# dictionary.)
_GENERIC_TERM = frozenset({
    "dakini", "daka", "yogi", "yogin", "yogini", "arhat", "arhant", "mahasiddha",
    "siddha", "pandita", "bhikshu", "bhikkhu", "sravaka", "naga", "yidam", "heruka",
    "deity", "goddess", "king", "queen", "monk", "nun", "sage", "guru",
})
# A connective word means the candidate is a title phrase ("The Story OF Atisa"),
# not a personal name.
_NAME_CONNECTIVE = frozenset({
    "of", "the", "and", "in", "on", "to", "for", "from", "with", "at", "or",
    "a", "an", "by",
})


def _is_author_name(name: str) -> bool:
    """True if `name` reads as a personal author name (for the "<Name>'s <title>" /
    "<title> by <Name>" split). After stripping leading honorifics the NAME is first;
    accept it iff that first word is a real name — explicit Skt/Tib evidence
    (`_is_translit_name`), or simply NOT an ordinary English word (a diacritic-less
    transliteration the script test misses: 'Atisa', 'Kusulu'). Reject English words
    ('Peacock', 'Poison', 'Story'), connective phrases ('The Story of Atisa'), and
    generic class-terms ('Dakini', 'Yogi'). Stays strict if no wordlist is present."""
    s = _strip_epithet((name or "").strip())
    words = re.findall(r"[A-Za-zÀ-῿'’.-]+", s)
    if not (1 <= len(words) <= 3):
        return False
    if any(w.lower() in _NAME_CONNECTIVE for w in words):
        return False
    first = words[0].lower()
    if first in _GENERIC_TERM:
        return False
    if _is_translit_name(s):
        return True
    ew = _english_words()
    if not ew:
        return False                         # no dictionary → stay strict
    return first not in ew                   # not English → a name (the name is first)


def split_title_author(title: str, *, by: bool = True,
                       possessive: bool = True) -> tuple[str, Optional[str]]:
    """Split an author embedded in a section title — '<title> by <Name>' or
    "<Name>'s <title>" — returning (clean_title, author) when <Name> reads as an
    author name (Skt/Tib transliteration, or simply not an English word), else
    (title, None). So "Atisa's Seven-Point …" / "Kusulu's Accumulation …" split, but
    "The Peacock's Neutralizing of Poison" does not."""
    t = (title or "").strip()
    if by:
        m = _BY_RE.match(t)
        if m:
            auth = m.group("auth").strip()
            if _is_author_name(auth):
                return m.group("title").strip(), (_strip_epithet(auth) or auth)
    if possessive:
        m = _POSS_RE.match(t)
        if m:
            auth = m.group("auth").strip()
            if _is_author_name(auth):
                return m.group("title").strip(), (_strip_epithet(auth) or auth)
    return t, None


def _part_work(title: Optional[str]) -> Optional[str]:
    m = _PART_PARENT.match(title or "")
    if not m:
        return None
    w = m.group("w").strip()
    return w if len(w.split()) >= 2 else None


def _mostly_upper(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= 0.7


def _is_distinct_work_title(title: str) -> bool:
    """True iff `title` looks like a standalone WORK title (not a chapter / page
    label / scan-id / running header). The single strongest lever against
    over-segmentation — the real corpus' bookmark titles are dominated by
    'page0037' labels, scan ids ('str_20160405_0002_2R'), numbered chapters, and
    OCR-mangled strings carrying null bytes, none of which is a Work."""
    raw = title or ""
    # OCR/bookmark garbage: a null byte or other control char is never a title.
    if any(ord(c) < 32 and c not in "\t" for c in raw):
        return False
    t = raw.strip()
    if not t:
        return False
    if _is_apparatus_title(t):              # back matter / editorial apparatus
        return False                        # (checked before _part_work: 'About …')
    if _part_work(t):                       # "Part Two: Precious Garland…" → a work
        return True
    if _is_front_back(t) or _CHAPTER_WORD.match(t):
        return False
    if _ORDINAL_CHAPTER.match(t) or _SECTION_LABEL.match(t):
        return False                        # "ONE - Action Tantra", "Homage", "Summary"
    if _PAGE_LABEL.match(t) or _NUMBER_START.match(t):
        return False                        # "page0037", "1 – Devatā…", "I. A Reed"
    if re.search(r"\d{4,}", t):             # page/scan/date ids: page0037, str_20160405
        return False
    letters = sum(c.isalpha() for c in t)
    if letters < 0.5 * len(t):              # mostly digits/symbols → a label, not a title
        return False
    if _mostly_upper(t) and len(t.split()) <= 4:
        return False                        # running header
    words = re.findall(r"[^\W\d_]+", t)
    return len(words) >= 2 or (len(words) == 1 and len(words[0]) >= 6)


# ── Data ────────────────────────────────────────────────────────────────────
@dataclass
class ContainedText:
    """A DISTINCT reproduced work inside a multi-work container. `authors`/
    `translators` are this work's OWN contributors (empty → inherit the book's,
    done in process.py)."""
    title: str
    authors: list                 # [str], own; [] → inherit book-level
    translators: list             # [str], own; usually [] → inherit book-level
    kind: str                     # root | commentary
    verse: float
    locator: str
    section_titles: list

    def to_dict(self) -> dict:
        return {"title": self.title, "authors": list(self.authors),
                "translators": list(self.translators), "kind": self.kind,
                "verse": round(self.verse, 2), "locator": self.locator,
                "section_titles": self.section_titles}


@dataclass
class BookAnalysis:
    structure: str                # single_work | multi_work | collection_unsegmented
    contained_texts: list         # list[ContainedText] — the DISTINCT works (≥2), else []
    n_sections: int
    source: str                   # epub-nav | pdf-bookmark | pdf-textlayer | ''
    n_reproduced: int = 0         # sections that passed the verse gate (for the flag)

    def to_dict(self) -> dict:
        return {"structure": self.structure, "n_sections": self.n_sections,
                "source": self.source, "n_reproduced": self.n_reproduced,
                "contained_texts": [c.to_dict() for c in self.contained_texts]}


def book_analysis_from_dict(d: dict) -> BookAnalysis:
    return BookAnalysis(
        structure=d.get("structure", "single_work"),
        n_sections=int(d.get("n_sections", 0) or 0),
        source=d.get("source", ""),
        n_reproduced=int(d.get("n_reproduced", 0) or 0),
        contained_texts=[
            ContainedText(
                title=c.get("title", ""),
                authors=list(c.get("authors") or ([c["author"]] if c.get("author") else [])),
                translators=list(c.get("translators") or []),
                kind=c.get("kind", "root"),
                verse=float(c.get("verse", 0.0) or 0.0),
                locator=c.get("locator", ""),
                section_titles=list(c.get("section_titles") or []))
            for c in (d.get("contained_texts") or [])],
    )


# ── Work detection (conservative) ────────────────────────────────────────────
def _is_anchor(sec: Section, verdict: PeekVerdict, *,
               enable_verse_gate: bool = False) -> bool:
    """A section that starts a distinct reproduced WORK.

    Gate OFF (default): a distinct work title on a reproduced-candidate section is
    enough — the book is already declared a collection, so we do NOT additionally
    demand a verse/attribution onset (that would drop every prose work).

    Gate ON (auto-detection): also requires a STRONG onset — a real opening
    attribution, or SUSTAINED verse (homage + stanzas, verse ≥ _ANCHOR_VERSE). A prose
    study/commentary chapter that merely quotes a numbered stanza opens-with-verse but
    scores only ~0.6–0.75, so it does not anchor; a real text (book 39: verse 1.0 /
    attribution 'Dharmarakṣita') still does."""
    if verdict.kind not in ("root", "commentary"):
        return False
    if not _is_distinct_work_title(sec.title):
        return False
    if not enable_verse_gate:               # declared collection: distinct title is enough
        return True
    if verdict.author:                      # the peek found a real opening attribution
        return True
    return opens_with_verse(sec.text) and sec.verse >= _ANCHOR_VERSE


def _detect_works(pairs: list, *, ladder: Optional[list[Rung]],
                  enable_verse_gate: bool = False, toc_hierarchy: bool = False,
                  title_by_author: bool = True,
                  title_with_possessive: bool = True) -> list:
    """Return the distinct contained Works (one per anchor), or [] when there is
    no anchor (degenerate → the whole book is one Work, emitted by the caller).
    Each anchor's run spans until the next anchor, folding its chapters in. A
    single anchor is kept — it preserves that text's own title + attribution
    rather than collapsing to the generic book title.

    With `toc_hierarchy` on, only the SHALLOWEST anchors are works: a book whose
    TOC nests chapters under parts (Chittamani Tara: 'Part 1 …' / 'Part 2 …' with
    chapters beneath) yields one Work per top-level part, its descendants folded in
    as members sharing the part's kind — not one Work per chapter. Anchors deeper
    than the minimum anchor level are demoted to members. (No effect when the TOC is
    flat — every anchor is already at the same level, e.g. book 51's one-level
    bookmark dump of a single commentary's outline.)"""
    anchors = [i for i, (sec, v) in enumerate(pairs)
               if _is_anchor(sec, v, enable_verse_gate=enable_verse_gate)]
    if toc_hierarchy and anchors:
        top = min(pairs[i][0].level for i in anchors)
        anchors = [i for i in anchors if pairs[i][0].level == top]
    if not anchors:
        return []

    n = len(pairs)
    works: list = []
    seen_titles: set = set()
    for k, start in enumerate(anchors):
        end = anchors[k + 1] if k + 1 < len(anchors) else n
        members = pairs[start:end]
        sec, v = pairs[start]
        title = _part_work(sec.title) or sec.title
        # Author embedded in the title ("<title> by X" / "X's <title>", X a Skt/Tib
        # transliteration) → split it out + clean the title.
        title, title_author = split_title_author(
            title, by=title_by_author, possessive=title_with_possessive)
        # Author precedence — deterministic, NO LLM backfill: the located section's
        # own FIRST-PAGE attribution ("Attributed to X" → peek's v.author) wins; else
        # the author named in the printed Contents (Section.toc_author) or embedded in
        # the title (above).
        author = v.author or getattr(sec, "toc_author", None) or title_author
        # Dedup on (title, author): a repeated heading with the same/no author is a
        # sub-section (book 22's "Appendices", book 230's "Root Verses"), but the same
        # base title by DIFFERENT authors is two works ("A Song" by Tantipa vs Saraha).
        key = (re.sub(r"\s+", " ", title.strip().lower()), (author or "").lower())
        if key in seen_titles:
            continue
        seen_titles.add(key)
        works.append(ContainedText(
            title=title,
            authors=[author] if author else [],
            translators=[],
            kind=v.kind,
            verse=max((mv.verse for _ms, mv in members), default=v.verse),
            locator=sec.locator,
            section_titles=[ms.title for ms, _mv in members]))
    return works


def analyze_book_sections(
    sections: list, *,
    edition_title: Optional[str] = None,
    ladder: Optional[list[Rung]] = None,
    enable_verse_gate: bool = False,
    toc_hierarchy: bool = False,
    title_by_author: bool = True,
    title_with_possessive: bool = True,
) -> BookAnalysis:
    """Container-model book analysis: peek every section (deterministic), detect
    distinct contained works, classify the container. Returns contained_texts=[] for
    a single-work book — process.py then emits the one whole-book Work with the
    book-level author/translator.

    `enable_verse_gate` (default False) selects the conservative auto-detection mode:
    the verse gate, the strong-onset anchor rule, and the `_MAX_WORKS`/`_COLLECTION_MIN`
    collapse-to-`collection_unsegmented` guards all apply ONLY when it is on. With the
    gate off (labeled segmentation), every distinct-titled non-front/back section is a
    work and the collapse guards never fire — so `collection_unsegmented` is an
    auto-detection-only outcome.

    `toc_hierarchy` (default False) is an orthogonal signal: when on, nested TOC depth
    groups a part's chapters under the part (one Work per top-level node) instead of
    emitting each chapter as its own Work. Apply it per-book to collections whose TOC
    nests chapters under parts; leave it off for flat anthologies whose distinct texts
    happen to sit under one wrapper heading (those would wrongly collapse to one Work)."""
    if not sections:
        return BookAnalysis("single_work", [], 0, "", 0)
    pairs = [(s, peek_section(s, ladder=None, enable_verse_gate=enable_verse_gate))
             for s in sections]                                    # bulk pass: no LLM
    n_reproduced = sum(1 for _s, v in pairs if v.kind in ("root", "commentary"))
    works = _detect_works(pairs, ladder=ladder, enable_verse_gate=enable_verse_gate,
                          toc_hierarchy=toc_hierarchy, title_by_author=title_by_author,
                          title_with_possessive=title_with_possessive)

    if enable_verse_gate and len(works) > _MAX_WORKS:
        # too many "distinct" works → bad bookmarks mis-segmented; flag, don't explode
        works = []
        structure = "collection_unsegmented"
    elif works:
        structure = "multi_work" if len(works) >= 2 else "single_work"
    elif enable_verse_gate and n_reproduced >= _COLLECTION_MIN:
        structure = "collection_unsegmented"      # real anthology, titles unusable → flag
    else:
        structure = "single_work"
    return BookAnalysis(structure, works, len(sections),
                        sections[0].source or "", n_reproduced)
