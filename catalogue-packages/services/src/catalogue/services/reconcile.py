"""Reconcile the library folder against the catalogue (filesystem sync).

Renames/moves are already handled by sweep.py's content-hash upsert; this adds
the rest — files deleted, edited/annotated in place, or re-OCR'd (in place or as
a new file) — and routes the ambiguous cases to the review queue with a PROPOSED
disposition the operator confirms in the UI (`/reconcile`).

Identity model (see whats_next / the design discussion):
  - `file_hash`    — SHA-256 of bytes: exact file identity (moves, exact dups).
  - `content_hash` — `content_fingerprint`: a hash of the normalized TEXT layer
    when it's trustworthy (native/ocr_good), else the byte hash. Annotations
    don't change the page text, so an annotated copy keeps its content_hash; a
    re-OCR (new text layer) changes it.

`classify` is pure (DB lookups only) → unit-tested without touching the real
library. `scan_dir` is the thin filesystem front end (operator-run).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from catalogue.db_store import signature
from .isbn import normalize_isbn
from catalogue.db_store import default_db_path


def _acc(db):
    """A system Access over this connection — engine-routed holding/edition reads + writes, the
    review queue, and the ingest-ignore list. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)

# ── Content fingerprint ───────────────────────────────────────────────────────
# The signature format lives entirely in `signature.py`. This stays as a thin
# wire-string adapter (db.py and scan_dir store the string in `content_hash`); all
# *reasoning* about a signature goes through the Signature value object, never the
# raw string.
def content_fingerprint(text: Optional[str], text_status: Optional[str],
                        byte_hash: Optional[str]) -> Optional[str]:
    """Wire-form content signature for storage in `holding.content_hash`. Delegates to
    `signature.of`; returns the opaque string (or None). To *interpret* a stored value
    use `signature.parse`, never string-inspect the result."""
    sig = signature.of(text, text_status, byte_hash)
    return sig.wire if sig else None


# ── Similarity (candidate "same edition?" search) ─────────────────────────────
def _tokens(s: Optional[str]) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", (s or "").lower()))


def _jaccard(a: set, b: set) -> float:
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def _weighted_jaccard(a: set, b: set, df: dict) -> float:
    """Jaccard with each token weighted by 1/(1+df) — its rarity across the catalogue's
    titles. Words that recur across many editions (a series name like "the new grove
    dictionary of music and musicians", shared by 25 volumes) collapse toward zero, so
    they can't by themselves make volume 15 look like a re-OCR of volume 13; a volume's
    own range words ("liturgy to martini", in one title) stay heavy and do the
    discriminating. Two genuinely-same titles still score ~1.0 (overlap == union)."""
    union = a | b
    if not union:
        return 0.0
    w = lambda t: 1.0 / (1 + df.get(t, 0))   # df 0 (novel query token) → 1.0; df 25 → 0.04
    wu = sum(w(t) for t in union)
    return sum(w(t) for t in (a & b)) / wu if wu else 0.0


def find_candidate_editions(db, *, text=None, title=None, isbn=None,
                            limit=5, threshold=0.4) -> list:
    """Rank existing editions that might be the same work as an incoming file —
    so a new/changed file's prompt comes with a suggested match. Signals:
    exact ISBN (1.0), df-discounted title-token Jaccard (shared series boilerplate
    discounted), and a text-shingle Jaccard boost against the title candidates'
    indexed text (robust to OCR char differences)."""
    cands: dict[int, dict] = {}
    isbn = normalize_isbn(isbn) if isbn else isbn
    all_eds = [(e.id, e.title, e.isbn) for e in _acc(db).editions.reads.all()]
    if isbn:
        for eid, t, e_isbn in all_eds:
            if e_isbn == isbn:
                cands[eid] = {"edition_id": eid, "title": t, "score": 1.0, "why": ["isbn"]}
    qt = _tokens(title)
    if qt:
        titles = [(eid, t) for eid, t, _i in all_eds]
        # Document frequency of each title token, so shared series words are down-weighted.
        df: dict[str, int] = {}
        for _eid, t in titles:
            for tok in _tokens(t):
                df[tok] = df.get(tok, 0) + 1
        for eid, t in titles:
            if eid in cands:
                continue
            j = _weighted_jaccard(qt, _tokens(t), df)
            if j >= threshold:
                cands[eid] = {"edition_id": eid, "title": t, "score": round(j, 2),
                              "why": ["title"]}
    # Text-shingle boost against the candidates we already have (bounded cost).
    if text:
        qs = _shingles(text)
        for c in cands.values():
            rows = _acc(db).editions.reads.text_content(c["edition_id"], 40)
            if rows:
                js = _jaccard(qs, _shingles(" ".join(rows)))
                if js > 0:
                    c["score"] = round(max(c["score"], js), 2)
                    if js >= threshold and "text" not in c["why"]:
                        c["why"].append("text")
    out = [c for c in cands.values() if c["score"] >= threshold]
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:limit]


