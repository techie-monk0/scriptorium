"""WebDAV-aware electronic sweep (§7.1, §6, §13).

Contract:
  - Walk the mount; for each file change-detect by `(path, size, mtime)`
    BEFORE hashing (§6, §13).
  - Hash on change; idempotent upsert keyed by file_hash (so a moved file
    updates its `holding.file_path` instead of duplicating).
  - Extract → NFC-normalize (in `extract.py`) → quality-score
    (`quality.py`) → set `text_status` → poor scores enqueue
    `review_queue.low_quality_ocr` (§4.8c, §4.8d).
  - Persist raw extracted text in `raw_extract_cache` keyed by
    (file_hash, extract_version) (§5, §12.3).
  - Mount is **read-only** (§6, §7.1, §13): every filesystem op against
    the mount opens in binary read mode; the sweep writes only to the DB
    and to a local staging dir.
  - I/O errors → `sweep_problem_log` and continue; commits after each file
    so an interruption resumes cleanly on the next run.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .extract import ExtractedText, ExtractorFn, extract as default_extractor
from .quality import score_text
from catalogue.db_store import default_db_path


def _acc(conn):
    """A system Access over the parent connection — engine-routed sweep persistence (resume state,
    extract caches, problem log), holding/edition upsert, and the review queue. The sweep owns the
    per-file commit on `conn`."""
    from catalogue.access_api import system_conn
    return system_conn(conn)


# ── Config ────────────────────────────────────────────────────────────────
@dataclass
class SweepConfig:
    mount_root: Path
    quality_threshold: float = 0.6      # ≥ → ocr_good ; < → ocr_poor (§4.8d)
    extract_version: int = 1            # §5 cache key part
    retry_attempts: int = 3             # §6 WebDAV resilience
    retry_backoff_s: float = 0.5
    extractor: ExtractorFn = field(default=default_extractor)
    suffixes: tuple[str, ...] = (".pdf", ".epub")
    # If this many consecutive files raise OSError, treat it as a mount-
    # level failure (WebDAV disconnect / ESTALE) and abort cleanly so
    # partial sweep_state survives for resume. Per-file errors below the
    # threshold continue to log + skip as before.
    max_consecutive_errors: int = 5
    # Streaming progress hook (Callable[[SweepReport, Path], None]).
    # Called after every file is processed (or skipped). Default is the
    # no-op; the CLI installs a one-line live counter. Cheap to override
    # — perf overhead is one Python call per file.
    progress: Optional[Callable[["SweepReport", Path], None]] = None
    # Optional (isbn → OL work key) callable. When set, the post-pass keys every
    # edition that has an ISBN but no ol_work_key yet (new editions from any path:
    # edition_resolve, manual edits, _inbox/ sidecars), so cross-format scan
    # matching keeps working without a separate backfill. Default None = skip
    # (keeps file sweeps offline; the CLI/launchd job supplies the real fetch).
    work_key_fetch: Optional[Callable[[str], Optional[str]]] = None


class SweepAborted(RuntimeError):
    """Sweep bailed out — likely a mount disconnect. Partial sweep_state
    is intact; rerunning resumes from where it stopped."""


@dataclass
class SweepReport:
    scanned: int = 0
    skipped_unchanged: int = 0
    new_holdings: int = 0
    updated_paths: int = 0
    image_only: int = 0
    ocr_good: int = 0
    ocr_poor: int = 0
    errors: int = 0
    consecutive_errors: int = 0     # internal — drives mount-disconnect bail
    aborted: bool = False           # set True when SweepAborted is raised
    inbox: object = None            # InboxReport from the _inbox/ sidecar post-pass


# ── Walk ──────────────────────────────────────────────────────────────────
def _walk(root: Path, suffixes: tuple[str, ...]) -> Iterable[Path]:
    from catalogue.services.skip import is_excluded
    for dirpath, dirs, files in os.walk(root):
        # Prune excluded subtrees (any folder matching an exclusion rule —
        # ANNOTATED by default), so nothing under them is ever ingested.
        dirs[:] = [d for d in dirs if not is_excluded(file_path=str(Path(dirpath) / d))]
        for name in sorted(files):
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in suffixes:
                p = Path(dirpath) / name
                if is_excluded(file_path=str(p)):
                    continue
                yield p


def _is_inbox(path: str) -> bool:
    """Whether `path` lives in an inbox — the shared rule, owned by `filing`."""
    from catalogue.services.filing import is_in_inbox
    return is_in_inbox(path)


def walk(roots, suffixes: tuple[str, ...], *, inbox_first: bool = True) -> Iterable[Path]:
    """Walk one root or many, yielding media-file paths. With `inbox_first` (default),
    every file under a configured inbox folder (`filing.inbox_dirs()`) — across ALL roots
    — is yielded BEFORE any other file. So a fresh drop is processed and committed first
    instead of waiting behind a full-library walk; the rest streams in after. `roots` may
    be a single path or a sequence."""
    if isinstance(roots, (str, Path)):
        roots = [roots]
    if not inbox_first:
        for r in roots:
            yield from _walk(Path(r), suffixes)
        return
    # Two buckets, one cheap traversal: inbox files first, everything else after.
    # (Path enumeration is cheap; the expensive hash/extract happens downstream.)
    inbox_paths: list[Path] = []
    other_paths: list[Path] = []
    for r in roots:
        for p in _walk(Path(r), suffixes):
            (inbox_paths if _is_inbox(str(p)) else other_paths).append(p)
    yield from inbox_paths
    yield from other_paths


# ── Hashing (read-only, with retry/back-off for WebDAV flakes) ────────────
def _hash_file(path: Path, attempts: int, backoff: float) -> str:
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            h = hashlib.sha256()
            # Read-only binary stream — guarantees we never mutate the mount.
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError as e:
            last_err = e
            time.sleep(backoff * (2 ** i))
    raise OSError(f"hash failed after {attempts} attempts: {last_err}")


# ── Worker side (pure, picklable) ─────────────────────────────────────────
# Multiprocessing splits the file into two halves:
#   1. Worker (no DB): stat (if needed) + hash + extract + quality score.
#      Pure read-only work against the WebDAV mount + CPU on the local box.
#   2. Parent (single writer): all SQL. Avoids SQLite WAL writer
#      serialization, busy_timeout retries, and the holding-upsert race
#      that you'd otherwise have to police with a UNIQUE index.
#
# The split means workers only ever see (path, size, mtime, extract_version)
# and return a `WorkerResult`. Workers always use the production
# `extract`/`score_text` pair — custom extractors (used by tests) force
# the legacy single-process path; multiprocessing won't pickle a closure.

@dataclass
class WorkerResult:
    """One file's read-side work, ready for the parent to commit."""
    path: str
    size: int
    mtime: float
    error: Optional[str] = None             # set → log + skip, don't write state
    file_hash: Optional[str] = None
    is_image_only: bool = False
    raw_text: Optional[str] = None          # NFC-normalized
    # Quality fields flattened so the dataclass picks/serializes cleanly.
    quality_score: Optional[float] = None
    garbage_ratio: Optional[float] = None
    alpha_ratio: Optional[float] = None
    avg_word_len: Optional[float] = None
    suspect_substitutions: Optional[int] = None    # count, mirrors QualityReport
    char_count: Optional[int] = None
    page_texts: Optional[tuple] = None             # per-page text (PDF); None for reflowable EPUB


