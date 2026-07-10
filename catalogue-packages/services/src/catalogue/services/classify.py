"""Per-entry, confidence-driven escalation ladder (§4.9, §13).

Ladder: `qwen3:8b → qwen3:14b → claude-haiku-4-5`, driven by a config-set
confidence threshold. The cache (`classification_cache`) is checked first;
settled entries never re-climb and never re-bill. Each rung's call is
wrapped — if a rung is unavailable (no API key, transport down), we record
it and advance, instead of failing the whole batch.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .llm import BudgetExceeded, LLMClient
from .toc import TOCEntry


# ── Classifier prompt ─────────────────────────────────────────────────────
# A *concrete* JSON example is load-bearing: given the literal template
# `{"kind":"...","confidence":0.0-1.0}` (placeholder `...` and the invalid
# value `0.0-1.0`), gemma emits empty/garbage content; a filled-in example it
# follows reliably. No `/no_think` — that was qwen3's reasoning-suppression
# directive (qwen3 was dropped, §4.9); to gemma/Haiku it is just noise.
_SYSTEM = (
    "You classify ONE table-of-contents entry from a Buddhist book into "
    "exactly one kind:\n"
    "- front_matter: editorial/introductory apparatus BEFORE the body — "
    "Title Page, Copyright, Dedication, Epigraph, Contents/Table of Contents, "
    "List of Abbreviations/Illustrations, Foreword, Preface, Acknowledgments, "
    "Introduction, Prologue, Translator's Introduction, Note on "
    "Transliteration/Pronunciation, Maps.\n"
    "- back_matter: apparatus AFTER the body — Conclusion, Epilogue, "
    "Afterword, Appendix, Notes/Endnotes, Glossary, Bibliography, References, "
    "Works Cited, Index, Colophon, About the Author, Further Reading.\n"
    "- root: an original/foundational/canonical text reproduced in the book "
    "(a 'root text').\n"
    "- commentary: a commentary on another (root) text.\n"
    "- subcommentary: a commentary on a commentary.\n"
    "- other: substantive content that is none of the above — e.g. a chapter "
    "or essay of a modern study/biography/history, or a structural divider "
    "like 'Part I'.\n"
    "\nRules (in priority order):\n"
    "- If the entry text itself NAMES a text type, classify by that name even "
    "when phrased as a part/section heading: contains 'root text' -> root, "
    "'commentary' -> commentary, 'subcommentary' -> subcommentary. So "
    "'Part 1 The Root Text' -> root and 'Part 2 The Commentary' -> commentary. "
    "A bare or topic-only divider with NO such word ('Part I', 'Section 2', "
    "'Part One: The Wheel-Weapon Mind Training') -> other.\n"
    "- Decide front/back matter by the entry's ROLE/label, not its topic.\n"
    "- A chapter of a MODERN scholarly book (history, biography, analysis) is "
    "'other', NOT front_matter — front_matter is ONLY the editorial apparatus "
    "listed above.\n"
    "- Otherwise use root/commentary/subcommentary only when the entry is, or "
    "names, an actual Buddhist text.\n"
    "\nRespond with ONLY a JSON object, no other text, exactly like:\n"
    '{"kind": "back_matter", "confidence": 0.9, "reasoning": "brief note"}\n'
    "kind must be exactly one of: root, commentary, subcommentary, "
    "front_matter, back_matter, other. confidence is a number 0.0-1.0 "
    "(1.0 = certain; below 0.7 = uncertain, a human will review)."
)


def _user_prompt(title: str, edition_title: Optional[str]) -> str:
    return (
        f"Edition: {edition_title or '(unknown)'}\n"
        f"TOC entry: {title}\n\n"
        "Classify this entry."
    )


# ── Ladder ────────────────────────────────────────────────────────────────
@dataclass
class Rung:
    name: str
    client: LLMClient
    available: Callable[[], bool] = field(default=lambda: True)


def default_ladder(*, budget=None, transport=None) -> list[Rung]:
    """Plan §4.9 ladder: local Ollama rung(s) first (free), Claude Haiku as the
    auth-gated cloud top rung.

    Local model(s) come from `CATALOGUE_LLM_MODELS` (comma-separated,
    cheap→strong) and default to `gemma3:12b`.

    Both qwen3 AND Gemma 4 were rejected: they are *reasoning* models whose
    hidden thinking channel cannot be disabled over Ollama's OpenAI-compat
    `/v1` endpoint (`/no_think`, `think:false`, and `chat_template_kwargs` are
    all ignored there). On any non-trivial entry they spend the whole
    `max_tokens` budget on reasoning and return empty `content` (verified:
    finish_reason='length', completion_tokens=256, content='').

    gemma3:12b (the last *non-reasoning* Gemma) and llama3.1:8b-instruct both
    work cleanly over `/v1` (finish_reason='stop', ~25–40 output tokens). gemma3
    is the default: on a Buddhist-title sample it was better calibrated
    (0.8–0.95 vs llama3.1 slamming 1.0, including on a misclassification) and
    showed stronger proper-noun knowledge (recognized English titles of
    canonical works). llama3.1:8b-instruct is the lighter fallback
    (`CATALOGUE_LLM_MODELS=llama3.1:8b-instruct-q4_K_M`). Backend stays a
    base_url/model swap (§4.9), overridable via `CATALOGUE_LLM_BASE_URL`."""
    import os
    kwargs = {}
    if budget is not None:
        kwargs["budget"] = budget
    if transport is not None:
        kwargs["transport"] = transport

    base_url = os.environ.get("CATALOGUE_LLM_BASE_URL", "http://localhost:11434/v1")
    local_models = [m.strip() for m in
                    os.environ.get("CATALOGUE_LLM_MODELS", "gemma3:12b").split(",")
                    if m.strip()]
    rungs = [Rung(m, LLMClient(model=m, base_url=base_url, **kwargs))
             for m in local_models]
    # Anthropic requires the dated ID at the API; `claude-haiku-4-5` without a
    # date returns 404. Bump in lockstep with §11's Haiku row when a new
    # revision ships.
    rungs.append(
        Rung("claude-haiku-4-5-20251001",
             LLMClient(model="claude-haiku-4-5-20251001",
                       base_url="https://api.anthropic.com/v1", **kwargs),
             available=lambda: bool(os.environ.get("ANTHROPIC_API_KEY"))))
    return rungs


# ── Cache + classify ──────────────────────────────────────────────────────
def _content_hash(title: str, edition_title: Optional[str]) -> str:
    h = hashlib.sha256()
    h.update((edition_title or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(title.encode("utf-8"))
    return h.hexdigest()


def _parse(content: str) -> tuple[dict, float]:
    """Parse the LLM's JSON. On any failure return a low-confidence stub
    so the caller advances the ladder rather than crashing."""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return ({"kind": "other", "reasoning": "unparsable"}, 0.0)
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return (data, max(0.0, min(1.0, conf)))


@dataclass
class ClassifyResult:
    kind: str
    confidence: float
    rung: str
    cached: bool
    raw: dict


def classify_entry(
    conn,
    title: str,
    *,
    edition_title: Optional[str] = None,
    classify_version: int = 1,
    threshold: float = 0.7,
    ladder: Optional[list[Rung]] = None,
) -> ClassifyResult:
    """Cache-first, then climb the ladder. Returns the first confident
    result; if no rung is confident, returns the last attempted result."""
    content_hash = _content_hash(title, edition_title)

    # §6: check cache before any rung — settled entries never re-climb.
    from catalogue.access_api import system_conn
    cached = system_conn(conn).classification_cache.get(content_hash, classify_version)
    if cached:
        raw = json.loads(cached[0])
        return ClassifyResult(
            kind=raw.get("kind", "other"),
            confidence=cached[1] or 0.0,
            rung=cached[2] or "cache",
            cached=True,
            raw=raw,
        )

    ladder = ladder if ladder is not None else default_ladder()
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user",   "content": _user_prompt(title, edition_title)},
    ]

    last_raw: dict = {"kind": "other", "reasoning": "no rungs ran"}
    last_conf = 0.0
    last_rung = "none"

    for rung in ladder:
        if not rung.available():
            continue       # e.g. no API key for Haiku → skip silently
        try:
            # The classifier answer is one small JSON object; cap output low
            # so generation stays fast and qwen3 can't ramble past the JSON.
            resp = rung.client.chat(messages, max_tokens=256)
        except BudgetExceeded:
            # Hard stop — surface to caller; do NOT cache.
            raise
        except Exception:
            # Network glitch, model unreachable — advance to next rung.
            continue
        raw, conf = _parse(resp["content"])
        last_raw, last_conf, last_rung = raw, conf, rung.name
        if conf >= threshold:
            break        # this rung wins; stop climbing

    # Cache the winner (or the last attempt if none reached threshold).
    system_conn(conn).classification_cache.put(
        content_hash, classify_version, json.dumps(last_raw), last_conf, last_rung)
    return ClassifyResult(
        kind=last_raw.get("kind", "other"),
        confidence=last_conf,
        rung=last_rung,
        cached=False,
        raw=last_raw,
    )


# ── Shared book-level helpers (§4.6) ──────────────────────────────────────
# Reused by the TOC-region parse below and by the section analysis in
# book_analysis.py: the front/back-matter filter, a ladder-runner that takes the
# first answering rung (no per-entry confidence loop), and a lenient JSON parse
# (gemma emits loose objects — commas/newlines without enclosing brackets, or
# wrapped in prose). Lesson from validation: locate tight, feed tight — a
# >20K-char dump derails the model into prose-summary.

_FRONT_BACK_WORDS = (
    "title page", "copyright", "dedication", "epigraph", "contents", "foreword",
    "preface", "acknowledg", "introduction", "prologue", "abbreviation",
    "index", "glossary", "bibliography", "references", "works cited", "notes",
    "appendix", "about the", "colophon", "further reading", "other titles",
    "also by", "cover", "maps", "e-mail", "translator's", "epilogue",
    # editorial apparatus (book 155: a critical edition's back matter that read as
    # verse under the bad pdf-textlayer locator and was being emitted as "works")
    "technical note", "emendation", "errata", "addenda", "concordance",
    "guide to the topics", "list of", "table of", "note on",
)


def _is_front_back(title: str) -> bool:
    t = (title or "").lower()
    return any(w in t for w in _FRONT_BACK_WORDS)


def _run_ladder(messages: list[dict], ladder: Optional[list[Rung]] = None,
                *, max_tokens: int = 400) -> str:
    """Climb the ladder, return the first available rung's content. Book-level
    calls don't use the per-entry confidence loop — the first model that
    answers wins. Returns '' if every rung fails."""
    ladder = ladder if ladder is not None else default_ladder()
    for rung in ladder:
        if not rung.available():
            continue
        try:
            return rung.client.chat(messages, max_tokens=max_tokens)["content"]
        except BudgetExceeded:
            raise
        except Exception:
            continue
    return ""


def _lenient_json(text: str):
    """Parse JSON, tolerating gemma's loose output (objects separated by commas
    or newlines without enclosing brackets, or wrapped in prose)."""
    try:
        return json.loads(text)
    except Exception:
        objs = []
        for m in re.findall(r"\{[^{}]*\}", text):
            try:
                objs.append(json.loads(m))
            except Exception:
                pass
        return objs


# ── Text-layer TOC parse (the located region → entries; §4.7 rung) ───────
_TOC_PARSE_SYS = (
    "This is the OCR'd Table-of-Contents region of a book (headings may be "
    "spaced like 'Co n ten ts'; an entry's number and title, or its title and "
    "page number, may land on separate lines). Output ONLY a JSON object with "
    "an 'entries' array holding EVERY entry, exactly like:\n"
    '{"entries": [{"title": "Introduction", "page": 7}, '
    '{"title": "Chapter 1", "page": 12}]}\n'
    "Merge a stray leading/standalone number into its title's entry; set page "
    "to null if absent; skip the word 'Contents' itself and pure page-number "
    "lines."
)


def parse_toc_region(region_text: str, *,
                     ladder: Optional[list[Rung]] = None) -> list[TOCEntry]:
    """LLM-parse a *located* TOC region (from toc.locate_toc_region) into
    TOCEntry[]. Feed only the tight region — never a large slice. The model is
    asked for an object wrapping an 'entries' array (json_object mode emits an
    object, not a bare array), but we accept a list / single object too."""
    data = _lenient_json(_run_ladder(
        [{"role": "system", "content": _TOC_PARSE_SYS},
         {"role": "user", "content": region_text}],
        ladder, max_tokens=1200))
    if isinstance(data, dict):
        items = (data.get("entries") or data.get("toc") or data.get("items")
                 or ([data] if data.get("title") else []))
    elif isinstance(data, list):
        items = data
    else:
        items = []
    entries: list[TOCEntry] = []
    for e in items:
        if isinstance(e, dict) and str(e.get("title", "")).strip():
            pg = e.get("page")
            entries.append(TOCEntry(
                title=str(e["title"]).strip(),
                page=pg if isinstance(pg, int) else None, level=1))
    return entries
