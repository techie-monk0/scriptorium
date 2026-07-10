"""Multi-work segmentation (dry-run, Part C) — split a marked multi_work edition
into its contained works, three ways so you can compare:

  • **deterministic** — `analyze_book_sections` (nav-depth top-level entries, fold
    descendants in, parse title→author). No LLM, no network.
  • **local LLM** (gemma3:12b) and **Haiku** — a NEW whole-list grouping pass
    (`llm_segment`, the missing G3): give the model the book's section/TOC title list
    and it returns the distinct works + authors. Run with each backend separately so
    you can judge how the local model does vs Haiku.

Each detected work is optionally run through the Part-B classical resolver for a
canonical# + Skt/Tib titles. WRITES NOTHING canonical — results land in the
`work_detection` cache (kind='multi') for /works/detect.

Reuse, not reinvent: `locator.extract_sections`, `analyze_book_sections`,
`classify._is_front_back` / `book_analysis._is_apparatus_title`, `llm.LLMClient`,
and `work_detect.live_classical` (per-work canonical).
"""
from __future__ import annotations

import json
from pathlib import Path

from catalogue.db_store import nfc
from catalogue.services.picker import authority_url


# ── sections ──────────────────────────────────────────────────────────────────

def get_sections(db, eid):
    """The edition's located sections (from its holding file). [] if none/unreadable."""
    from catalogue.access_api import system_conn
    fp = system_conn(db).editions.reads.first_file_path(eid)
    if not fp:
        return []
    from catalogue.services.locator import extract_sections
    try:
        secs = extract_sections(Path(fp))
    except Exception:
        secs = None
    return secs or []


def clean_titles(sections):
    """Section titles minus front/back-matter and apparatus, preserving order."""
    from catalogue.services.classify import _is_front_back
    from catalogue.services.book_analysis import _is_apparatus_title
    out = []
    for s in sections:
        t = (getattr(s, "title", None) or "").strip()
        if t and not _is_front_back(t) and not _is_apparatus_title(t):
            out.append(t)
    return out


# ── deterministic baseline ────────────────────────────────────────────────────

def deterministic_segment(sections, *, book_title=None):
    """The contained works the deterministic segmenter finds (nav-depth grouping)."""
    from catalogue.services.book_analysis import analyze_book_sections
    ba = analyze_book_sections(sections, edition_title=book_title,
                               enable_verse_gate=False, toc_hierarchy=True)
    works = []
    for ct in ba.contained_texts:
        works.append({"title": ct.title, "authors": list(ct.authors or []),
                      "translators": list(ct.translators or []),
                      "kind": ct.kind, "locator": ct.locator})
    return {"structure": ba.structure, "works": works, "n_sections": ba.n_sections}


# ── LLM whole-list grouping (the new G3 pass) ─────────────────────────────────

_SYS = (
    "You are cataloguing a Buddhist book that contains one or more distinct texts "
    "(\"works\") — e.g. root texts, commentaries, or an anthology of translated texts. "
    "Given the book's table-of-contents / section title list, identify the DISTINCT "
    "contained works. Merge a work's own sub-chapters into that one work; drop front/back "
    "matter (preface, glossary, index, about the author). For each work give its title and, "
    "if the title or list names one, its author (a person, often a Sanskrit/Tibetan name). "
    "Return ONLY JSON: {\"works\":[{\"title\":\"...\",\"author\":\"... or null\"}]}."
)


def _llm_messages(titles, *, book_title, book_authors):
    ctx = f"Book title: {book_title or '(unknown)'}\n"
    if book_authors:
        ctx += f"Book-level author(s): {', '.join(book_authors)}\n"
    ctx += "Section titles (in order):\n" + "\n".join(f"- {t}" for t in titles)
    return [{"role": "system", "content": _SYS}, {"role": "user", "content": ctx}]