def store_page_texts(conn, file_hash, extract_version, page_texts) -> None:
    """Persist per-page text durably in page_text_cache so a future training
    corpus can be chunked WITHOUT re-OCRing (re-OCR is costly/lossy, re-chunking
    is free). One row per page, 1-indexed. No-op when there is no per-page text
    (reflowable EPUB → page_texts is None; the full text stays in raw_extract_cache)."""
    _acc(conn).sweep_state.writes.cache_pages(file_hash, extract_version, page_texts)


@dataclass
class _WorkerInput:
    """The slim, picklable payload sent across the pool boundary. No DB
    handle, no `SweepConfig` (callable fields don't always pickle)."""
    path: str
    size: int
    mtime: float
    extract_version: int
    retry_attempts: int
    retry_backoff_s: float


def _worker_process(inp: _WorkerInput) -> WorkerResult:
    """Top-level (picklable) function run inside the pool. Catches every
    failure into `WorkerResult.error` so a single bad PDF can never
    crash a worker and starve the pool."""
    from .extract import extract as _extract
    from .quality import score_text as _score
    path = Path(inp.path)
    try:
        file_hash = _hash_file(path, inp.retry_attempts, inp.retry_backoff_s)
    except OSError as e:
        return WorkerResult(path=inp.path, size=inp.size, mtime=inp.mtime,
                            error=str(e))
    try:
        extracted = _extract(path)
    except Exception as e:
        return WorkerResult(path=inp.path, size=inp.size, mtime=inp.mtime,
                            error=f"extract: {e}")

    if extracted is None or extracted.is_image_only:
        return WorkerResult(
            path=inp.path, size=inp.size, mtime=inp.mtime,
            file_hash=file_hash, is_image_only=True,
        )
    qr = _score(extracted.text)
    return WorkerResult(
        path=inp.path, size=inp.size, mtime=inp.mtime,
        file_hash=file_hash, raw_text=extracted.text,
        quality_score=qr.score,
        garbage_ratio=qr.garbage_ratio,
        alpha_ratio=qr.alpha_ratio,
        avg_word_len=qr.avg_word_len,
        suspect_substitutions=qr.suspect_substitutions,
        char_count=qr.char_count,
        page_texts=extracted.page_texts,
    )


