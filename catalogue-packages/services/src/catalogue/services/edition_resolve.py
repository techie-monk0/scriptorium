"""edition_resolve — identifier-first edition metadata resolution.

For each book, in order of authority:
  1. Pull its identifiers (ISBN, LCCN, …) with `BookIdentifier` from the title
     string + the cached copyright-page text (no rescan).
  2. Resolve them to an authoritative bibliographic record (OpenLibrary / Google
     Books) — scheme-agnostically; the pass never branches on ISBN-vs-LCCN.
  3. Apply the FULL record — title + authors + translators + publisher + year +
     the stored identifier — and queue an `edition_metadata` review item
     (revertable). A pending LLM `title_proposal` for the same book is superseded.
  4. Books with NO identifier, or whose identifier doesn't resolve, fall through
     to the LLM title pass (catalogue/work_titles), which reads the title off the
     page.

Authoritative hits are high-confidence: applied now + queued for review (an ISBN
can point to the wrong printing, so it stays revertable). Mirrors the other passes'
`(db, item_id, *, commit=True) -> bool` accept/reject contract + a `main()` CLI.
The authority `sources` and the LLM `ladder` are injectable so the whole pass runs
offline in tests.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from . import cip as cip_mod
from . import work_titles
from .book_identifier import BookIdentifier, Identifier
from catalogue.db_store import add_alias, fold_key, init_db


def _acc(db):
    """A system Access over this connection — engine-routed edition/work reads + writes + the
    review queue + the cached-extract read. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)


_TITLE_STOP_WORDS = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "with",
                     "by", "de", "la", "le", "el", "a'i", "pa"}


def _sig_words(t: str) -> set:
    """Significant fold-keyed word set of a title (drop short/stop words) — for
    fuzzy title agreement, diacritic/spelling-insensitive."""
    return {w for w in (fold_key(x) for x in re.findall(r"[A-Za-z0-9]+", t or ""))
            if w and len(w) >= 3 and w not in _TITLE_STOP_WORDS}


def _titles_agree(a: str, b: str) -> bool:
    """True if two titles plausibly describe the same work — they share ≥40% of the
    SMALLER title's significant words (tolerant of subtitle presence/absence and
    spelling). Empty/uncomparable → True (can't contradict). This is the
    title-confirmation gate: an ISBN's looked-up title must agree with the book's own
    printed (CIP/page) title, else the ISBN resolved to the wrong book."""
    wa, wb = _sig_words(a), _sig_words(b)
    if not wa or not wb:
        return True
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.4

# Lowercased in title-case unless first word (after a clean source title from an
# authority that may be sentence-cased, e.g. "The Dalai Lamas on tantra").
_TITLE_STOP = {"a", "an", "the", "and", "or", "nor", "but", "of", "on", "in", "to",
               "for", "with", "at", "by", "from", "as", "into", "over", "vs"}


def _titlecase(title: str) -> str:
    """Normalize a title to title case — fixes OpenLibrary's sentence-case ('Deity,
    mantra, and wisdom') AND all-caps page titles ('BUDDHIST ETHICS'). Keeps
    deliberate mixed-case (McDonald, iOS) and SHORT all-caps acronyms (IBM, HHDL);
    stop-words are lowercased except in first position."""
    words = (title or "").split()
    out = []
    for i, w in enumerate(words):
        letters = [c for c in w if c.isalpha()]
        all_caps = bool(letters) and all(c.isupper() for c in letters)
        # keep mixed-case words (McDonald) and short all-caps acronyms (≤4 letters);
        # a long all-caps word (BUDDHIST) is normalized like any ordinary word.
        if (not all_caps and any(c.isupper() for c in w[1:])) \
                or (all_caps and len(letters) <= 4):
            out.append(w)
            continue
        lw = w.lower()
        # first word of the title OR of a subtitle (prev word ended ':'/'—'/'–') is
        # always capitalized, even if it's a stop-word ("…Path: A Guide…").
        starts = (i == 0) or (out and out[-1][-1:] in (":", "—", "–"))
        out.append(lw if (not starts and lw in _TITLE_STOP) else lw[:1].upper() + lw[1:])
    return " ".join(out)


