"""Step 6 — the `Digitizer` interface and its default implementations
(§4.8, §8). One contract; backends swap by config (§12.1).

Default: `OCRmyPDFDigitizer` invokes the `ocrmypdf` CLI in-process via
subprocess. `--redo-ocr` is the default (lossless masked re-OCR);
`--force-ocr` is the controlled fallback when redo can't detect the
existing layer (rasterizes, lossy — §4.8a). Tesseract loads `eng` plus
the Shreeshrii community IAST traineddata so diacritics survive
(`san` is forbidden — Devanagari hallucinations, §4.8a / §13).

Alternative: `ABBYYImportDigitizer` — ABBYY on Mac is GUI-only (§4.8b),
so we don't automate it. Instead the operator runs ABBYY by hand,
exports a PDF/A, and this digitizer ingests the result through the same
pipeline: extract text, NFC-normalize, re-score, update the holding.

The orchestrator `digitize_holding()` is what callers use. It honours
§6 (cache-version awareness, local-copy-before-heavy-processing) and
§4.8c (NFC then validate before any indexing).
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

from catalogue.db_store import nfc
from .extract import ExtractedText, extract
from .ocr_route import count_foreign_diacritics, plan_escalation
from .quality import score_text


def _acc(conn):
    """A system Access over this connection — engine-routed holding reads/writes + the review
    queue. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(conn)


# Read text from a finished archival PDF. Injectable so tests don't need
# PyMuPDF and don't need to materialize real PDFs.
ExtractFn = Callable[[Path], Optional[ExtractedText]]


# ── Result + interface ────────────────────────────────────────────────────


@dataclass(frozen=True)
class DigitizeResult:
    """One digitize attempt's output. `digitizer_used` is the
    `digitizer_kind` code recorded on `holding`."""
    archival_pdf_path: Path
    text: str                       # NFC, diacritics intact (§4.8c step 1)
    digitizer_used: str
    page_count: Optional[int] = None
    force_used: bool = False        # True iff the `--force-ocr` fallback ran
    # M3 audit trail: when the digitizer processed a DIFFERENT file than
    # the holding's `file_path` (e.g. an ABBYY-exported PDF/A), record
    # both. None = no substitution; the orchestrator used the holding's
    # file directly. Surface via `holding.notes` or review queue so the
    # operator never wonders which file landed in the archive.
    substituted_from: Optional[Path] = None
    # Per-page NFC text from the freshly-OCRed archival PDF — feeds the
    # Step-6 router (§4.8d). None when the backend can't supply it.
    page_texts: Optional[tuple[str, ...]] = None


class Digitizer(Protocol):
    """*input* scanned PDF / page images → *output* searchable PDF/A-2b +
    extracted text + per-page confidence (§4.8b). Implementations write
    the archival PDF locally; never to the WebDAV mount (§6, §13)."""

    kind: str

    def digitize(self, src_pdf: Path, out_dir: Path) -> DigitizeResult: ...


# ── OCRmyPDF default ──────────────────────────────────────────────────────

# Injectable subprocess runner — keeps tests hermetic. Returns the
# CompletedProcess so the digitizer can branch on exit code without
# pretending it knows what `ocrmypdf` will do.
RunFn = Callable[[list[str]], subprocess.CompletedProcess]


def _default_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


# OCRmyPDF exits 6 when `--redo-ocr` can't detect an existing text layer
# to strip. That's the documented signal that `--force-ocr` is the only
# remaining path. We do NOT treat it as a hard failure — we fall back,
# log via `force_used=True`, and let downstream see the (lossy) result.
_REDO_NEEDS_FORCE_EXIT = 6