# ── Per-file processing (serial path) ─────────────────────────────────────
def _process(conn, cfg: SweepConfig, path: Path, report: SweepReport) -> None:
    report.scanned += 1

    # §6: change-detect on (path, size, mtime) BEFORE hashing.
    try:
        st = path.stat()
    except OSError as e:
        _log_problem(conn, path, f"stat: {e}")
        report.errors += 1
        report.consecutive_errors += 1
        return

    if _acc(conn).sweep_state.reads.unchanged(str(path), st.st_size, st.st_mtime):
        report.skipped_unchanged += 1
        return

    # Change (or first sight) → hash.
    try:
        file_hash = _hash_file(path, cfg.retry_attempts, cfg.retry_backoff_s)
    except OSError as e:
        _log_problem(conn, path, str(e))
        report.errors += 1
        report.consecutive_errors += 1
        return

    # Extract + NFC (done inside extractor) + quality score.
    try:
        extracted = cfg.extractor(path)
    except Exception as e:  # log + skip; DO NOT record sweep_state, so a
        _log_problem(conn, path, f"extract: {e}")  # subsequent sweep retries
        report.errors += 1                          # (§7.1 "skip + log",
        report.consecutive_errors += 1              # §6 resumability).
        return

    # Made it past stat + hash + extract — reset the disconnect counter.
    report.consecutive_errors = 0

    quality: float | None = None
    if extracted is None or extracted.is_image_only:
        text_status = "image_only"
        report.image_only += 1
    else:
        # §5/§12.3: persist raw text per (file_hash, extract_version).
        _acc(conn).sweep_state.writes.cache_extract(file_hash, cfg.extract_version, extracted.text)
        # Persist per-page text alongside the joined text (training-corpus material).
        store_page_texts(conn, file_hash, cfg.extract_version, extracted.page_texts)
        # §4.8c step 2: validate on raw NFC text BEFORE any FTS folding.
        qr = score_text(extracted.text)
        quality = qr.score
        if qr.score >= cfg.quality_threshold:
            text_status = "ocr_good"
            report.ocr_good += 1
        else:
            text_status = "ocr_poor"
            report.ocr_poor += 1
            _acc(conn).review.writes.enqueue("low_quality_ocr", {
                "path": str(path),
                "file_hash": file_hash,
                "score": qr.score,
                "garbage_ratio": qr.garbage_ratio,
                "alpha_ratio": qr.alpha_ratio,
                "avg_word_len": qr.avg_word_len,
                "suspect_substitutions": qr.suspect_substitutions,
                "char_count": qr.char_count,
            })

    # Idempotent upsert by file_hash: a moved file updates path, not duplicates.
    _upsert_holding(conn, str(path), path.stem, file_hash, quality, text_status, report)

    # Final: record sweep_state so a re-run skips this file unchanged.
    _acc(conn).sweep_state.writes.record(str(path), st.st_size, st.st_mtime, file_hash)
    conn.commit()  # per-file commit → interruption is resumable (§6, §13)


