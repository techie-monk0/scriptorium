"""Dry-run detection for the works rebuild — single-work editions (Part B).

For each `single_work` edition this answers: is it a *translation of a classical
text* (→ link a classical work, with a canonical# + Skt/Tib/English titles) or an
*original English book* (→ author(s) on the edition, no work)? It WRITES NOTHING to
the canonical tables — results land in the read-only `work_detection` cache for the
/works/detect review report, where a human verifies before anything is applied.

Reuse, not reinvent: `sanskrit_title.extract_sanskrit_title` (IAST off the title),
the 84000 Toh index (`work_canonical_resolver.EightyFourThousandIndex.by_english`),
and `wylie_resolve.verify_from_cip` (Tibetan, via the parsed CIP block) — all behind
an injectable `classical` resolver so the pass is hermetically testable and the
network/Toh-snapshot calls only happen when you run it live.
"""
from __future__ import annotations

import json

from catalogue.db_store import nfc
from catalogue.services import sanskrit_title as skt
from catalogue.services.picker import authority_url


def _acc(db):
    """A system Access over this connection — engine-routed edition/holding reads + writes, the
    review queue, the detection cache (editions.writes.store_detection) + the gloss cache."""
    from catalogue.access_api import system_conn
    return system_conn(db)


# ── gather an edition's verification context ──────────────────────────────────

def recorded_contributors(db, eid):
    """Authors/translators already recorded for the edition — authors from its
    works (work_author) + book-level (edition_author); translators from
    edition_translator + per-work overrides. `[(person_id, name)]`."""
    authors, translators = _acc(db).editions.reads.contributor_persons(eid)
    return ([{"id": pid, "name": name} for pid, name in authors],
            [{"id": pid, "name": name} for pid, name in translators])


def build_proposal_index(db):
    """`{holding_id: {"authors": [...], "translators": [...]}}` — the contributor
    names the ingest pass read off each book's title page/TOC (the `book_toc_pattern`
    proposal). The 'detected (from book)' column, independent of the saved persons so
    drift shows. Build once, pass to detect_single, to avoid re-scanning per edition."""
    idx = {}
    for pj in _acc(db).review.reads.payloads_by_type("book_toc_pattern"):
        try:
            p = json.loads(pj)
        except (TypeError, ValueError):
            continue
        hid = p.get("holding_id")
        if hid is None:
            continue
        authors = [nfc(a).strip() for a in (p.get("book_authors") or []) if a and a.strip()]
        for w in p.get("works") or []:
            for a in w.get("authors") or []:
                a = nfc(a or "").strip()
                if a and a not in authors:
                    authors.append(a)
        translators = [nfc(t).strip() for t in (p.get("book_translators") or []) if t and t.strip()]
        idx[hid] = {"authors": authors, "translators": translators}   # last proposal wins
    return idx


def _detected_contributors(db, eid, proposal_index):
    """Names the ingest detection read for this edition (across its holdings)."""
    authors, translators = [], []
    for h in _acc(db).holdings.reads.by_edition(eid):
        hit = (proposal_index or {}).get(h.id)
        if not hit:
            continue
        for a in hit["authors"]:
            if a not in authors:
                authors.append(a)
        for t in hit["translators"]:
            if t not in translators:
                translators.append(t)
    return authors, translators


def gather_edition(db, eid, *, proposal_index=None):
    """The verification context for one edition: stored title/isbn, the file, the
    recorded contributors, the ingest-detected contributors, and the cached
    title-page text."""
    acc = _acc(db)
    e = acc.editions.reads.get(eid)
    if not e:
        return None
    h = acc.holdings.reads.primary_file(eid)
    authors, translators = recorded_contributors(db, eid)
    det_authors, det_translators = _detected_contributors(db, eid, proposal_index)
    return {
        "edition_id": e.id, "title": e.title, "isbn": e.isbn,
        "structure": acc.editions.reads.structure_of(eid),
        "holding_id": h[0] if h else None,
        "file_path": h[1] if h else None,
        "raw_text": acc.editions.reads.raw_text_for_hash(h[2] if h else None),
        "recorded_authors": authors, "recorded_translators": translators,
        "detected_authors": det_authors, "detected_translators": det_translators,
    }


# ── classical-identity resolution (injectable) ────────────────────────────────

def canonical_links(system, number, *, native=None, english=None):
    """`(url_84000, url_bdrc)` for a canonical id. The 84000 page is the English
    reading-room translation — the way to verify a match without reading Tibetan/
    Sanskrit. For a Toh id we have no direct BDRC work id, so the BDRC link is a
    title search; a `bdr:W…` id links straight to its BDRC page."""
    url_84000 = authority_url(f"toh:{number}") if (system == "toh" and number) else None
    if number and str(number).startswith("bdr:"):
        url_bdrc = authority_url(number)                 # bdr:W… → purl.bdrc.io/resource/W…
    else:
        from urllib.parse import quote
        q = (native or english or "").strip()
        url_bdrc = f"https://library.bdrc.io/search?q={quote(q)}" if q else None
    return url_84000, url_bdrc