def _shingles(text: str, k: int = 4) -> set:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {" ".join(toks[i:i + k]) for i in range(max(0, len(toks) - k + 1))}


# ── Classify a scan against the catalogue ─────────────────────────────────────
@dataclass
class ScannedFile:
    path: str
    file_hash: str
    content_hash: Optional[str] = None
    text: Optional[str] = None
    title: Optional[str] = None
    isbn: Optional[str] = None


# Dispositions and whether they're safe to auto-apply without asking.
AUTO_KINDS = {"unchanged", "moved", "annotated"}


@dataclass
class _HoldingIndex:
    """One snapshot of the catalogue's holdings, indexed for per-file classification.
    Built once per scan (the streaming path reuses it across every file)."""
    holdings: list
    by_hash: dict
    by_path: dict
    by_content: dict
    ignored_paths: set
    ignored_hashes: set


def _holding_index(db) -> _HoldingIndex:
    acc = _acc(db)
    holdings = acc.holdings.reads.reconcile_index()
    by_hash = {h[2]: h for h in holdings if h[2]}
    by_path = {h[1]: h for h in holdings if h[1]}
    ignored_paths = acc.ingest_ignore.paths()
    ignored_hashes = acc.ingest_ignore.hashes()
    by_content: dict[str, list] = {}
    for h in holdings:
        sig = signature.parse(h[3])              # only text-based signatures dedup
        if sig and sig.is_text:
            by_content.setdefault(h[3], []).append(h)
    return _HoldingIndex(holdings, by_hash, by_path, by_content,
                         ignored_paths, ignored_hashes)


def classify_one(db, idx: "_HoldingIndex", sf: "ScannedFile") -> Optional[dict]:
    """Disposition for ONE scanned file against `idx` (no missing-pass — that needs the
    whole scan). Returns the disposition dict, or None when the file was operator-ignored
    (stay silent). Pure read except `find_candidate_editions` (similarity lookup)."""
    h = idx.by_hash.get(sf.file_hash)
    if h:
        if h[1] == sf.path:
            return {"kind": "unchanged", "path": sf.path, "holding_id": h[0]}
        return {"kind": "moved", "path": sf.path, "holding_id": h[0],
                "edition_id": h[4], "detail": f"was {h[1]}"}
    hp = idx.by_path.get(sf.path)
    if hp:                                        # same path, different bytes
        stored_sig = signature.parse(hp[3])
        same_text = bool(stored_sig and stored_sig.matches(sf.content_hash))
        return {
            "kind": "annotated" if same_text else "content_changed",
            "path": sf.path, "holding_id": hp[0], "edition_id": hp[4],
            "file_hash": sf.file_hash, "content_hash": sf.content_hash,
            "detail": "bytes changed, text identical" if same_text
                      else "text layer changed (re-OCR or edit)"}
    # operator ignored this file before (by path or by bytes) — stay silent.
    if sf.path in idx.ignored_paths or sf.file_hash in idx.ignored_hashes:
        return None
    # genuinely new file (unknown bytes, unknown path)
    dups = idx.by_content.get(sf.content_hash) if sf.content_hash else None
    if dups:
        return {"kind": "content_match", "path": sf.path,
                "file_hash": sf.file_hash, "content_hash": sf.content_hash,
                "candidates": [{"edition_id": d[4], "score": 1.0,
                                "why": ["identical text"]} for d in dups],
                "detail": "same text layer as an existing copy"}
    cands = find_candidate_editions(db, text=sf.text, title=sf.title, isbn=sf.isbn)
    return {"kind": "new_maybe_reocr" if cands else "new", "path": sf.path,
            "file_hash": sf.file_hash, "content_hash": sf.content_hash,
            "title": sf.title, "isbn": sf.isbn, "candidates": cands}