def _upsert_holding(conn, path: str, stem: str, file_hash, quality, text_status,
                    report: "SweepReport") -> None:
    """Idempotent holding upsert by file_hash, shared by the serial + parallel sweep paths: a moved
    file updates its path (not a duplicate); an unknown file mints a fresh edition + holding."""
    acc = _acc(conn)
    existing = acc.holdings.reads.by_file_hash(file_hash)
    if existing:
        if existing[1] != path:
            acc.holdings.writes.set_file_path(existing[0], path)
            report.updated_paths += 1
        # Refresh status/score in case quality changed (e.g. extractor bump).
        acc.holdings.writes.set_columns(
            existing[0], {"text_status": text_status, "ocr_quality_score": quality})
    else:
        edition_id = acc.editions.writes.create({"title": stem}).target.id
        from catalogue.services import subjects as S
        S.ensure_categorized(conn, "edition", edition_id)   # never subject-less; review later
        from catalogue.services.mount import owning_root_id
        acc.holdings.writes.insert_holding(
            edition_id=edition_id, form="electronic", file_path=path, file_hash=file_hash,
            ocr_quality_score=quality, text_status=text_status, root_id=owning_root_id(path))
        report.new_holdings += 1


def _log_problem(conn, path: Path, msg: str) -> None:
    _acc(conn).sweep_state.writes.log_problem(str(path), msg)
    conn.commit()


# ── Parent-side writer (used by the parallel path) ────────────────────────
def _apply_result(conn, cfg: SweepConfig, r: WorkerResult,
                  report: SweepReport) -> None:
    """Write one `WorkerResult` to the DB. All SQL lives on the parent —
    workers never touch SQLite, so there is no WAL-writer contention,
    no `database is locked`, and no holding-upsert race."""
    path = Path(r.path)

    if r.error is not None:
        _log_problem(conn, path, r.error)
        report.errors += 1
        report.consecutive_errors += 1
        return

    # Worker finished cleanly → reset the disconnect counter.
    report.consecutive_errors = 0

    quality: float | None = None
    if r.is_image_only:
        text_status = "image_only"
        report.image_only += 1
    else:
        _acc(conn).sweep_state.writes.cache_extract(r.file_hash, cfg.extract_version, r.raw_text)
        store_page_texts(conn, r.file_hash, cfg.extract_version, r.page_texts)
        quality = r.quality_score
        if (quality or 0.0) >= cfg.quality_threshold:
            text_status = "ocr_good"
            report.ocr_good += 1
        else:
            text_status = "ocr_poor"
            report.ocr_poor += 1
            _acc(conn).review.writes.enqueue("low_quality_ocr", {
                "path": r.path,
                "file_hash": r.file_hash,
                "score": quality,
                "garbage_ratio": r.garbage_ratio,
                "alpha_ratio": r.alpha_ratio,
                "avg_word_len": r.avg_word_len,
                "suspect_substitutions": r.suspect_substitutions,
                "char_count": r.char_count,
            })

    _upsert_holding(conn, r.path, path.stem, r.file_hash, quality, text_status, report)
    _acc(conn).sweep_state.writes.record(r.path, r.size, r.mtime, r.file_hash)
    conn.commit()


# ── Entry points ──────────────────────────────────────────────────────────
def sweep(conn, cfg: SweepConfig, *, workers: int = 1) -> SweepReport:
    """Run the sweep. `workers=1` (default) uses the serial path — same
    semantics as before, and the only path that respects a custom
    `cfg.extractor` (closures don't pickle across the pool boundary).

    `workers>1` dispatches read-side work (hash + extract + quality
    score) to a `multiprocessing.Pool` and keeps all SQL on the parent.
    Avoids SQLite writer contention by construction; the bottleneck
    becomes the WebDAV mount's throughput rather than the DB lock.
    """
    _warn_if_pdf_extractor_missing(cfg)
    if workers > 1:
        report = _sweep_parallel(conn, cfg, workers=workers)
    else:
        report = _sweep_serial(conn, cfg)
    # Phone-drop post-pass: the walk above has ingested any media files dropped in
    # `_inbox/`; now apply their JSON sidecars (metadata + review flag + OCR). A
    # bare connect() path (no `_inbox/`) is a no-op. Kept best-effort so a sidecar
    # problem can't fail the whole sweep.
    try:
        from . import inbox
        report.inbox = inbox.apply_inbox_sidecars(conn, cfg)
    except Exception:
        pass
    # Catch-all: key any edition that gained an ISBN (sidecar above, or
    # edition_resolve / manual edits since the last run) so cross-format scan
    # matching keeps working. No-op unless a work-key fetch is configured.
    if cfg.work_key_fetch is not None:
        try:
            from . import intake_match
            intake_match.backfill_work_keys(conn, fetch=cfg.work_key_fetch)
        except Exception:
            pass
    # Wishlist post-pass: a book just ingested from the filesystem may fulfil (or weakly match) a
    # wishlist item — reconcile it here (write-side), so GET /api/v1/wishlist stays read-only.
    try:
        from . import wishlist_reconcile
        wishlist_reconcile.reconcile_acquisitions(conn)
    except Exception:
        pass
    # Same for the capture inbox: a scan whose book just got ingested is now held, so clear it
    # out of the inbox (and the Capture pill) — it surfaces in the capture log's "Added" section.
    try:
        from . import capture_reconcile
        capture_reconcile.reconcile_captures(conn)
    except Exception:
        pass
    return report