@dataclass
class OCRmyPDFDigitizer:
    """Default backend (§4.8a). Subprocess so the heavy native
    dependency stays out of import-time; the `ocrmypdf` binary is
    expected on PATH at runtime, not at test time."""

    # `eng` + Shreeshrii community IAST traineddata; NEVER `san`
    # (Devanagari hallucinations — §4.8a). Latin script model would be
    # `Latin`, not `Latn` (§13). Tesseract resolves languages by the
    # traineddata file STEM in TESSDATA_PREFIX — the Shreeshrii repo
    # `tesstrain-Sanskrit-IAST` ships `tessdata_best/IAST.traineddata`,
    # so the code is literally `IAST`. Passing `Shreeshrii` (vendor name)
    # would fail with 'Failed loading language' at runtime.
    languages: tuple[str, ...] = ("eng", "IAST")
    # `--redo-ocr` is lossless and preferred (§4.8a [v5]).
    redo_by_default: bool = True
    # `--force-ocr` rasterizes; allow it only as a controlled fallback.
    allow_force_fallback: bool = True
    binary: str = "ocrmypdf"
    run_fn: RunFn = field(default=_default_run)
    extract_fn: ExtractFn = field(default=extract)
    kind: str = "ocrmypdf_tesseract"

    def _base_cmd(self, src: Path, dst: Path) -> list[str]:
        return [
            self.binary,
            "--output-type", "pdfa-2",          # PDF/A-2b (§4.8)
            "-l", "+".join(self.languages),
            str(src), str(dst),
        ]

    def digitize(self, src_pdf: Path, out_dir: Path) -> DigitizeResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / (src_pdf.stem + ".pdfa.pdf")
        force_used = False

        if self.redo_by_default:
            cmd = self._base_cmd(src_pdf, dst) + ["--redo-ocr"]
            r = self.run_fn(cmd)
            if r.returncode == _REDO_NEEDS_FORCE_EXIT and self.allow_force_fallback:
                # Documented case: no detectable text layer to strip.
                # Fall back to `--force-ocr` (lossy: rasterizes pages).
                cmd = self._base_cmd(src_pdf, dst) + ["--force-ocr"]
                r = self.run_fn(cmd)
                force_used = True
            elif r.returncode != 0:
                raise RuntimeError(
                    f"ocrmypdf failed (exit {r.returncode}): {r.stderr.strip()}"
                )
        else:
            cmd = self._base_cmd(src_pdf, dst) + ["--force-ocr"]
            r = self.run_fn(cmd)
            force_used = True
            if r.returncode != 0:
                raise RuntimeError(
                    f"ocrmypdf --force-ocr failed (exit {r.returncode}): "
                    f"{r.stderr.strip()}"
                )

        # §4.8c step 1: NFC-normalize the resulting text before anything
        # else looks at it (matching, quality scoring, FTS).
        extracted = self.extract_fn(dst)
        text = extracted.text if extracted else ""
        pages = extracted.page_count if extracted else None
        return DigitizeResult(
            archival_pdf_path=dst,
            text=text,
            digitizer_used=self.kind,
            page_count=pages,
            force_used=force_used,
            page_texts=extracted.page_texts if extracted else None,
        )


# ── ABBYY import (manual on Mac) ──────────────────────────────────────────


@dataclass
class ABBYYImportDigitizer:
    """ABBYY on Mac is GUI-only (§4.8b). The operator runs ABBYY by hand,
    exports a searchable PDF/A, and points this digitizer at the result.
    We copy the file under our managed archival dir, extract the text,
    and the orchestrator handles the rest — identical downstream path.

    `digitize(src_pdf, out_dir)` is the protocol entry point: `src_pdf`
    is the ABBYY-produced PDF/A to import. The optional `source_pdf`
    attribute is a backward-compatible default used only when no
    `src_pdf` is passed; if both are supplied they MUST agree, otherwise
    a different holding's file could get stamped onto this row (M3)."""

    source_pdf: Optional[Path] = None
    extract_fn: ExtractFn = field(default=extract)
    kind: str = "abbyy_import"

    def digitize(self, src_pdf: Path, out_dir: Path) -> DigitizeResult:
        """M3: `src_pdf` is the holding's file (what the orchestrator
        thought it was processing); `self.source_pdf` is the ABBYY
        export the operator wants stamped on the archive. The two are
        DELIBERATELY DIFFERENT on the manual ABBYY path — that's the
        whole point. We use `self.source_pdf` when set, but record the
        substitution in the result so the operator never wonders which
        file landed in the archive."""
        chosen = (
            Path(self.source_pdf) if self.source_pdf is not None
            else (Path(src_pdf) if src_pdf is not None else None)
        )
        if chosen is None:
            raise RuntimeError(
                "ABBYYImportDigitizer needs a source PDF — set "
                "source_pdf to the ABBYY export, or pass src_pdf."
            )
        if not chosen.exists():
            raise RuntimeError(f"ABBYY source not found: {chosen}")
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / chosen.name
        shutil.copy2(chosen, dst)
        ext = self.extract_fn(dst)
        substituted = (
            Path(src_pdf) if (src_pdf is not None
                              and Path(src_pdf).resolve() != chosen.resolve())
            else None
        )
        return DigitizeResult(
            archival_pdf_path=dst,
            text=ext.text if ext else "",
            digitizer_used=self.kind,
            page_count=ext.page_count if ext else None,
            force_used=False,
            substituted_from=substituted,
            page_texts=ext.page_texts if ext else None,
        )