def classify_missing(db, idx: "_HoldingIndex", seen_paths: set, seen_hashes: set) -> list:
    """The cross-file pass: holdings whose file wasn't seen this scan (by path or hash).
    A file can be unseen yet present (it lives outside the scanned root) — so confirm it's
    genuinely gone with file_state before crying 'missing', and let an offline
    kDrive/iCloud placeholder report as 'offline' rather than deleted. Needs the COMPLETE
    seen-set, so it runs only after the whole walk."""
    out = []
    for h in idx.holdings:
        if h[1] and h[1] not in seen_paths and h[2] not in seen_hashes:
            if file_state(h[1]) == "present" and not _looks_offline(h[1]):
                continue                       # really there, just outside this scan's
                                               # scope (an offline 0-byte stub falls through)
            offline = _looks_offline(h[1])
            out.append({"kind": "offline" if offline else "missing",
                        "path": h[1], "holding_id": h[0], "edition_id": h[4],
                        "detail": "iCloud/kDrive placeholder (not downloaded)"
                                  if offline else "file not found on disk"})
    return out


def classify(db, scanned: list) -> list:
    """Diff a list of ScannedFile against `holding`. Returns a list of disposition
    dicts: {kind, path, holding_id?, edition_id?, candidates?, detail}. Composed of the
    per-file `classify_one` plus the final `classify_missing` pass (behaviour unchanged;
    the streaming path reuses the same two helpers)."""
    idx = _holding_index(db)
    seen_hashes, seen_paths, out = set(), set(), []
    for sf in scanned:
        seen_hashes.add(sf.file_hash)
        seen_paths.add(sf.path)
        item = classify_one(db, idx, sf)
        if item is not None:
            out.append(item)
    out.extend(classify_missing(db, idx, seen_paths, seen_hashes))
    return out


def _looks_offline(path: str) -> bool:
    """A kDrive/iCloud placeholder (evicted, not deleted): present on disk but its
    content isn't downloaded. Catches a 0-byte stub AND a kDrive Lite Sync online-only
    placeholder (real apparent size, zero content). Conservative — only True when we can
    positively tell it's offline rather than gone."""
    try:
        p = Path(path)
        if not p.exists():
            return False
        if p.stat().st_size == 0:
            return True
        from .cloudsync import is_online_only
        return is_online_only(str(p))
    except OSError:
        return False


def file_state(path: Optional[str]) -> str:
    """Disk state of a recorded holding path — the shared truth for BOTH the scan's
    broken-link detection and the UI's open-control marker. One of:
      'none'    — no path recorded.
      'present' — the file is on disk: real bytes OR a kDrive/iCloud dataless
                  placeholder (the stub still has a directory entry; its content
                  hydrates when opened). `os.path.exists` is metadata-only — it does
                  NOT trigger a download — so this is cheap and placeholder-safe.
      'missing' — path recorded but the file is GONE (deleted/renamed) AND we can
                  prove it: the parent directory is present, so the mount is up. If
                  the parent is ALSO absent the whole drive/folder is offline; we say
                  'present' there rather than cry 'missing' for every book at once."""
    if not path:
        return "none"
    try:
        p = Path(path)
        if p.exists():
            return "present"
        return "missing" if p.parent.exists() else "present"
    except OSError:
        return "present"                       # can't tell → never false-alarm