# ── Reprocess: clear state for holdings the sweep wasn't done with ────────
# `image_only`, `ocr_poor`, `none`, or NULL all mean "we couldn't finish
# the read side for this file." The sweep's change-detect by
# (path, size, mtime) means a re-run skips these files as "unchanged" —
# so when an upstream fix (e.g. installing PyMuPDF, raising the quality
# threshold, fixing the scorer) makes them processable, you need a way
# to force re-extract WITHOUT nuking the whole DB.

# Statuses that mean "incomplete work" — eligible for reprocess.
_INCOMPLETE_STATUSES = ("image_only", "ocr_poor", "none")


@dataclass
class ReprocessReport:
    holdings_targeted: int = 0
    sweep_state_cleared: int = 0
    raw_extract_cache_cleared: int = 0
    review_queue_cleared: int = 0


def count_by_status(conn) -> dict[str, int]:
    """Return `{status: count}` for every holding whose work is
    incomplete. NULL appears under the key `'NULL'`. Stable order
    (`_INCOMPLETE_STATUSES` + 'NULL') so the CLI prompt is repeatable."""
    counts = _acc(conn).holdings.reads.text_status_counts()
    out: dict[str, int] = {s: counts.get(s, 0) for s in _INCOMPLETE_STATUSES}
    out["NULL"] = counts.get(None, 0)
    return out


def reset_for_reprocess(conn, statuses: tuple[str, ...] = _INCOMPLETE_STATUSES,
                        *, include_null: bool = True) -> ReprocessReport:
    """Clear the rows that would otherwise make the next sweep skip
    incomplete-work files. Preserves the `holding` row itself (so manual
    edits — edition links, shelf locations — survive) and the
    `sweep_problem_log` (so the audit trail is intact).

    Rows deleted:
      - `sweep_state` for these holdings' file paths → next sweep
        re-hashes + re-extracts them.
      - `raw_extract_cache` for these holdings' file hashes → no stale
        text shadows the fresh extract.
      - `review_queue.low_quality_ocr` items pointing at these file
        hashes → no duplicate queue rows on the next run.
    """
    # An empty `statuses` + `include_null=False` is a no-op (the engine read returns []).
    acc = _acc(conn)
    rows = acc.holdings.reads.by_text_status(statuses, include_null)
    rep = ReprocessReport(holdings_targeted=len(rows))
    if not rows:
        return rep

    paths = [r[0] for r in rows if r[0]]
    hashes = [r[1] for r in rows if r[1]]
    if paths:
        rep.sweep_state_cleared = acc.sweep_state.writes.delete_paths(paths)
    if hashes:
        rep.raw_extract_cache_cleared = acc.sweep_state.writes.delete_extract_cache(hashes)
        # Match low_quality_ocr items by file_hash in their JSON payload.
        rep.review_queue_cleared = acc.review.writes.delete_by_json_in(
            "low_quality_ocr", "$.file_hash", hashes)
    conn.commit()
    return rep