def llm_segment(titles, *, book_title=None, book_authors=(), client=None):
    """Run the grouping pass with one LLM client. Returns
    `{"works":[{title, author}], "model":..., "ok":bool, "error":?}` — never raises."""
    if client is None or not titles:
        return {"works": [], "model": None, "ok": False, "error": "no client / no titles"}
    try:
        resp = client.chat(_llm_messages(titles, book_title=book_title,
                                         book_authors=book_authors),
                           max_tokens=1024, json_only=True)
        data = json.loads(resp.get("content") or "{}")
        works = []
        for w in (data.get("works") or [])[:200]:
            title = nfc((w.get("title") or "")).strip()
            if not title:
                continue
            author = nfc((w.get("author") or "")).strip() or None
            works.append({"title": title, "authors": [author] if author else []})
        return {"works": works, "model": resp.get("model"), "ok": True,
                "tokens_out": resp.get("tokens_out")}
    except Exception as exc:
        return {"works": [], "model": getattr(client, "model", None), "ok": False,
                "error": str(exc)}


# ── per-work canonical (optional, reuses the Part-B classical resolver) ────────

def _annotate_canonical(works, classical, glossers=None):
    """Run each contained work's title through the classical resolver for a
    canonical# + authority English + Skt/Tib titles + 84000/BDRC links, and (like
    single-work) a rough English gloss of the native title when there's no authority
    English. `classical(ctx)->{...}` is the work_detect resolver; `glossers` =
    {label: fn(text,lang)}. None skips."""
    if classical is None:
        return works
    from catalogue.services.work_detect import canonical_links
    for w in works:
        try:
            cl = classical({"title": w["title"], "recorded_authors": [], "raw_text": None})
        except Exception:
            cl = {}
        sys_, num = cl.get("system"), cl.get("number")
        u84000, ubdrc = canonical_links(sys_, num, native=cl.get("tibetan") or cl.get("sanskrit"),
                                        english=cl.get("authority_en") or w["title"])
        glosses = {}
        if glossers and not cl.get("authority_en"):
            native, lang = ((cl.get("tibetan"), "Tibetan Wylie") if cl.get("tibetan")
                            else (cl.get("sanskrit"), "Sanskrit IAST") if cl.get("sanskrit")
                            else (None, None))
            if native:
                for label, fn in glossers.items():
                    try:
                        g = fn(native, lang)
                    except Exception:
                        g = None
                    if g:
                        glosses[label] = g
        w["canonical"] = {"system": sys_, "number": num, "title_en": cl.get("authority_en"),
                          "glosses": glosses or None, "url_84000": u84000, "url_bdrc": ubdrc}
        w["title_sanskrit"] = cl.get("sanskrit")
        w["title_tibetan"] = cl.get("tibetan")
    return works


# ── orchestration ─────────────────────────────────────────────────────────────

def segment_edition(db, eid, *, sections=None, clients=None, classical=None, glossers=None):
    """Segment one multi_work edition every available way. `clients` =
    `{label: LLMClient}` (any subset; omit/None to skip). `classical` = the
    work_detect resolver for per-work canonical#; `glossers` = {label: fn} for the
    per-work English gloss. Returns the payload stored in work_detection
    (kind='multi'). Injectable for hermetic tests."""
    from catalogue.access_api import system_conn
    e = system_conn(db).editions.reads.get(eid)
    book_title = e.title if e else None
    isbn = e.isbn if e else None
    secs = get_sections(db, eid) if sections is None else sections
    titles = clean_titles(secs)

    det = deterministic_segment(secs, book_title=book_title)
    det["works"] = _annotate_canonical(det["works"], classical, glossers)
    methods = {"deterministic": det}

    for name, client in (clients or {}).items():
        r = llm_segment(titles, book_title=book_title, client=client)
        r["works"] = _annotate_canonical(r["works"], classical, glossers)
        methods[name] = r

    f = system_conn(db).holdings.reads.primary_file(eid)
    return {
        "stored_title": book_title, "isbn": isbn, "n_sections": len(secs),
        "n_titles": len(titles), "methods": methods,
        "file": {"holding_id": f[0] if f else None, "path": f[1] if f else None},
    }