# ── Orchestrator ──────────────────────────────────────────────────────────


@dataclass
class DigitizeConfig:
    """Knobs are config, not constants (§12 rule 7)."""
    quality_threshold: float = 0.6
    archival_dir: Path = Path("archival")
    # §6 / §13: copy files off the WebDAV mount before heavy processing.
    # Tests pass `local_copy_dir=None` to skip the copy when the source
    # is already on local disk.
    local_copy_dir: Optional[Path] = None
    import_source: Optional[Path] = None     # ABBYY path-through, if used
    # §4.8d / §9 bake-off: after the local Tesseract pass, route
    # diacritic-relevant pages to the high-accuracy Cloud-Vision engine
    # (recall 55–78% vs ~25% local). `escalate` toggles the routing;
    # `local_kinds` are the backends whose output is worth escalating
    # (a Cloud-Vision result is already high-recall — don't re-route it).
    # NOTE: this records the escalation PLAN as a review item; the actual
    # per-page GCV re-OCR + hOCR/OcrElement merge into the PDF is the next
    # increment (§4.8a adapter), gated behind a real GCV backend.
    escalate: bool = True
    local_kinds: tuple[str, ...] = ("ocrmypdf_tesseract",)
    # Cache-key part for the per-page OCR text persisted to page_text_cache;
    # matches SweepConfig.extract_version so downstream "latest version" reads
    # find the OCR text where there was none for an image-only file.
    extract_version: int = 1


@dataclass
class DigitizeReport:
    holding_id: int
    digitizer_used: str
    archival_pdf_path: Path
    quality_score: float
    text_status: str            # post-digitize status written to holding
    force_used: bool
    queued_low_quality: bool
    # §4.8d Cloud-Vision routing: 0-based page indices recommended for the
    # high-accuracy pass, and whether that recommendation was queued.
    escalation_pages: list[int] = field(default_factory=list)
    queued_escalation: bool = False
    # valid-IAST filter (§4.8c): count of non-IAST Latin diacritics in the
    # result — the Cloud-Vision substitution signature. ~0 for Tesseract.
    foreign_diacritics: int = 0


def _stage_locally(src: Path, cfg: DigitizeConfig) -> Path:
    """Copy WebDAV → local working dir; no-op if `local_copy_dir` is None."""
    if cfg.local_copy_dir is None:
        return src
    cfg.local_copy_dir.mkdir(parents=True, exist_ok=True)
    dst = cfg.local_copy_dir / src.name
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dst)
    return dst