def _warn_if_pdf_extractor_missing(cfg: SweepConfig) -> None:
    """Soft-dep guard: if `.pdf` is in `cfg.suffixes` but PyMuPDF (`fitz`)
    isn't importable, every PDF will silently come back `is_image_only=
    True` (`extract.py:_extract_pdf` ImportError branch) — looks like a
    library full of unscanned books when actually we just can't read
    them. Warn LOUDLY once so this doesn't burn another sweep cycle."""
    if ".pdf" not in {s.lower() for s in cfg.suffixes}:
        return
    try:
        import fitz  # noqa: F401
        return
    except ImportError:
        pass
    import sys as _sys
    _sys.stderr.write(
        "WARNING: PyMuPDF (`fitz`) is not installed — every .pdf in "
        "this sweep will be recorded as `text_status='image_only'`, "
        "regardless of whether it has a real text layer.\n"
        "Install with:  pip3 install pymupdf\n"
        "Then re-run the sweep (the change-detect will skip unchanged "
        "EPUBs; clear sweep_state for .pdf rows to force re-extract).\n"
    )
    _sys.stderr.flush()


def _sweep_serial(conn, cfg: SweepConfig) -> SweepReport:
    """The original single-process path. Per-file errors log + continue;
    `cfg.max_consecutive_errors` triggers `SweepAborted`."""
    report = SweepReport()
    try:
        for path in walk(cfg.mount_root, cfg.suffixes):
            _process(conn, cfg, path, report)
            if cfg.progress is not None:
                cfg.progress(report, path)
            if report.consecutive_errors >= cfg.max_consecutive_errors:
                report.aborted = True
                raise SweepAborted(
                    f"{report.consecutive_errors} consecutive I/O errors — "
                    f"mount {cfg.mount_root} likely disconnected; "
                    "partial sweep_state preserved for resume"
                )
    finally:
        # Internal counter is not interesting to callers once the loop is done.
        report.consecutive_errors = 0
    return report


def _sweep_parallel(conn, cfg: SweepConfig, *, workers: int) -> SweepReport:
    """Parallel sweep: parent enumerates + filters via `sweep_state`,
    pool runs hash + extract + score per file, parent commits results.

    Resilience: the same `max_consecutive_errors` short-circuit applies.
    On abort we terminate the pool so we don't keep pulling files off a
    dead WebDAV mount. Partial sweep_state survives for resume.
    """
    import multiprocessing as _mp

    report = SweepReport()
    # Materialize the input list on the MAIN thread so every SQLite call
    # (the sweep_state filter, the problem-log writes for stat errors)
    # happens on the connection's owning thread. `imap_unordered` runs
    # its feeder in an internal helper thread; a generator that touches
    # `conn` would raise `SQLite objects created in a thread can only
    # be used in that same thread`. Cost is one stat + one indexed
    # SELECT per file — cheap compared to the hash + extract that follow.
    inputs = list(_enumerate_inputs(conn, cfg, report))

    # `spawn` is the macOS default and the only safe start method
    # post-fork. Explicit so a future Linux run doesn't silently inherit
    # an open SQLite handle into workers (would corrupt WAL).
    ctx = _mp.get_context("spawn")
    pool = ctx.Pool(processes=workers)
    try:
        for r in pool.imap_unordered(_worker_process, inputs, chunksize=1):
            report.scanned += 1
            _apply_result(conn, cfg, r, report)
            if cfg.progress is not None:
                cfg.progress(report, Path(r.path))
            if report.consecutive_errors >= cfg.max_consecutive_errors:
                report.aborted = True
                pool.terminate()
                raise SweepAborted(
                    f"{report.consecutive_errors} consecutive I/O errors — "
                    f"mount {cfg.mount_root} likely disconnected; "
                    "partial sweep_state preserved for resume"
                )
    finally:
        pool.close()
        pool.join()
        report.consecutive_errors = 0
    return report


def _enumerate_inputs(conn, cfg: SweepConfig, report: SweepReport):
    """Yield `_WorkerInput` for every file that needs re-processing.
    Filters out unchanged files via `sweep_state` here in the parent so
    workers don't waste a round trip on no-op work — and so workers
    never touch the DB at all. Inbox files are enumerated first (see `walk`)."""
    for path in walk(cfg.mount_root, cfg.suffixes):
        try:
            st = path.stat()
        except OSError as e:
            _log_problem(conn, path, f"stat: {e}")
            report.scanned += 1
            report.errors += 1
            report.consecutive_errors += 1
            if cfg.progress is not None:
                cfg.progress(report, path)
            continue
        if _acc(conn).sweep_state.reads.unchanged(str(path), st.st_size, st.st_mtime):
            report.scanned += 1
            report.skipped_unchanged += 1
            if cfg.progress is not None:
                cfg.progress(report, path)
            continue
        yield _WorkerInput(
            path=str(path), size=st.st_size, mtime=st.st_mtime,
            extract_version=cfg.extract_version,
            retry_attempts=cfg.retry_attempts,
            retry_backoff_s=cfg.retry_backoff_s,
        )