_GLOSS_SYS = (
    "You translate Buddhist text TITLES. Given a work's title in {lang}, give a SHORT, "
    "literal English rendering of the title only — not a description, not commentary. "
    "Output only the English title, nothing else.")


def cached_gloss(db, text, *, lang, model_label, client):
    """`gloss_title` with a persistent cache (gloss_cache) keyed by the folded title +
    model, so the LLM is called once per (title, model) — re-runs and other editions
    of the same text reuse it. Returns the gloss (cached or fresh), or None."""
    if not text:
        return None
    from catalogue.services.work_canonical_resolver import _native_key
    key = _native_key(text) or text.strip().lower()
    # Ignore any NULL-gloss rows (a past failure) so a now-working credential retries.
    cached = _acc(db).gloss_cache.reads.get(key, model_label)
    if cached is not None:
        return cached
    g = gloss_title(text, lang=lang, client=client)
    # Cache only a SUCCESSFUL gloss — a transient failure (no credit / network /
    # Ollama down) returns None and must be retried next run, not fossilised.
    if g:
        _acc(db).gloss_cache.writes.put(key, model_label, g)
        db.commit()                          # persist immediately — LLM calls are expensive
    return g


def gloss_title(text, *, lang, client):
    """A rough English gloss of a Tibetan-Wylie / Sanskrit title via an LLM, for
    titles with no authority English (so a non-script-reader can still recognise
    the work against the book's own English title). Approximate by nature — labelled
    as such in the report. Returns None on any failure."""
    if not text or client is None:
        return None
    try:
        resp = client.chat(
            [{"role": "system", "content": _GLOSS_SYS.format(lang=lang)},
             {"role": "user", "content": f"{lang} title: {text}"}],
            max_tokens=60, json_only=False)
        g = (resp.get("content") or "").strip().strip('"').strip()
        return g or None
    except Exception:
        return None


def live_classical(*, toh_index=None, bdrc_work_search=None):
    """Default resolver composing the real detectors. `toh_index` =
    EightyFourThousandIndex (by_english / by_sanskrit); `bdrc_work_search` = a
    `BdrcWorkSearch().work_search`-shaped fn (Sanskrit IAST + Tibetan Wylie). Both
    optional so the resolver degrades gracefully (title-only signals) when the 84000
    snapshot or the network is absent."""
    from catalogue.services.work_canonical_resolver import EightyFourThousandIndex
    from catalogue.services import wylie_resolve, sanskrit_resolve, cip as cip_mod
    idx = toh_index if toh_index is not None else EightyFourThousandIndex()
    search = bdrc_work_search

    def resolve(ctx):
        title = ctx["title"] or ""
        sanskrit = [t for t, _src in skt.extract_sanskrit_title(title)]
        author = (ctx.get("recorded_authors") or [{}])[0].get("name") if ctx.get("recorded_authors") else None
        # `english` stays the BOOK title; `authority_en` is what the authority calls
        # it (the English you verify the match against).
        out = {"english": title or None, "authority_en": None,
               "sanskrit": sanskrit[0] if sanskrit else None,
               "tibetan": None, "system": None, "number": None,
               "confidence": 0.0, "source": None}

        # 1. Toh by English title (the index already holds en/bo/sa per Toh#).
        try:
            hit = idx.by_english(title) if title else None
        except Exception:
            hit = None
        if hit:
            out.update(system="toh", number=str(hit.get("toh") or ""),
                       authority_en=hit.get("english"),
                       sanskrit=hit.get("sanskrit") or out["sanskrit"],
                       tibetan=hit.get("tibetan") or out["tibetan"],
                       confidence=0.9, source="84000-by-english")
            return out

        # 2. Sanskrit: Toh by_sanskrit → BDRC IAST (sanskrit_resolve).
        for cand in sanskrit:
            try:
                v = sanskrit_resolve.verify_sanskrit(
                    cand, author=author, toh_index=idx, bdrc_search=search)
            except Exception:
                v = {"matched": False}
            if v.get("matched"):
                out.update(system=v["system"], number=v["number"],
                           authority_en=v.get("english"),
                           sanskrit=v.get("sanskrit") or cand,
                           tibetan=v.get("tibetan") or out["tibetan"],
                           confidence=v["confidence"], source=v["reason"])
                return out

        # 3. Tibetan: parse a CIP uniform title off the page text → Wylie (EWTS).
        raw = ctx.get("raw_text")
        if raw:
            try:
                rec = cip_mod.parse_cip(raw)
                uni = getattr(rec, "uniform_title", None)
                script = getattr(rec, "uniform_script", None) or "tibetan"
                if uni and script == "tibetan":
                    from catalogue.services.translit import to_ewts
                    ewts = to_ewts(uni, ocr=True)
                    # 3a. 84000 by Tibetan title — gives a verifiable ENGLISH title
                    #     (so a Tibetan match isn't a dead end for a non-Wylie reader).
                    thit = idx.by_tibetan(ewts) if ewts else None
                    if thit:
                        out.update(system="toh", number=str(thit.get("toh") or ""),
                                   authority_en=thit.get("english"),
                                   sanskrit=thit.get("sanskrit") or out["sanskrit"],
                                   tibetan=thit.get("tibetan") or ewts,
                                   confidence=0.9, source="84000-by-tibetan")
                        return out
                    # 3b. BDRC Wylie match (no 84000 English; use the BDRC page link).
                    if search is not None:
                        v = wylie_resolve.verify_from_cip(uni, script=script, search=search)
                        if getattr(v, "matched", False):
                            out.update(system="bdrc", number=v.bdrc_id,
                                       tibetan=v.ewts_query or ewts or out["tibetan"],
                                       confidence=v.confidence, source=f"cip-wylie: {v.reason}")
                            return out
            except Exception:
                pass

        # 4. No canonical match — title-only signal (Sanskrit if structurally present).
        out["confidence"] = 0.4 if out["sanskrit"] else 0.0
        return out

    return resolve