# Identifiers (ISBN/LCCN) are read ONLY from the OCR'd in-book text — never the
# filename (hard requirement). We scan the WHOLE cached text, because EPUB extraction
# isn't always in reading order: the copyright/CIP page (with the ISBN) can land at
# the head, the very end, OR the middle (~35% in). When several ISBNs appear, prefer
# the one nearest a copyright/CIP marker (the book's own) over "other volumes in this
# series" lists. (A full regex scan is microseconds — negligible beside the per-book
# network lookup + LLM call the pass already does.)
_CIP_MARKERS = re.compile(
    r"(?i)catalog(?:u)?ing.?in.?publication|library of congress|copyright\s|"
    r"all rights reserved")        # NOT "ISBN" — every ISBN abuts it, so it can't
                                   # distinguish the book's own from a series list


# CIP title/ISBN extraction now lives in catalogue/cip.py (structured, OCR-tolerant);
# resolve_edition calls cip_mod.parse_cip(text).


def _full_text(db, edition_id: int) -> str:
    """The whole OCR'd text for a book (for identifier scanning). NEVER the filename."""
    return _acc(db).editions.reads.cached_extract_text(edition_id) or ""


def _book_identifier(db, edition_id: int, bi):
    """The book's own identifier (ISBN/LCCN) from its OCR'd text, CIP-aware. None
    when no identifier is printed."""
    return bi.find_in_text(_full_text(db, edition_id), markers=_CIP_MARKERS)


def _proposal_pending(db, edition_id: int) -> bool:
    return _acc(db).review.reads.exists_pending(
        "edition_metadata", f'%"edition_id": {edition_id}%')


_VOLUME_RE = re.compile(
    r"(?i)\b(?:volume|vol|book|part|tome)\b\.?\s*"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|[ivxlc]+)\b")


def _has_structure(title: str) -> bool:
    """True if the title carries a SUBTITLE (':'/'—') or a VOLUME/PART marker — the
    signal that distinguishes a genuinely richer title from a bare over-read. Used so
    the LLM page title (or a free-form CIP fragment) beats the clean ISBN title only
    when it really adds something; a longer-but-structureless run does not."""
    t = title or ""
    return any(sep in t for sep in (":", "—", "–")) or bool(_VOLUME_RE.search(t))


def _page_title(db, edition_id: int, ladder) -> Optional[str]:
    """The title inferred from the OCR'd title page via the LLM (page text ONLY,
    mojibake-guarded). None if no usable/clean text or no title."""
    fm = work_titles._title_page_text(db, edition_id)
    if not fm or work_titles.looks_mojibake(fm):
        return None
    sug = work_titles.suggest_title(fm, ladder=ladder)
    if not sug or not sug.title or work_titles.looks_mojibake(sug.title):
        return None
    return sug.title


def _build_payload(db, edition_id: int, *, resolution, identifier, page_title,
                   id_title, final_title, title_source, cip_title=None) -> dict:
    """Build the edition_metadata payload. `final_title` is the chosen title (CIP >
    fuller of ISBN/page) — title-cased for storage; the candidates are kept for review
    transparency. ISBN/publisher/year come from the authority record (if any); the
    `identifier` is passed in (not derived from `resolution`) so the ISBN is stored
    even when the lookup MISSED but a CIP/page title won."""
    rec = resolution.record if resolution else None
    ident = identifier
    wid = work_titles._edition_work(db, edition_id)
    prim = work_titles._work_primary_alias(db, wid) if wid else None
    _ed = _acc(db).editions.reads.get(edition_id)
    cur = (_ed.title, _ed.isbn, _ed.publisher, _ed.year) if _ed else (None, None, None, None)
    isbn = (rec.isbn if rec and rec.isbn else
            (ident.value if ident and ident.scheme == "isbn" else None))
    return {
        "edition_id": edition_id, "work_id": wid,
        "identifier": str(ident) if ident else None,
        "id_scheme": ident.scheme if ident else None,
        "id_value": ident.value if ident else None,
        "found_in": ident.found_in if ident else None,
        "authority": getattr(rec, "source", None) if rec else None,
        # current values (for revert)
        "old_title": cur[0], "old_isbn": cur[1],
        "old_publisher": cur[2], "old_year": cur[3],
        "old_work_title": prim[1] if prim else None,
        "primary_alias_id": prim[0] if prim else None,
        # the two title candidates + which was chosen (page wins for generic-authority
        # multi-volume sets; the fuller ISBN title wins for clean single books).
        "id_title": id_title, "page_title": page_title, "cip_title": cip_title,
        "title_source": title_source,
        "new_title": _titlecase((final_title or "").strip()), "isbn": isbn,
        "publisher": (rec.publisher if rec else None),
        "year": (rec.year if rec else None),
        # authors/translators shown for review only — NOT written (role-aware passes
        # own contributors; a bare ISBN record can't tell author from translator).
        "authors": list(rec.authors) if rec else [],
        "translators": list(rec.translators) if rec else [],
        "applied": False, "created_alias_ids": [],
    }