# ── CLI: interactive category picker ──────────────────────────────────────
def _prompt_for_categories(counts: dict[str, int],
                           ) -> tuple[tuple[str, ...], bool]:
    """Interactive category picker for `--incremental`.

    Shows holding counts per incomplete status and asks the user which
    to reprocess. Accepts comma-separated numbers (`1,2`), status names
    (`image_only,NULL`), `all`, or empty (= all). Statuses with zero
    holdings are still listed (so the prompt is repeatable across runs)
    but selecting them is harmless.

    Non-interactive fallback: when stdin is not a TTY we default to ALL
    categories rather than block forever on `input()`. The chosen set
    is echoed to stderr so the audit trail still shows what ran.
    """
    import sys as _sys
    categories = list(counts.keys())   # e.g. [image_only, ocr_poor, none, NULL]
    _sys.stderr.write("incremental: holdings flagged for reprocess\n")
    width = max(len(c) for c in categories)
    for i, key in enumerate(categories, 1):
        _sys.stderr.write(f"  [{i}] {key:<{width}}  ({counts[key]})\n")
    _sys.stderr.flush()

    if not _sys.stdin.isatty():
        _sys.stderr.write("  (stdin is not a TTY → defaulting to ALL)\n")
        return tuple(s for s in categories if s != "NULL"), True

    raw = input("Select categories (e.g. 1,2  or  image_only,NULL  or  Enter for all): ").strip()
    if not raw or raw.lower() == "all":
        return tuple(s for s in categories if s != "NULL"), True

    chosen: set[str] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit():
            idx = int(tok) - 1
            if 0 <= idx < len(categories):
                chosen.add(categories[idx])
                continue
        # Status-name match — case-insensitive, accepts the literal
        # "NULL" key too.
        for c in categories:
            if c.lower() == tok.lower():
                chosen.add(c)
                break
        else:
            _sys.stderr.write(f"  (ignoring unknown selection: {tok!r})\n")

    if not chosen:
        _sys.stderr.write("  (nothing selected → nothing to clear)\n")
        return (), False
    include_null = "NULL" in chosen
    statuses = tuple(s for s in chosen if s != "NULL")
    return statuses, include_null


# Last-resort default books directory. Resolution order (config, not constants —
# §13 / §12 rule 7): $CATALOGUE_MOUNT_ROOT → vocab.json `_library_root` (the
# operator-set mount root, edited via /settings) → this built-in default.
DEFAULT_MOUNT_ROOT = os.environ.get("CATALOGUE_MOUNT_ROOT_DEFAULT", "")


def default_mount_root() -> Path:
    """The books-tree root, resolved at call time so tests / shell sessions can
    override via `CATALOGUE_MOUNT_ROOT` and so an operator's /settings change to
    the configured mount root takes effect without a restart or monkey-patch.

    $CATALOGUE_MOUNT_ROOT (explicit override) > vocab.json `_library_root`
    (the single source of truth, set via /settings) > the built-in default."""
    import os as _os
    env = _os.environ.get("CATALOGUE_MOUNT_ROOT")
    if env:
        return Path(env)
    try:
        from .mount import current_mount_root
        root = current_mount_root()
        if root:
            return Path(root)
    except Exception:
        pass
    return Path(DEFAULT_MOUNT_ROOT)