def detect_single(db, eid, *, classical=None, proposal_index=None, glossers=None):
    """Detect one single-work edition. Returns the result dict stored in
    `work_detection` (kind='single'). `classical(ctx) -> {...}` is injectable;
    default = `live_classical()`. `proposal_index` (build_proposal_index) feeds the
    'detected from book' contributor column. `glossers` = `{label: fn(text, lang)}`
    (e.g. gemma + Claude) — each produces a rough English gloss of the native title
    when the authority gives no English, shown side by side for comparison."""
    ctx = gather_edition(db, eid, proposal_index=proposal_index)
    if ctx is None:
        return None
    resolve = classical if classical is not None else live_classical()
    cl = resolve(ctx) or {}
    has_canonical = bool(cl.get("system") and cl.get("number"))
    has_native = bool(cl.get("sanskrit") or cl.get("tibetan"))
    determination = "classical" if (has_canonical or has_native) else "modern"
    u84000, ubdrc = canonical_links(
        cl.get("system"), cl.get("number"),
        native=cl.get("tibetan") or cl.get("sanskrit"),
        english=cl.get("authority_en") or ctx["title"])
    # Rough glosses only when there's a native title but NO authority English to
    # verify by — each configured model (gemma, Claude) renders it, side by side.
    glosses = {}
    if glossers and determination == "classical" and not cl.get("authority_en"):
        native, lang = ((cl.get("tibetan"), "Tibetan Wylie") if cl.get("tibetan")
                        else (cl.get("sanskrit"), "Sanskrit IAST") if cl.get("sanskrit")
                        else (None, None))
        if native:
            for label, fn in glossers.items():
                g = fn(native, lang)
                if g:
                    glosses[label] = g
    return {
        "determination": determination,        # 'classical' (link a work) | 'modern' (edition author, no work)
        "title": {"english": ctx["title"],     # the BOOK's title (what you know)
                  "sanskrit": cl.get("sanskrit"), "tibetan": cl.get("tibetan")},
        "canonical": {"system": cl.get("system"), "number": cl.get("number"),
                      "title_en": cl.get("authority_en"),   # the authority's English — verify against this
                      "glosses": glosses or None,            # {model: rough gloss} when no authority English
                      "url_84000": u84000, "url_bdrc": ubdrc},
        "confidence": cl.get("confidence", 0.0), "source": cl.get("source"),
        "authors_recorded": ctx["recorded_authors"],
        "translators_recorded": ctx["recorded_translators"],
        "authors_detected": ctx["detected_authors"],
        "translators_detected": ctx["detected_translators"],
        "stored_title": ctx["title"], "isbn": ctx["isbn"],
        "isbn_url": (f"https://books.google.com/books?vid=ISBN{ctx['isbn']}"
                     if ctx["isbn"] else None),
        "file": {"holding_id": ctx["holding_id"], "path": ctx["file_path"]},
    }


# ── cache store / read ────────────────────────────────────────────────────────

def store_detection(db, eid, kind, payload, *, commit=True):
    _acc(db).editions.writes.store_detection(eid, kind, json.dumps(payload))
    if commit:
        db.commit()


def get_detection(db, eid):
    r = _acc(db).editions.reads.detection(eid)
    return {"kind": r[0], **json.loads(r[1])} if r else None