def _apply_record(db, p: dict) -> None:
    """Write the authoritative EDITION fields (title/isbn/publisher/year) + the work
    title; mutate `p` to record what changed so a reject can revert. Old title kept
    as a 'filename' alias. Contributors are deliberately NOT written here — a bare
    ISBN record can't distinguish author from translator, and writing an author would
    block work_authority (which assigns correct roles). Supersedes any pending LLM
    title_proposal for this book."""
    eid, wid = p["edition_id"], p.get("work_id")
    acc = _acc(db)
    cur = acc.editions.reads.get(eid)
    cols = {"title": p["new_title"]}      # title overwritten; isbn/publisher/year fill-if-empty
    for f in ("isbn", "publisher", "year"):
        v = p.get(f)
        cols[f] = v if v is not None else (getattr(cur, f) if cur else None)
    acc.editions.writes.set_columns(eid, cols)

    created_aliases: list = []
    if wid and p.get("primary_alias_id") and p["new_title"] \
            and p["new_title"] != p.get("old_work_title"):
        acc.works.writes.update_alias(p["primary_alias_id"], p["new_title"])
        if p.get("old_work_title"):
            created_aliases.append(add_alias(db, "work", wid, p["old_work_title"],
                                             "filename"))

    p["applied"] = True
    p["created_alias_ids"] = created_aliases
    # An authoritative record beats an LLM guess: close any pending title proposal.
    acc.review.writes.resolve_pending_of_type("title_proposal", f'%"edition_id": {eid}%')


def _revert_record(db, p: dict) -> None:
    eid, wid = p["edition_id"], p.get("work_id")
    acc = _acc(db)
    acc.editions.writes.set_columns(eid, {
        "title": p.get("old_title") or "", "isbn": p.get("old_isbn"),
        "publisher": p.get("old_publisher"), "year": p.get("old_year")})
    if wid and p.get("primary_alias_id") and p.get("old_work_title") is not None:
        acc.works.writes.update_alias(p["primary_alias_id"], p["old_work_title"])
    for aid in p.get("created_alias_ids") or []:
        acc.works.writes.delete_alias(aid)
    p["applied"] = False
    p["created_alias_ids"] = []


_UNSET = object()