if __name__ == "__main__":
    import argparse
    from catalogue.db_store import init_db

    ap = argparse.ArgumentParser(
        description="Sweep the books directory into the catalogue. "
                    "Mount root resolves as: --mount-root > "
                    "$CATALOGUE_MOUNT_ROOT > built-in default."
    )
    ap.add_argument("mount_root", type=Path, nargs="?", default=None,
                    help="Optional positional override (legacy form).")
    ap.add_argument("--mount-root", dest="mount_root_flag", type=Path,
                    default=None)
    ap.add_argument("--db", default=default_db_path())
    ap.add_argument("--threshold", type=float, default=0.6)
    ap.add_argument("--workers", type=int, default=5,
                    help="Parallel worker processes for hash+extract+score. "
                         "Default 5; pass 1 for the original serial path. "
                         "All SQL stays on the parent regardless.")
    ap.add_argument(
        "--incremental", action="store_true",
        help="Before sweeping, clear sweep_state + raw_extract_cache + "
             "stale review_queue rows for every holding whose text_status "
             "is one of (image_only, ocr_poor, none, NULL). The next sweep "
             "re-extracts those files; ocr_good holdings stay skipped. "
             "Holdings, editions, manual edits and sweep_problem_log are "
             "preserved.",
    )
    ap.add_argument(
        "--inbox-only", action="store_true",
        help="Skip the full walk; only run the _inbox/ sidecar post-pass "
             "(apply phone-dropped metadata to already-swept holdings).",
    )
    args = ap.parse_args()

    root = args.mount_root_flag or args.mount_root or default_mount_root()
    if not root or str(root) in (".", ""):
        raise SystemExit(
            "No library books directory is configured. Set $CATALOGUE_MOUNT_ROOT (or "
            "$CATALOGUE_LIBRARY_ROOT) to your books folder, set the library root in the "
            "web Settings page, or pass --mount-root."
        )
    if not root.exists():
        raise SystemExit(
            f"Mount root does not exist: {root}\n"
            "Pass --mount-root, or set $CATALOGUE_MOUNT_ROOT."
        )

    import sys as _sys

    def _live_progress(rep: SweepReport, path: Path) -> None:
        """One-line live status to stderr. `\\r` overwrite keeps the
        terminal quiet when the sweep is sweeping (~450 files), but
        falls back to newline-per-file when stderr is not a TTY (logs,
        CI) so progress survives in a captured log."""
        name = path.name[:60]
        line = (
            f"scanned={rep.scanned:>5}  new={rep.new_holdings:>4}  "
            f"unchanged={rep.skipped_unchanged:>4}  "
            f"good={rep.ocr_good:>4}  poor={rep.ocr_poor:>4}  "
            f"image_only={rep.image_only:>3}  err={rep.errors:>3}  "
            f"› {name}"
        )
        if _sys.stderr.isatty():
            _sys.stderr.write(f"\r\x1b[K{line}")
        else:
            _sys.stderr.write(line + "\n")
        _sys.stderr.flush()

    conn = init_db(args.db)

    if args.inbox_only:
        from . import inbox, intake_match
        from .isbn import make_fetch as _wk_fetch
        rep = inbox.apply_inbox_sidecars(
            conn, SweepConfig(mount_root=root, quality_threshold=args.threshold))
        intake_match.backfill_work_keys(conn, fetch=_wk_fetch())
        print(rep)
        raise SystemExit(0)

    if args.incremental:
        counts = count_by_status(conn)
        # All-or-nothing if no incomplete holdings exist — say so and skip
        # the prompt rather than ask the user to pick from an empty menu.
        if sum(counts.values()) == 0:
            _sys.stderr.write("incremental: no holdings with incomplete work — nothing to reprocess.\n")
            statuses = ()
            include_null = False
        else:
            statuses, include_null = _prompt_for_categories(counts)
        rp = reset_for_reprocess(conn, statuses=statuses,
                                 include_null=include_null)
        _sys.stderr.write(
            f"incremental: {rp.holdings_targeted} holdings flagged → "
            f"cleared sweep_state={rp.sweep_state_cleared}, "
            f"raw_extract_cache={rp.raw_extract_cache_cleared}, "
            f"review_queue={rp.review_queue_cleared}\n"
        )

    from .isbn import make_fetch as _wk_fetch
    try:
        rep = sweep(conn,
                    SweepConfig(mount_root=root,
                                quality_threshold=args.threshold,
                                progress=_live_progress,
                                work_key_fetch=_wk_fetch()),
                    workers=args.workers)
        if _sys.stderr.isatty():
            _sys.stderr.write("\n")
        print(rep)
    except SweepAborted as e:
        if _sys.stderr.isatty():
            _sys.stderr.write("\n")
        print(f"sweep aborted: {e}")
        raise SystemExit(2)