def digitize_holding(
    conn,
    holding_id: int,
    digitizer: Digitizer,
    cfg: Optional[DigitizeConfig] = None,
) -> DigitizeReport:
    """Run one holding through the chosen Digitizer, update the holding
    row, and queue review if the post-digitize quality is still poor.
    Idempotent against the holding columns (re-runs overwrite)."""
    cfg = cfg or DigitizeConfig()

    row = _acc(conn).holdings.reads.ocr_fields(holding_id)
    if row is None:
        raise ValueError(f"holding {holding_id} not found")
    file_path_str, _prior_status, file_hash = row
    if not file_path_str:
        raise ValueError(f"holding {holding_id} has no file_path")
    src = Path(file_path_str)
    local_src = _stage_locally(src, cfg)

    result = digitizer.digitize(local_src, cfg.archival_dir)

    # §4.8c: NFC then quality score on raw text BEFORE any FTS folding (§6).
    text = nfc(result.text)
    quality = score_text(text)
    status = "ocr_good" if quality.score >= cfg.quality_threshold else "ocr_poor"

    _acc(conn).holdings.writes.set_columns(holding_id, {
        "text_status": status, "ocr_quality_score": quality.score,
        "archival_pdf_path": str(result.archival_pdf_path),
        "digitizer_used": result.digitizer_used})

    # Persist the fresh OCR per-page text durably (training-corpus material) —
    # the only place an image-only file's page text becomes available, since the
    # sweep skipped page_text_cache for it. Keyed by the holding's file_hash.
    if file_hash:
        from .sweep import store_page_texts
        store_page_texts(conn, file_hash, cfg.extract_version, result.page_texts)

    if result.substituted_from is not None:
        # M3 audit: the archival PDF came from a different file than the
        # holding's file_path (e.g. ABBYY manual export). Surface this in
        # the review queue so an operator can verify the substitution was
        # intended — never silent.
        _acc(conn).review.writes.enqueue("low_confidence_extraction", _payload({
            "holding_id": holding_id,
            "kind": "archival_source_substitution",
            "holding_file": str(result.substituted_from),
            "archival_source": str(result.archival_pdf_path),
            "digitizer_used": result.digitizer_used,
        }))

    queued = False
    if status == "ocr_poor":
        # §4.8c step 2 + §6: failures land in the review queue, not in any
        # cache, so a later re-OCR with a tuned backend can pick them up.
        _acc(conn).review.writes.enqueue("low_quality_ocr", _payload({
            "holding_id": holding_id,
            "score": quality.score,
            "digitizer_used": result.digitizer_used,
            "force_used": result.force_used,
            "suspect_substitutions": quality.suspect_substitutions,
        }))
        queued = True

    # §4.8d / §9 bake-off — route diacritic-relevant pages to Cloud Vision.
    # Run only on a LOCAL backend's fresh per-page output (never a stale text
    # layer; a Cloud-Vision result is already high-recall, so don't re-route).
    # Independent of the quality gate: a page can score "good" yet still have
    # lost most diacritics — that is exactly what escalation is for.
    escalation_pages: list[int] = []
    queued_escalation = False
    foreign = count_foreign_diacritics(text)
    if cfg.escalate and result.digitizer_used in cfg.local_kinds:
        escalation_pages = plan_escalation(result.page_texts)
        if escalation_pages:
            # Record the plan as an actionable review item. (Actually running
            # the per-page GCV re-OCR + hOCR merge is the next increment.)
            _acc(conn).review.writes.enqueue("low_quality_ocr", _payload({
                "holding_id": holding_id,
                "kind": "cloud_vision_escalation",
                "digitizer_used": result.digitizer_used,
                "pages": escalation_pages,
                "page_count": len(escalation_pages),
            }))
            queued_escalation = True
    conn.commit()

    return DigitizeReport(
        holding_id=holding_id,
        digitizer_used=result.digitizer_used,
        archival_pdf_path=result.archival_pdf_path,
        quality_score=quality.score,
        text_status=status,
        force_used=result.force_used,
        queued_low_quality=queued,
        escalation_pages=escalation_pages,
        queued_escalation=queued_escalation,
        foreign_diacritics=foreign,
    )


def find_re_ocr_candidates(conn) -> list[int]:
    """Holdings the sweep flagged for re-OCR or that have no usable text
    layer (§4.8d, §7 step 6). Caller iterates these into `digitize_holding`."""
    return _acc(conn).holdings.reads.ids_by_text_status(
        ("ocr_poor", "image_only", "none"))


def _payload(d: dict) -> str:
    import json
    return json.dumps(d, sort_keys=True)