def resolve_edition(db, edition_id: int, *, bi: Optional[BookIdentifier] = None,
                    sources=None, ladder=None, identifier=_UNSET,
                    commit: bool = True) -> str:
    """applied | no_identifier | miss | already.

    Derives BOTH the ISBN/authority title AND the page (LLM) title, and keeps the
    fuller of the two (the user's rule) — so a generic authority title for a
    multi-volume set loses to the volume-specific page title, while a clean single
    book keeps its ISBN title. ISBN/publisher/year (when found) are always stored.

    'applied'       → a title was chosen (from ISBN and/or page) and applied + queued.
    'no_identifier' → no ISBN/LCCN/… on the page (caller falls back to the LLM).
    'miss'          → identifier(s) found, but neither a record NOR a page title.
    'already'       → a pending edition_metadata proposal for this book exists."""
    if _proposal_pending(db, edition_id):
        return "already"
    bi = bi or BookIdentifier()
    text = _full_text(db, edition_id)
    rec_cip = cip_mod.parse_cip(text)                       # structured CIP record
    labeled_cip = rec_cip.title if (rec_cip and rec_cip.kind == "labelled") else None
    freeform_cip = rec_cip.title if (rec_cip and rec_cip.kind == "freeform") else None
    cip_title = labeled_cip or freeform_cip                 # for the payload/display

    # Identifier (from OCR text ONLY, never the filename): PREFER the CIP block's own
    # ISBN — it's co-located with the title, so the two describe the same book — else
    # the whole-text CIP-aware scan / the one the walk passed in.
    cip_isbn = rec_cip.isbns[0] if (rec_cip and rec_cip.isbns) else None
    if cip_isbn:
        ident = Identifier("isbn", cip_isbn, "cip")
    elif identifier is not _UNSET:
        ident = identifier
    else:
        ident = bi.find_in_text(text, markers=_CIP_MARKERS)
    if ident is None:
        return "no_identifier"

    res = bi.resolve([ident], sources=sources)
    page_title = _page_title(db, edition_id, ladder)        # page text only, LLM
    id_title = _titlecase((res.record.title or "").strip()) if res else None

    # TITLE-CONFIRMATION GATE: trust the ISBN's looked-up title only if it AGREES with
    # the book's OWN printed title (CIP, else page). On disagreement the ISBN resolved
    # to a DIFFERENT book (OCR-corrupted into another valid ISBN, or a stray ISBN
    # elsewhere on the page) → drop its title AND its metadata; keep it only if it is
    # the CIP block's own ISBN (which is consistent with the title by construction).
    own = cip_title or page_title
    if id_title and own and not _titles_agree(id_title, own):
        id_title, res = None, None
        if ident.found_in != "cip":
            ident = None

    # Title-of-record priority:
    #   1. labelled CIP "Title:" field — authoritative + has the subtitle;
    #   2. else a STRUCTURED alternative (free-form CIP or page title with a real
    #      subtitle/volume, fuller than the ISBN title) — drops over-reads & fragments;
    #   3. else the clean ISBN title; else page; else free-form CIP.
    if labeled_cip:
        final, source = labeled_cip, "cip"
    else:
        alts = [(c, tag) for c, tag in ((freeform_cip, "cip"), (page_title, "page"))
                if c and _has_structure(c) and len(c) > len(id_title or "")]
        if alts:
            final, source = max(alts, key=lambda ct: len(ct[0]))
        elif id_title:
            final, source = id_title, "isbn"
        elif page_title:
            final, source = page_title, "page"
        elif freeform_cip:
            final, source = freeform_cip, "cip"
        else:
            final, source = None, None
    if not final:
        # No title from any source. Keep a trusted ISBN (real fact about the book) and
        # defer — do NOT guess a title.
        isbn = ident.value if (ident and ident.scheme == "isbn") else None
        if isbn:
            acc = _acc(db)
            cur = acc.editions.reads.get(edition_id)
            if cur is not None and not (cur.isbn or "").strip():   # COALESCE(isbn, ?): fill if empty
                acc.editions.writes.set_columns(edition_id, {"isbn": isbn})
            if commit:
                db.commit()
        return "miss"
    payload = _build_payload(db, edition_id, resolution=res, identifier=ident,
                             cip_title=cip_title, page_title=page_title,
                             id_title=id_title, final_title=final, title_source=source)
    _apply_record(db, payload)
    _acc(db).review.writes.enqueue("edition_metadata", payload)
    if commit:
        db.commit()
    return "applied"


# ── review accept / reject ─────────────────────────────────────────────────────────
def accept_edition_metadata(db, item_id: int, *, commit: bool = True) -> bool:
    """Confirm an applied edition record (it's already written → just resolve)."""
    review = _acc(db).review
    row = review.reads.get_typed(item_id, "edition_metadata")
    if not row or row[1] != "pending":
        return False
    p = json.loads(row[0])
    if not p.get("applied"):
        _apply_record(db, p)
        review.writes.set_payload(item_id, p)
    review.writes.resolve(item_id)
    if commit:
        db.commit()
    return True


def reject_edition_metadata(db, item_id: int, *, commit: bool = True) -> bool:
    """Reject an edition record → revert title/isbn/publisher/year + the added
    contributors and aliases."""
    review = _acc(db).review
    row = review.reads.get_typed(item_id, "edition_metadata")
    if not row or row[1] != "pending":
        return False
    p = json.loads(row[0])
    if p.get("applied"):
        _revert_record(db, p)
        review.writes.set_payload(item_id, p)
    review.writes.reject(item_id)
    if commit:
        db.commit()
    return True


def _throttled_sources():
    """Authority sources whose HTTP opener throttles + retries 429/5xx — so a bulk
    run's rate-limits aren't mistaken for misses. Used by the identifier job."""
    from .edition_verify import GoogleBooksSource, OpenLibrarySource
    from .http_util import ThrottledOpener
    op = ThrottledOpener()
    return [OpenLibrarySource(opener=op), GoogleBooksSource(opener=op)]