def broken_links(db) -> dict:
    """The two 'broken link' classes the Scan page surfaces and the UI flags:
      'gone'    — holdings whose recorded file is missing on disk (deleted/renamed).
      'orphans' — editions with NO holding at all (the file record was removed,
                  leaving the book with nothing to open — e.g. edition #248)."""
    acc = _acc(db)
    gone = []
    for hid, eid, path, _fh, _ch in acc.holdings.reads.with_files():
        if file_state(path) == "missing":
            gone.append({"holding_id": hid, "edition_id": eid, "path": path})
    orphans = [{"edition_id": r[0], "title": r[1]} for r in acc.editions.reads.without_holding()]
    return {"gone": gone, "orphans": orphans}


def prune_stale_ingest(db, *, commit: bool = True) -> int:
    """Drop pending ingest items that newer catalogue state has made moot — so the
    Reconcile page self-heals instead of showing cruft from an earlier scan:
      • any item whose `path` is already some holding's CURRENT file_path (it's been
        catalogued/moved/relinked since — it's not 'new' or a 'duplicate' anymore);
      • a 'missing' item whose holding now resolves to a present file (e.g. relinked).
    Returns the number dropped. Mirrors mount.repoint's drop_pending, but keyed on
    live state rather than a root prefix."""
    current = {os.path.normpath(fp) for _i, _e, fp, _fh, _ch in _acc(db).holdings.reads.with_files()}
    # Only "this file is uncatalogued" dispositions go stale when the path turns out to
    # already be a holding's file. NOT content_changed/annotated — those legitimately
    # share the holding's path (an in-place edit) and must survive for the operator.
    _stale_if_catalogued = {"new", "new_maybe_reocr", "content_match"}
    acc = _acc(db)
    dropped = 0
    for rid, pj in acc.review.reads.pending_items("ingest"):
        p = json.loads(pj) or {}
        path, kind, hid = p.get("path"), p.get("kind"), p.get("holding_id")
        drop = bool(path and kind in _stale_if_catalogued
                    and os.path.normpath(path) in current)
        if not drop and kind == "missing" and hid:
            r = acc.holdings.reads.location_of(hid)
            if r and file_state(r[0]) == "present":
                drop = True
        if drop:
            acc.review.writes.delete(rid)
            dropped += 1
    if commit:
        db.commit()
    return dropped


def prune_excluded_ingest(db, *, commit: bool = True) -> int:
    """Drop pending ingest items whose file now lives under an excluded folder (or
    otherwise matches an exclusion rule) — so unchecking a folder in /settings clears
    its already-scanned 'new' files from the Scan page, not just future scans. Run on
    every Reconcile-page load (self-heal) and on each folder toggle. Returns the count
    dropped. See [[mount-root-settings]] / catalogue.services.skip."""
    from catalogue.services.skip import is_excluded
    acc = _acc(db)
    dropped = 0
    for rid, pj in acc.review.reads.pending_items("ingest"):
        path = (json.loads(pj) or {}).get("path")
        if path and is_excluded(file_path=path):
            acc.review.writes.delete(rid)
            dropped += 1
    if commit:
        db.commit()
    return dropped


# ── Apply ─────────────────────────────────────────────────────────────────────
def _enqueue(db, item: dict) -> int:
    """Enqueue an ingest disposition, idempotent on `path`: a file path yields at
    most one *pending* ingest row. Re-running the scan while an item is still
    pending refreshes that row's payload to the latest classification instead of
    piling up duplicates (the Scan page was showing the same file N times)."""
    acc = _acc(db)
    path = item.get("path")
    if path is not None:
        existing = acc.review.reads.pending_id_by_json("ingest", "$.path", path)
        if existing:
            acc.review.writes.set_payload(existing, item)
            return existing
    return acc.review.writes.enqueue("ingest", item)


def apply_auto(db, item: dict) -> None:
    """Apply a safe disposition (moved / annotated) with no operator input."""
    if item["kind"] == "moved":
        _acc(db).holdings.writes.set_file_path(item["holding_id"], item["path"])
    elif item["kind"] == "annotated":      # same path, new bytes, same text
        _acc(db).holdings.writes.set_hashes(
            item["holding_id"], item["file_hash"], item["content_hash"])


