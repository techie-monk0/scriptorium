"""OCR-tolerant parser for the Cataloging-in-Publication (CIP) block printed on a
book's copyright page. It turns the CIP record into a structured `CipRecord`
(title, ISBNs, LCCN, year, authors, publisher) so the title and the identifier come
from the SAME block — the basis for trusting an ISBN only when it agrees with the
book's own printed title.

Designed for SCANNED PDFs whose OCR is noisy (e.g. "p. cm." → "p. em.", "Title" →
"Tide", "0" → "o", a middle-dot "·" for a period). Handles the formats books
actually ship with:

  • LABELLED (modern LC, ~2015+):
      Names: McDonald, Kathleen, 1952– author.
      Title: How to meditate on the stages of the path: a guide to the Lamrim / by …
      Description: First edition. | New York: Wisdom Publications, 2024. | …
      Identifiers: LCCN 2024008414 | ISBN 9781614298939 (paperback) | ISBN … (ebook)
  • FREE-FORM (older LC / AACR2):
      Kongtrul, Jamgön, 1813-1899.
      [Uniform title. English]
      Creation & completion : essential points of tantric meditation / Jamgön … .
        p. cm.
      ISBN 0-86171-312-5 (alk. paper)
      … I. Title.  BQ… 294.3'4435—dc21  2001003915
  • ABBREVIATED:  "Library of Congress Cataloging-in-Publication Data is available."
      (+ LCCN / ISBN, no structured title)
  • BRITISH LIBRARY:  "A catalogue record … is available from the British Library."
  • CARD NUMBER (pre-ISBN):  "Library of Congress Catalog Card Number: 75-189390"

Everything is best-effort and any field may be None; ISBNs are checksum-validated
(reusing book_identifier.IsbnScheme), so a misread ISBN is dropped, not trusted.
parse_cip() returns None only when no CIP/BL marker is present at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .book_identifier import IsbnScheme
from .translit import classify_script, strip_language_subfield

# ── markers ────────────────────────────────────────────────────────────────────────
# "Cataloging in Publication" (allow British 'Cataloguing', OCR of the joining
# spaces/hyphens), or a bare "Library of Congress", or the British Library line.
_MARKER = re.compile(
    r"(?is)catalog\w{0,3}[\s\-]*in[\s\-]*publicat|library of congress|british library|"
    r"national library|donn[ée]es de catalogage")        # US/UK + AU/CA(fr) markers

# The UNAMBIGUOUS CIP marker: the actual "Catalog(u)ing-in-Publication" phrase. Unlike
# the bare-institution alternatives in _MARKER (which appear in prose — "the Library of
# Congress catalogs …", a "National Library of Nepal" manuscript source, a bibliographic
# "Library of Congress P.L. 480 program"), this phrase only occurs on the copyright page.
_STRONG_MARKER = re.compile(r"(?is)catalog\w{0,3}[\s\-]*in[\s\-]*publicat")

# What a genuine CIP window contains. Used to vet a BARE-institution marker so a prose
# mention of "Library of Congress" isn't parsed as a CIP block.
_CIP_EVIDENCE = re.compile(
    r"(?is)\b(?:Names|Identifiers?|Title|Tit[il1]e|Tide|Description|Subjects?)\s*[:.]"
    r"|\bISBN\b|\bLCCN\b|\bp\.?\s*[ce][mn]\b|\bis\s+available\b"
    r"|catalog(?:ue)?\s*card|publication\s+data")

# OCR character-equivalence sets (vertical bars, slashes, colons routinely degrade).
_PIPE = r"[|lI1!]"
_SOR = r"(?:/|\\|\s[Il]\s|\|)"                            # statement-of-resp separator


def _normalize_ocr(block: str) -> str:
    """Light, lossless-ish sanitation BEFORE field parsing, so individual patterns
    don't each have to absorb OCR noise: unify dash variants, and repair a field
    label whose trailing colon was misread (';'/'.'/'·' → ':'). ISBN digit/letter
    repair is left to IsbnScheme (checksum-gated)."""
    block = re.sub(r"[–—−]", "-", block)                 # unify dash variants
    block = re.sub(
        r"(?i)\b(Names|Title|Tit[il1]e|Tide|Description|Identifiers?|Subjects?|"
        r"Classification|Series)\s*[:;.,·•]",
        lambda m: m.group(1) + ":", block)
    return block
_BL = re.compile(r"(?is)british library")
_ABBREV = re.compile(r"(?is)\bis\s+available\b")
_CARD = re.compile(r"(?is)catalog(?:ue)?\s*card\s*(?:number|no)\.?[:\s]*([0-9][0-9\-\s]{4,12}[0-9])")

# Field labels for the modern labelled record. The value runs until the next known
# label or a blank line. 'Title' is allowed a couple of OCR variants.
_LABELS = (r"Names|Title|Tit[il1]e|Tide|Other\s+Titles?|Description|Identifiers?|"
           r"Subjects?|Classification|Series|Contents")


def _field(block: str, label: str) -> Optional[str]:
    pat = re.compile(
        rf"(?is)\b(?:{label})\s*[:.]\s*(.+?)(?=\s*(?:{_LABELS})\s*[:.]|\n\s*\n|\Z)")
    m = pat.search(block)
    return m.group(1).strip() if m else None


# ── helpers ──────────────────────────────────────────────────────────────────────────
def _clean(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    t = re.sub(r"\s+", " ", raw).strip(" .,;:·•/")
    t = re.sub(r"\s+([:;,])", r"\1", t)        # "Tantra : sub" → "Tantra: sub"
    return t or None


_CIP_BOILERPLATE = re.compile(
    r"(?i)catalog\w*.{0,3}in.{0,3}publicat|library of congress|national library|"
    r"publication data|british library|catalogue record|available from")


def _looks_titleish(t: Optional[str]) -> bool:
    # Strip leading quotes/brackets/punctuation before judging — don't reject a title
    # that opens with a quote/bracket, or with a lowercase OCR typo. No first-letter
    # case requirement (that threw away valid titles over a single mis-cased char).
    core = (t or "").lstrip("\"'([{ ·•-").strip()
    if not (4 <= len(core) <= 250 and sum(c.isalpha() for c in core) >= 4):
        return False
    # reject CIP boilerplate dragged in when OCR merges the header onto the title line.
    return not _CIP_BOILERPLATE.search(core)


def _strip_brackets(s: str) -> str:
    return re.sub(r"\[[^\]]*\]", " ", s)        # drop "[uniform title. English]"


# Bracket contents that are NOT a uniform title (general material designations, edition
# statements). Rejected so "[electronic resource]" / "[2nd ed.]" don't masquerade.
_NOT_UNIFORM = re.compile(
    r"(?i)^(?:sound recording|electronic resource|microform|videorecording|"
    r"book|map|\d+(?:st|nd|rd|th)?\s*(?:ed|edition|print)|rev\.|reprint)")


def _uniform_title(block: str):
    """Capture the MARC 240 uniform title — the romanized vernacular ORIGINAL — that
    `_strip_brackets`/`_freeform_title` discard. Two surface forms: an RDA labelled
    'Other titles:' / 'Uniform title(s):' field, or the older AACR2 bracket
    '[Romanized original. English. Selections]' sitting under the author main entry.
    Returns (core_title_as_printed, language|None, is_selection, script) or
    (None, None, False, None). Diacritics are PRESERVED for the downstream converter."""
    raw = _field(block, r"Other\s+Tit[il1]es?|Uniform\s+Tit[il1]es?")
    if not raw:
        # bracketed form: take the first bracket whose content is title-ish. Brackets
        # appear early (right after the name entry); scan a generous window.
        for m in re.finditer(r"\[\s*([^\]\n]{4,150}?)\s*\]", block[:1200]):
            cand = m.group(1).strip()
            if not _NOT_UNIFORM.match(cand) and sum(c.isalpha() for c in cand) >= 4:
                raw = cand
                break
    if not raw:
        # AACR2 linking note: "Translation of: <romanized original …>" — runs (often
        # across wrapped lines) until the subject tracing ("1. …"), ISBN, or a note. The
        # script gate below rejects English prose like "…translation of Nagarjuna's…".
        m = re.search(
            r"(?is)\btranslation\s+of\s*[:.]\s*(.+?)"
            r"(?=\n\s*\d{1,2}\.\s|\n\s*(?:ISBN|Includes|Bibliography)\b|\n[A-Z]{2}\d|\Z)",
            block)
        if m and classify_script(_clean(m.group(1)) or "") in ("tibetan", "sanskrit"):
            raw = m.group(1)
    if not raw:
        return None, None, False, None
    # Strip any statement-of-responsibility that rode along (esp. from a "Translation
    # of: <title> / by <name>" note) — a uniform title never contains " by …"/" / …".
    raw = re.split(r"\s+/\s+|\s+\\\s+|\s+(?:by|edited|translated|compiled)\b", raw, 1)[0]
    raw = _clean(raw)                                    # collapse ws; KEEPS diacritics
    core, lang, sel = strip_language_subfield(raw or "")
    if not core or sum(c.isalpha() for c in core) < 4:
        return None, None, False, None
    script = classify_script(core)
    # Only accept a genuine vernacular uniform title: it must carry a language subfield
    # (the '. English' marker) OR read as Tibetan/Sanskrit script. This rejects an
    # English parallel title or a stray bracket that slipped past _NOT_UNIFORM.
    if not (lang or script in ("tibetan", "sanskrit")):
        return None, None, False, None
    return core, lang, sel, script


def _strip_name(s: Optional[str]) -> Optional[str]:
    """Drop role words + lifespan dates from a name heading, keeping the romanized name
    (ALA-LC for Tibetan authors). Diacritics/hyphens preserved for the EWTS converter."""
    if not s:
        return None
    s = re.sub(r"\b(author|editor|translator|compiler)\b\.?", "", s, flags=re.I)
    s = re.sub(r",?\s*\d{3,4}\s*-\s*(?:\d{3,4})?\.?", "", s)        # dates
    s = _clean(s)
    return s if s and any(c.isalpha() for c in s) else None


def _author_heading(block: str, kind: str) -> Optional[str]:
    """The MAIN-ENTRY author name as printed — ALA-LC for Tibetan authors (e.g.
    'Tsoṅ-kha-pa Blo-bzaṅ-grags-pa'), dates/role stripped. This is the Wylie author
    ANCHOR for BDRC verification (the translator, who comes second, is irrelevant).
    Labelled: the first segment of 'Names:'. Free-form: the top line before its
    lifespan dates. None when not confidently found (→ no anchor, by design)."""
    if kind == "labelled":
        raw = _field(block, "Names")
        return _strip_name(re.split(r"\||;", raw)[0]) if raw else None
    b = block
    m = _MARKER.search(b)                              # drop the CIP header line
    if m:
        nl = b.find("\n", m.start())
        b = b[nl + 1:] if nl != -1 else b
    # the main entry sits at the top, terminated by its birth-death dates.
    m = re.search(r"^\s*([A-ZÀ-ɏ][^\n]{2,70}?),?\s*\d{3,4}\s*-\s*\d{0,4}", b)
    return _strip_name(m.group(1)) if m else None


def _author_dates(block: str) -> List[str]:
    """Birth–death lifespans from the name-heading region (e.g. '1357-1419', '1813-1899',
    or an open '1951-'). Dashes are already unified by `_normalize_ocr`. Used as the date
    anchor for BDRC person disambiguation. Scans the head, where the main entry sits."""
    head = block[:500]
    out, seen = [], set()
    # Lookbehind/ahead reject ISBN segments ('978-1-345-…' would otherwise read as
    # '978-' or '345-388'); plausibility (100–2025) drops any survivor that isn't a year.
    for m in re.finditer(r"(?<![\d.\-/])\b(\d{3,4})\s*-\s*(\d{3,4})?(?![\d\-/])", head):
        b, d = m.group(1), m.group(2)
        if not (100 <= int(b) <= 2025) or (d and not (100 <= int(d) <= 2025)):
            continue
        val = f"{b}-{d}" if d else f"{b}-"
        if val not in seen:
            seen.add(val)
            out.append(val)
    return out


# ── record ─────────────────────────────────────────────────────────────────────────
@dataclass
class CipRecord:
    kind: str                                  # labelled|freeform|abbreviated|british_library|card|none
    title: Optional[str] = None
    isbns: List[str] = field(default_factory=list)     # checksum-valid ISBN-13s
    lccn: Optional[str] = None
    year: Optional[int] = None
    authors: List[str] = field(default_factory=list)
    publisher: Optional[str] = None
    # MARC 240 uniform title — the work's romanized ORIGINAL title (ALA-LC), captured
    # AS PRINTED (diacritics preserved; never folded here). The deterministic
    # ALA-LC→EWTS conversion + BDRC verification happen downstream in wylie_resolve, not
    # in the parser. `uniform_script` flags whether it's Tibetan (→ Wylie) or Sanskrit
    # (already IAST), so the consumer knows whether to run the converter.
    uniform_title: Optional[str] = None
    uniform_lang: Optional[str] = None         # MARC 240 $l language of THIS edition
    uniform_selections: bool = False           # MARC 240 $k — a PARTIAL translation
    uniform_script: Optional[str] = None       # tibetan | sanskrit | unknown
    author_dates: List[str] = field(default_factory=list)   # name-heading lifespans
    author_heading: Optional[str] = None        # main-entry name as printed (ALA-LC)


# ── title extraction ───────────────────────────────────────────────────────────────
def _labelled_title(block: str) -> Optional[str]:
    raw = _field(block, r"Title|Tit[il1]e|Tide")
    if not raw:
        return None
    # cut at the statement-of-responsibility slash; flanking spaces optional (OCR
    # often drops one: " /" or "/ ") and the slash may be an OCR backslash.
    raw = re.split(r"\s*[/\\]\s*", raw, 1)[0]
    return _clean(raw)


def _freeform_title(block: str) -> Optional[str]:
    b = _strip_brackets(block)
    # Method 1 (survives OCR): AACR2 title proper carries a " : " subtitle separator
    # ("Creation & completion : essential points …"). Scan each line for it, ending
    # at the statement-of-responsibility marker — "/" OR its common OCR forms (" I ",
    # " l ", "|"), a responsibility word, or end of line. The " : " is distinctive,
    # so this works even when the "/" was OCR'd as "I".
    for line in b.splitlines():
        # pre-colon part has NO period, so the capture starts at the title proper,
        # not a preceding subject/uniform-title heading ("…Cakrasamvaratantra English.").
        m = re.search(
            r"([A-ZÀ-ɏ][^/|\n.]{2,90}?\s:\s[^/|\n]{2,150}?)"
            r"(?=\s*(?:/|\sI\s|\sl\s|\||\sby\s|\sedited\s|\stranslated\s)|\s*$)", line)
        if m:
            t = re.sub(r"\s+[Il/|]\s*$", "", m.group(1))   # drop a trailing OCR'd "/"
            t = _clean(t)
            if _looks_titleish(t):
                return t
    # Method 2: title ending at the FIRST " / " (clean records, no subtitle); trim the
    # preceding author main-entry (birth–death dates / year / sentence boundary).
    sm = re.search(r"\s/\s", b)
    region = b[:sm.start()] if sm else None
    if region is None:
        # no SOR slash — fall back to the title before the bibliographic "… cm"
        # ("cm" OCR-tolerant: c/e + m/n), still trimming the author prefix.
        cm = re.search(r"(?is)\b(?:p|pp|pages|v|vol)?\.?\s*[ce][mn]\b", b)
        region = b[:cm.start()] if cm else None   # None → fall through to Method 3
    if region is not None:
        region = region[-220:]                  # title sits just before the boundary
        cut = 0
        for mm in re.finditer(
                r"\d{3,4}\s*[-–—]\s*\d{3,4}[.·,]?|\d{4}[.·]|[.·•]\s|\n", region):
            cut = mm.end()                      # advance past the last author-ish boundary
        t = _clean(region[cut:])
        if _looks_titleish(t):
            return t
    # Method 3 ("sandwich"): no SOR slash, no " : ", no "cm" (very old records, e.g.
    # "<author, dates.>  Liberation in our hands.  Bibliography: p.  …"). The title is
    # the line(s) between the author main-entry (top) and the physical-description /
    # notes / subject-tracing / ISBN block (bottom).
    return _sandwich_title(b)


_BOTTOM = re.compile(
    r"(?i)^(?:\d+\s*)?(?:p\.|pp\.|pages\b|v\.\s|\d+\s*[ce][mn]\b|includes?\b|"
    r"bibliograph|isbn\b|translation of|library of|national library|other titles?\b|"
    r"\d+\.\s|[A-Z]{1,3}\d|.*\bd[ce]\d)")


def _sandwich_title(b: str) -> Optional[str]:
    lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
    if lines and _MARKER.search(lines[0]):
        lines = lines[1:]                       # drop the CIP header line
    out, started = [], False
    for i, ln in enumerate(lines):
        if not started:                         # skip the author main-entry
            if i == 0 or re.search(r"\b\d{4}\s*-\s*(?:\d{4})?", ln):
                continue
            started = True
        if _BOTTOM.match(ln):
            break
        out.append(ln)
        if len(" ".join(out)) > 200:
            break
    t = _clean(" ".join(out))
    return t if _looks_titleish(t) else None


# ── identifiers / dates / people ─────────────────────────────────────────────────────
def _isbns(block: str) -> List[str]:
    out, seen = [], set()
    for _pos, v in IsbnScheme().spans(block):
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _lccn(block: str) -> Optional[str]:
    # labelled "Identifiers: … LCCN 2024008414"; bare "LCCN 2017018045"; modern bare
    # 10-digit (20YYnnnnnn); or pre-ISBN card number "75-189390".
    m = re.search(r"(?is)\bLCCN\b[:\s]*([0-9][0-9\-\s/]{6,12}[0-9])", block)
    if not m:
        m = _CARD.search(block)
    if not m:
        m = re.search(r"\b((?:19|20)\d{8})\b", block)   # modern LCCN, no label
    if not m:
        return None
    v = re.sub(r"[\s/\-]", "", m.group(1))
    return v or None


def _year(block: str) -> Optional[int]:
    """Publication year, defended against chronological hijacking by author lifespans.
    Order: (1) a year next to a publication cue (©/copyright/published/Description/
    p. cm); (2) a modern LCCN's 4-digit prefix; (3) a plausible year that is NOT part
    of an author date-range and NOT an open lifespan ('1951–')."""
    YR = r"((?:19|20)\d{2})"
    for cue in (r"©", r"copyright", r"published", r"\bDescription\b",
                r"\bp\.?\s*[ce][mn]\b"):
        m = (re.search(cue + r"[^\n]{0,60}?" + YR, block, re.I)
             or re.search(YR + r"[^\n]{0,8}?" + cue, block, re.I))
        if m:
            return int(m.group(1))
    m = re.search(r"(?is)\bLCCN\b[:\s]*((?:19|20)\d{2})\d{5,7}", block)   # LCCN prefix
    if m:
        return int(m.group(1))
    ranges = [(mm.start(), mm.end())
              for mm in re.finditer(r"\d{4}\s*-\s*\d{4}", block)]         # author lifespans
    cand = []
    for mm in re.finditer(YR, block):
        if any(s <= mm.start() < e for s, e in ranges):
            continue                                  # inside a YYYY-YYYY range
        if re.match(r"\s*-\s*(?!\d)", block[mm.end():mm.end() + 4]):
            continue                                  # open lifespan "1951-"
        cand.append(int(mm.group(1)))
    return max(cand) if cand else None


def _authors(block: str, kind: str) -> List[str]:
    if kind == "labelled":
        raw = _field(block, "Names")
        if not raw:
            return []
        parts = re.split(r"\||;", raw)
        out = []
        for p in parts:
            p = re.sub(r"\b(author|editor|translator|compiler)\b\.?", "", p, flags=re.I)
            p = re.sub(r",?\s*\d{3,4}\s*[-–—]\s*(?:\d{3,4})?\.?", "", p)   # drop dates
            p = _clean(p)
            if p and any(c.isalpha() for c in p):
                out.append(p)
        return out
    return []                                   # free-form author parse is unreliable


def _publisher(block: str) -> Optional[str]:
    # modern Description: "First edition. | New York : Wisdom Publications, 2024. | …"
    # OCR-tolerant: the vertical bar may be l/I/1/!/slash; the comma before the year
    # may be missing.
    m = re.search(
        rf"(?is)\bDescription\b\s*[:.]\s*.*?(?:{_PIPE}|/)\s*[^:|]+?:\s*"
        rf"([^,|]+?)\s*,?\s*(?:19|20)\d{{2}}", block)
    return _clean(m.group(1)) if m else None


# ── top-level ────────────────────────────────────────────────────────────────────────
def _select_cip_block(t: str) -> Optional[str]:
    """The 1800-char window of the REAL CIP record, or None. We must not just take the
    first marker: in a scholarly book the bare-institution markers ("Library of
    Congress", "National Library of Nepal") appear in the introduction, bibliography,
    and manuscript-source lists LONG before the copyright-page CIP. So: prefer the
    unambiguous "Cataloging-in-Publication" phrase; otherwise accept a bare-marker
    window only when it actually carries CIP content (ISBN/LCCN/p. cm/field labels/…)."""
    m = _STRONG_MARKER.search(t)
    if m:
        return t[m.start(): m.start() + 1800]
    for mm in _MARKER.finditer(t):                           # weak markers — vet each
        win = t[mm.start(): mm.start() + 1800]
        if _CIP_EVIDENCE.search(win):
            return win
    return None


def parse_cip(text: str) -> Optional[CipRecord]:
    """Parse the CIP/British-Library block in `text` into a CipRecord, or None if no
    such block is present. Fields are best-effort and may be None/empty."""
    raw = _select_cip_block(text or "")
    if raw is None:
        return None
    block = _normalize_ocr(raw)                              # compact + OCR-sanitized

    if _BL.search(block[:80]) and not re.search(r"(?is)cataloging", block[:80]):
        # British Library "A catalogue record … is available" — no structured title.
        return CipRecord("british_library", isbns=_isbns(block), lccn=_lccn(block),
                         year=_year(block))

    card = _CARD.search(block)
    # Detect the modern labelled record by its 'Names:' / 'Identifiers:' fields —
    # NOT by 'Title', because free-form records carry a 'I. Title.' subject tracing
    # (and OCR 'Cover tide:') that would false-trigger.
    labelled = bool(re.search(r"(?is)\b(?:Names|Identifiers?)\s*[:.]", block))
    abbreviated = bool(_ABBREV.search(block[:120])) and not labelled

    if abbreviated:
        return CipRecord("abbreviated", isbns=_isbns(block), lccn=_lccn(block),
                         year=_year(block))
    uni, ulang, usel, uscript = _uniform_title(block)
    dates = _author_dates(block)
    if labelled:
        return CipRecord(
            "labelled", title=_labelled_title(block), isbns=_isbns(block),
            lccn=_lccn(block), year=_year(block), authors=_authors(block, "labelled"),
            publisher=_publisher(block), uniform_title=uni, uniform_lang=ulang,
            uniform_selections=usel, uniform_script=uscript, author_dates=dates,
            author_heading=_author_heading(block, "labelled"))
    if card and not _isbns(block):
        return CipRecord("card", lccn=_lccn(block), year=_year(block),
                         author_dates=dates)
    # default: older free-form record
    return CipRecord("freeform", title=_freeform_title(block), isbns=_isbns(block),
                     lccn=_lccn(block), year=_year(block), uniform_title=uni,
                     uniform_lang=ulang, uniform_selections=usel,
                     uniform_script=uscript, author_dates=dates,
                     author_heading=_author_heading(block, "freeform"))