# ── the walk: two partitions (identifier / LLM), runnable independently ─────────────
def resolve_all_editions(db, *, sources=None, ladder=None, limit: Optional[int] = None,
                         verbose: bool = False, only: Optional[str] = None) -> dict:
    """Resolve editions, partitioned by whether they carry an identifier:
      • books WITH an ISBN/LCCN → the identifier pass (authoritative; on a miss the
        ISBN is kept and the book is deferred — NO LLM);
      • books WITHOUT one        → the LLM title pass.
    `only='identifier'` or `only='llm'` runs just that partition — so the two can run
    as separate (even concurrent) jobs: the fast local LLM job and the slow throttled
    network job. `only=None` does both. Commits per row (resumable)."""
    bi = BookIdentifier()
    if sources is None and only in (None, "identifier"):
        sources = _throttled_sources()
    ids = sorted(_acc(db).editions.reads.all_ids())
    if limit:
        ids = ids[:int(limit)]
    tally = {"id_applied": 0, "miss": 0, "no_identifier": 0, "llm_applied": 0,
             "llm_queued": 0, "llm_unchanged": 0, "no_text": 0, "no_title": 0,
             "mojibake": 0, "already": 0, "skipped": 0}
    if verbose:
        print(f"Edition resolution over {len(ids)} book(s) "
              f"— partition={only or 'both'}…", flush=True)
    for i, eid in enumerate(ids, 1):
        ident = _book_identifier(db, eid, bi)        # whole-text, CIP-aware, no filename
        has_id = ident is not None
        # Honour the partition: skip what this job doesn't own.
        if (only == "identifier" and not has_id) or (only == "llm" and has_id):
            tally["skipped"] += 1
            continue

        id_t = page_t = cip_t = None      # the title candidates (shown in verbose)
        if has_id:
            s = resolve_edition(db, eid, bi=bi, sources=sources, ladder=ladder,
                                identifier=ident, commit=True)
            if s == "applied":
                tally["id_applied"] += 1
                src, name, id_t, page_t, cip_t = _last_applied_meta(db, eid)
                method, mark = src, "✓"        # 'cip' | 'isbn' | 'page' — which won
            elif s == "already":
                tally["already"] += 1
                method, mark, name = "—", "»", _edition_title(db, eid)
            else:  # miss — ISBN kept, deferred
                tally["miss"] += 1
                method, mark, name = "id:miss", "·", _edition_title(db, eid)
        else:
            s2 = work_titles.derive_title_for_edition(db, eid, ladder=ladder, commit=True)
            key = {"applied": "llm_applied", "queued": "llm_queued",
                   "unchanged": "llm_unchanged", "no_text": "no_text",
                   "no_title": "no_title", "mojibake": "mojibake",
                   "already": "already"}.get(s2, "no_title")
            tally[key] += 1
            method = "llm"
            mark = {"llm_applied": "✓", "llm_queued": "?", "already": "»",
                    "mojibake": "✗"}.get(key, "·")
            name = work_titles._assigned_name(db, eid, s2)

        if verbose:
            fname = work_titles._edition_filename(db, eid)
            print(f"  [{i}/{len(ids)}] {mark} {method:9} {name[:60]!r}  ⟵  {fname[:50]}",
                  flush=True)
            # for identifier books, dump ALL candidates so the merge is auditable
            if id_t is not None or page_t is not None or cip_t is not None:
                for tag, val in (("cip", cip_t), ("isbn", id_t), ("page", page_t)):
                    print(f"               {tag} {'◀' if method == tag else ' '} "
                          f"{(val or '—')[:70]!r}", flush=True)
    if verbose:
        print(f"done: {tally}", flush=True)
    return tally


def _edition_title(db, eid: int) -> str:
    ed = _acc(db).editions.reads.get(eid)
    return ed.title if ed and ed.title else ""


def _last_applied_meta(db, eid: int) -> tuple:
    """(title_source, chosen_title, id_title, page_title) from the most recent
    edition_metadata proposal for `eid` — title_source is 'page' or 'isbn'."""
    raw = _acc(db).review.reads.latest_payload_of_type(
        "edition_metadata", f'%"edition_id": {eid}%')
    if raw:
        p = json.loads(raw)
        return ((p.get("title_source") or "?"), (p.get("new_title") or ""),
                p.get("id_title"), p.get("page_title"), p.get("cip_title"))
    return "?", _edition_title(db, eid), None, None, None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Edition metadata resolution. Books with an ISBN/LCCN → "
                    "authoritative record (throttled, retries rate-limits); books "
                    "without → LLM title pass. Run the two partitions separately with "
                    "--only so the fast local job isn't blocked by the slow network job.")
    ap.add_argument("db")
    ap.add_argument("--only", choices=("identifier", "llm"), default=None,
                    help="run just one partition (default: both)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    db = init_db(args.db)
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA journal_mode = WAL")     # let the two jobs write concurrently
    tally = resolve_all_editions(db, only=args.only, limit=args.limit,
                                 verbose=not args.quiet)
    print("summary:", tally)


if __name__ == "__main__":
    main()