def _tally(db, item: dict, summary: dict, auto_safe: bool) -> None:
    """Apply one disposition: count it, auto-apply the safe kinds (moved/annotated),
    enqueue the rest as an `ingest` review item. Shared by `reconcile` (batch) and
    `reconcile_stream` (incremental). Never commits — the caller controls that."""
    k = item["kind"]
    summary["by_kind"][k] = summary["by_kind"].get(k, 0) + 1
    if k == "unchanged":
        return
    if auto_safe and k in ("moved", "annotated"):
        apply_auto(db, item)
        summary["applied"] += 1
    else:
        _enqueue(db, item)
        summary["enqueued"] += 1


def reconcile(db, scanned: list, *, auto_safe: bool = True, commit: bool = True) -> dict:
    """Classify a scan, auto-apply the safe rows, and enqueue the rest as `ingest`
    review items the operator resolves in the UI. Returns a summary."""
    summary: dict = {"applied": 0, "enqueued": 0, "by_kind": {}}
    for item in classify(db, scanned):
        _tally(db, item, summary, auto_safe)
    if commit:
        db.commit()
    return summary


def reconcile_stream(db, roots, *, suffixes=(".pdf", ".epub"), auto_safe: bool = True,
                     batch: int = 25, on_progress=None) -> dict:
    """Streaming scan + reconcile: walk INBOX-FIRST, classify/apply/enqueue each file as
    it's read, and commit every `batch` files — so fresh inbox drops become reviewable
    within seconds and the rest of the library streams in behind them, instead of one
    long wait for a full-library dump. The 'missing holding' pass runs once at the end
    (it needs the complete seen-set). `on_progress(summary)` fires after each commit.
    Returns the same summary shape as `reconcile`."""
    idx = _holding_index(db)
    summary: dict = {"applied": 0, "enqueued": 0, "by_kind": {}, "scanned": 0}
    seen_hashes, seen_paths = set(), set()
    for sf in iter_scanned(db, roots, suffixes=suffixes):
        seen_hashes.add(sf.file_hash)
        seen_paths.add(sf.path)
        item = classify_one(db, idx, sf)
        if item is not None:
            _tally(db, item, summary, auto_safe)
        summary["scanned"] += 1
        if summary["scanned"] % batch == 0:
            db.commit()
            if on_progress is not None:
                on_progress(summary)
    for item in classify_missing(db, idx, seen_paths, seen_hashes):
        _tally(db, item, summary, auto_safe)
    db.commit()
    if on_progress is not None:
        on_progress(summary)
    return summary


def apply_decision(db, item_id: int, action: str, *, target_edition_id=None,
                   commit: bool = True) -> dict:
    """Resolve one `ingest` review item with an operator-chosen `action`:
    repoint | accept | replace | add_copy | distinct | remove | ignore.
    Marks the review item resolved."""
    acc = _acc(db)
    row = acc.review.reads.get_typed(item_id, "ingest")
    if not row:
        raise ValueError(f"no ingest item {item_id}")
    p = json.loads(row[0])
    path, fh, ch = p.get("path"), p.get("file_hash"), p.get("content_hash")
    result = {"action": action}

    if action == "repoint":                       # moved
        acc.holdings.writes.set_file_path(p["holding_id"], path)
    elif action == "accept":                      # annotated / content_changed in place
        acc.holdings.writes.set_path_hashes(p["holding_id"], path, fh, ch)
    elif action == "replace":                     # new file supersedes a chosen edition's copy
        hs = acc.holdings.reads.by_edition(target_edition_id)
        if not hs:
            raise ValueError("replace target edition has no holding")
        acc.holdings.writes.set_path_hashes(hs[0].id, path, fh, ch)
        result["holding_id"] = hs[0].id
    elif action == "add_copy":                    # another copy of an existing edition
        result["holding_id"] = _new_holding(db, target_edition_id, path, fh, ch)
    elif action in ("distinct", "new"):           # brand-new edition + holding
        eid = acc.editions.writes.create({"title": Path(path).stem}).target.id
        from catalogue.services import subjects as S
        S.ensure_categorized(db, "edition", eid)   # never subject-less; review later
        result["edition_id"] = eid
        result["holding_id"] = _new_holding(db, eid, path, fh, ch)
        # Don't silently catalogue it as finished — drop it into the Books review
        # pile (a work_detection row) so the operator confirms its details (and
        # can catch duplicates) before it's treated as a settled record.
        _enqueue_for_review(db, eid, p, result["holding_id"])
    elif action == "remove":                       # confirmed delete of a missing holding
        acc.journal.clear("holding", "id", [p["holding_id"]])
    elif action == "ignore":                       # never surface this file again
        if path is not None:
            acc.ingest_ignore.add(path, fh, ch)
    else:
        raise ValueError(f"unknown action {action!r}")

    acc.review.writes.resolve(item_id)
    if commit:
        db.commit()
    return result


def _new_holding(db, edition_id, path, file_hash, content_hash) -> int:
    from catalogue.db_store import derive_holding_type
    from catalogue.services.mount import owning_root_id
    ht = derive_holding_type("electronic", path, None)
    # text-based signature implies a readable text layer; byte-based → unknown.
    sig = signature.parse(content_hash)
    ts = "ocr_good" if (sig and sig.is_text) else None
    hid = _acc(db).holdings.writes.insert_holding(
        edition_id=edition_id, form="electronic", file_path=path, file_hash=file_hash,
        content_hash=content_hash, holding_type=ht, text_status=ts, root_id=owning_root_id(path))
    _relink_reader_orphans(db, hid, content_hash)
    return hid


def _relink_reader_orphans(db, holding_id, content_hash) -> None:
    """Re-attach reader marks orphaned by an earlier delete of the SAME file (matched by
    content_hash) to this freshly-(re)imported holding — the reader plan's "survive and re-attach"
    (N0/N6). Best-effort: a scan DB without the reader-state tables (CLI ingest) just no-ops, and
    marks simply stay orphaned until opened in a context that has them."""
    if not content_hash:
        return
    try:
        from catalogue.db_store.reader_state import SqliteReaderStateStore
        SqliteReaderStateStore(db).relink_orphans(holding_id=holding_id, content_hash=content_hash)
    except Exception:
        pass


def _enqueue_for_review(db, edition_id, item: dict, holding_id) -> None:
    """Put a Scan-created edition into the Books review pile (a `work_detection`
    row) so it isn't treated as a finished record. Minimal payload — title/isbn
    from the scan, everything else left for the operator to confirm in review.
    Mirrors the shape `work_detect.store_detection` produces; default
    determination 'modern' (no classical match assumed), flippable in review."""
    from catalogue.services import work_detect as WD
    path = item.get("path")
    title = item.get("title") or (Path(path).stem if path else None)
    WD.store_detection(db, edition_id, "single", {
        "determination": "modern",
        "title": {"english": title, "sanskrit": None, "tibetan": None},
        "canonical": {"system": None, "number": None, "title_en": None,
                      "glosses": None, "url_84000": None, "url_bdrc": None},
        "confidence": 0.0,
        "source": "scan-new",
        "authors_recorded": [], "translators_recorded": [],
        "authors_detected": [], "translators_detected": [],
        "stored_title": title,
        "isbn": item.get("isbn"),
        "isbn_url": None,
        "file": {"holding_id": holding_id, "path": path},
    }, commit=False)


# ── Filesystem front end (operator-run) ───────────────────────────────────────
def _pdf_text_sans_annots(path) -> Optional[str]:
    """PDF page text with annotations removed (in memory — the file is not
    written), so the content fingerprint ignores highlights/notes/typed FreeText.
    Returns None if PyMuPDF isn't available or the file can't be read."""
    try:
        import fitz
        doc = fitz.open(str(path))
        try:
            parts = []
            for pg in doc:
                for a in list(pg.annots() or []):
                    pg.delete_annot(a)
                parts.append(pg.get_text())
            return "\n".join(parts)
        finally:
            doc.close()
    except Exception:
        return None


def iter_scanned(db, roots, *, suffixes=(".pdf", ".epub")):
    """Walk `roots` INBOX-FIRST (via `sweep.walk`) and YIELD a ScannedFile per media
    file as it's read — so a streaming caller can classify/commit incrementally instead
    of waiting for the whole walk. Reuses cached text for known files; extracts new
    files (best-effort) so similarity has something to match."""
    from .sweep import walk, _hash_file
    from .extract import extract, book_metadata
    for path in walk(roots, suffixes):                  # inbox files first, then the rest
        fh = _hash_file(path, 3, 0.5)
        acc = _acc(db)
        text = acc.editions.reads.raw_text_for_hash(fh)
        ts_row = acc.holdings.reads.text_status_by_hash(fh)
        text_status = ts_row[0] if ts_row else None
        isbn = None
        if text is None and not ts_row:                 # unknown file → extract
            try:
                ex = extract(path)
                if ex and not ex.is_image_only:
                    text_status = "ocr_good"
                    # Strip annotations from the fingerprint text so highlights,
                    # bookmarks AND typed notes don't change identity (a reader's
                    # marginalia isn't the work's content).
                    text = _pdf_text_sans_annots(path) or ex.text \
                        if str(path).lower().endswith(".pdf") else ex.text
                else:
                    text_status = "image_only"
                isbn = (book_metadata(path) or {}).get("isbn")
            except Exception:
                pass
        yield ScannedFile(
            str(path), fh, content_fingerprint(text, text_status, fh),
            text=text, title=path.stem, isbn=isbn)


def scan_dir(db, roots, *, suffixes=(".pdf", ".epub")) -> list:
    """Eager (whole-library) form of `iter_scanned` — kept for the CLI and callers that
    want the full list. The web scan uses `reconcile_stream` (incremental) instead."""
    return list(iter_scanned(db, roots, suffixes=suffixes))


def scan_roots() -> list:
    """Directories the scan WALKS: the configured library roots PLUS any standalone inbox
    dirs (vocab `_inbox_dirs`) that aren't already inside a root — so a top-level
    `Library/_INBOX/` sibling of the roots is picked up. A per-root `_inbox/` sits
    inside a root and is already covered by walking it."""
    roots = library_roots()
    from catalogue.services import filing
    seen = {os.path.normpath(r) for r in roots}
    for d in filing.inbox_dirs():
        nd = os.path.normpath(d)
        if nd not in seen and not any(nd.startswith(r + os.sep) for r in seen):
            if os.path.isdir(d):
                roots = roots + [d]
                seen.add(nd)
    return roots


def library_roots() -> list:
    """Configured library root PATHS: $CATALOGUE_LIBRARY_ROOT (os.pathsep-separated),
    else every root in vocab `_library_roots` (via mount), else the sweep default.
    scan_dir already loops over the returned list, so multiple roots just work."""
    env = os.environ.get("CATALOGUE_LIBRARY_ROOT")
    if env:
        return [r for r in env.split(os.pathsep) if r]
    from catalogue.services.mount import library_roots as configured_roots
    paths = [r.path for r in configured_roots()]
    if paths:
        return paths
    from .sweep import default_mount_root
    return [str(default_mount_root())]


def main(argv=None) -> None:
    import argparse
    from catalogue.db_store import connect
    ap = argparse.ArgumentParser(description="Reconcile the library folder with the catalogue.")
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--root", action="append", help="library root(s) to scan")
    ap.add_argument("--no-auto", action="store_true",
                    help="don't auto-apply moved/annotated; enqueue everything")
    args = ap.parse_args(argv)
    db = connect(args.db)
    db.execute("PRAGMA busy_timeout = 30000")
    roots = args.root or scan_roots()
    print(f"scanning {roots}… (inbox first)", flush=True)
    summary = reconcile_stream(
        db, roots, auto_safe=not args.no_auto,
        on_progress=lambda s: print(f"  …{s['scanned']} files "
                                    f"({s['enqueued']} enqueued)", flush=True))
    print("summary:", summary)


if __name__ == "__main__":
    main()

