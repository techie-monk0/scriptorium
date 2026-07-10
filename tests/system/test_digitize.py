"""§4.8 / §8 Step 6 — Digitizer interface and orchestrator.

Hermetic: `ocrmypdf` is never executed. `run_fn` is mocked; the result
of "extraction from the archival PDF" is also injected, so PyMuPDF is
not required.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from catalogue.db_store import init_db
from catalogue.services.digitize import (
    ABBYYImportDigitizer, DigitizeConfig, OCRmyPDFDigitizer,
    digitize_holding, find_re_ocr_candidates,
)
from catalogue.services.extract import ExtractedText


# ── Helpers ──────────────────────────────────────────────────────────────


def _completed(rc: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout="", stderr=stderr)


def _fake_extract(text: str):
    """Return an injectable that ignores the path and yields fixed text."""
    def _fn(_p):
        return ExtractedText(text=text, page_count=10, producer="ocrmypdf",
                             is_image_only=not text.strip())
    return _fn


def _fake_extract_pages(pages: list[str]):
    """Injectable yielding per-page text (drives the §4.8d router)."""
    def _fn(_p):
        joined = "\n".join(pages)
        return ExtractedText(text=joined, page_count=len(pages),
                             producer="ocrmypdf", is_image_only=not joined.strip(),
                             page_texts=tuple(pages))
    return _fn


def _setup_db(tmp_path: Path, *, file_path: Path, text_status="ocr_poor"):
    db_path = tmp_path / "digitize.db"
    init_db(db_path).close()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO edition (title) VALUES ('T')")
    conn.execute(
        "INSERT INTO holding (edition_id, form, file_path, text_status, "
        "                     ocr_quality_score) "
        "VALUES (?, 'electronic', ?, ?, ?)",
        (1, str(file_path), text_status, 0.2),
    )
    conn.commit()
    return conn


# ── OCRmyPDF backend — command construction & flag policy ────────────────


def test_ocrmypdf_redo_ocr_is_default_command():
    """§4.8a [v5]: `--redo-ocr` is lossless and the default. The command
    must include it; `--force-ocr` must NOT appear on a happy-path run."""
    captured: list[list[str]] = []

    def _run(cmd):
        captured.append(cmd)
        return _completed(0)

    d = OCRmyPDFDigitizer(
        run_fn=_run, extract_fn=_fake_extract("Bodhicaryāvatāra " * 200),
    )
    d.digitize(Path("/tmp/in.pdf"), Path("/tmp/out"))

    cmd = captured[0]
    assert "--redo-ocr" in cmd
    assert "--force-ocr" not in cmd
    assert "--output-type" in cmd and "pdfa-2" in cmd
    # Tesseract language string: `eng+IAST` — Shreeshrii's repo
    # `tesstrain-Sanskrit-IAST` ships `tessdata_best/IAST.traineddata`,
    # so the `-l` code is the file stem `IAST`. Vendor name `Shreeshrii`
    # would fail to load. NEVER `san` (§4.8a/§13: Devanagari).
    assert "-l" in cmd and "eng+IAST" in cmd
    assert not any(tok == "san" or tok.endswith("+san") or "+san+" in tok
                   for tok in cmd)


def test_ocrmypdf_force_fallback_only_after_redo_signals_no_layer():
    """OCRmyPDF exit 6 = `--redo-ocr` found no text layer to strip.
    That is the only signal that justifies the lossy `--force-ocr` rerun."""
    calls: list[list[str]] = []

    def _run(cmd):
        calls.append(cmd)
        # First call: redo → exit 6. Second call: force → success.
        return _completed(6) if len(calls) == 1 else _completed(0)

    d = OCRmyPDFDigitizer(
        run_fn=_run, extract_fn=_fake_extract("Bodhicaryāvatāra " * 200),
    )
    result = d.digitize(Path("/tmp/in.pdf"), Path("/tmp/out"))

    assert len(calls) == 2
    assert "--redo-ocr" in calls[0] and "--force-ocr" not in calls[0]
    assert "--force-ocr" in calls[1] and "--redo-ocr" not in calls[1]
    assert result.force_used is True


def test_ocrmypdf_does_not_force_when_redo_succeeds():
    """Sanity guard: a clean redo exit must NEVER trigger the rasterizing
    fallback (that path is lossy — §4.8a)."""
    calls: list[list[str]] = []
    d = OCRmyPDFDigitizer(
        run_fn=lambda cmd: calls.append(cmd) or _completed(0),
        extract_fn=_fake_extract("text " * 200),
    )
    result = d.digitize(Path("/tmp/in.pdf"), Path("/tmp/out"))
    assert len(calls) == 1
    assert result.force_used is False


def test_ocrmypdf_non_redo_error_bubbles_up():
    """Any non-6 non-zero exit is a real error — don't silently fall
    back, surface it."""
    d = OCRmyPDFDigitizer(
        run_fn=lambda _: _completed(2, stderr="bad pdf"),
        extract_fn=_fake_extract(""),
    )
    with pytest.raises(RuntimeError, match="bad pdf"):
        d.digitize(Path("/tmp/in.pdf"), Path("/tmp/out"))


def test_ocrmypdf_force_fallback_can_be_disabled():
    """Operator-controlled: if `allow_force_fallback=False`, the digitizer
    raises on exit 6 instead of rasterizing the input."""
    d = OCRmyPDFDigitizer(
        run_fn=lambda _: _completed(6, stderr="no text to redo"),
        extract_fn=_fake_extract(""),
        allow_force_fallback=False,
    )
    with pytest.raises(RuntimeError):
        d.digitize(Path("/tmp/in.pdf"), Path("/tmp/out"))


# ── Orchestrator — DB updates, quality regate, review queue ──────────────


def test_digitize_holding_marks_good_when_quality_passes(tmp_path):
    src = tmp_path / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 stub")
    conn = _setup_db(tmp_path, file_path=src)

    d = OCRmyPDFDigitizer(
        run_fn=lambda _: _completed(0),
        # Clean prose → quality.score_text returns ≥ 0.6.
        extract_fn=_fake_extract(
            "The Bodhisattva path requires great compassion and wisdom. " * 40
        ),
    )
    cfg = DigitizeConfig(archival_dir=tmp_path / "archival")
    report = digitize_holding(conn, holding_id=1, digitizer=d, cfg=cfg)

    assert report.text_status == "ocr_good"
    assert report.queued_low_quality is False

    row = conn.execute(
        "SELECT text_status, ocr_quality_score, archival_pdf_path, digitizer_used "
        "FROM holding WHERE id = 1"
    ).fetchone()
    assert row[0] == "ocr_good"
    assert row[1] >= 0.6
    assert row[2].endswith(".pdfa.pdf")
    assert row[3] == "ocrmypdf_tesseract"


def test_digitize_holding_queues_review_when_quality_still_poor(tmp_path):
    """Re-OCR doesn't always win — if quality is still poor, the holding
    lands back in the review queue (§4.8c step 2 / §6)."""
    src = tmp_path / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 stub")
    conn = _setup_db(tmp_path, file_path=src)

    d = OCRmyPDFDigitizer(
        run_fn=lambda _: _completed(0),
        extract_fn=_fake_extract("���\x00\x00\x00 g a r b a g e"),
    )
    cfg = DigitizeConfig(archival_dir=tmp_path / "archival")
    report = digitize_holding(conn, holding_id=1, digitizer=d, cfg=cfg)

    assert report.text_status == "ocr_poor"
    assert report.queued_low_quality is True
    qrow = conn.execute(
        "SELECT item_type, payload_json FROM review_queue "
        "WHERE item_type = 'low_quality_ocr'"
    ).fetchone()
    assert qrow is not None
    payload = json.loads(qrow[1])
    assert payload["holding_id"] == 1
    assert payload["digitizer_used"] == "ocrmypdf_tesseract"


def test_digitize_holding_records_force_used_in_review_payload(tmp_path):
    """When the controlled `--force-ocr` fallback ran, the review queue
    payload must carry that signal so the operator can spot lossy archivals."""
    src = tmp_path / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 stub")
    conn = _setup_db(tmp_path, file_path=src)

    calls = []

    def _run(cmd):
        calls.append(cmd)
        return _completed(6) if len(calls) == 1 else _completed(0)

    d = OCRmyPDFDigitizer(
        run_fn=_run, extract_fn=_fake_extract("���\x00\x00\x00 g a r b a g e"),
    )
    report = digitize_holding(
        conn, 1, d,
        cfg=DigitizeConfig(archival_dir=tmp_path / "archival"),
    )
    assert report.force_used is True
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM review_queue "
        "WHERE item_type = 'low_quality_ocr'"
    ).fetchone()[0])
    assert payload["force_used"] is True


# ── §4.8d Cloud-Vision escalation routing ────────────────────────────────


_IAST_PAGE = ("Bhairavapadmāvatīkalpa oṃ hrīṃ hṛtkamale gajendravaśakaṃ "
              "sarvāṅgasandhiṣv māyām āvilikhet pariveṣṭya kroṃkāraiḥ " * 3)
_ENGLISH_PAGE = ("A plain English page of running prose with ordinary words "
                 "and nothing that needs the high accuracy engine at all. " * 4)


def test_escalation_queues_diacritic_pages_from_local_backend(tmp_path):
    """A fresh local Tesseract pass that emitted diacritic-relevant pages
    must queue a `cloud_vision_escalation` recommendation listing them —
    independent of the overall quality gate (§4.8d / §9)."""
    src = tmp_path / "scan.pdf"; src.write_bytes(b"%PDF-1.4 stub")
    conn = _setup_db(tmp_path, file_path=src)
    d = OCRmyPDFDigitizer(
        run_fn=lambda _: _completed(0),
        extract_fn=_fake_extract_pages([_ENGLISH_PAGE, _IAST_PAGE, _ENGLISH_PAGE]),
    )
    report = digitize_holding(conn, 1, d,
                              cfg=DigitizeConfig(archival_dir=tmp_path / "archival"))
    assert report.escalation_pages == [1]
    assert report.queued_escalation is True
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM review_queue WHERE item_type='low_quality_ocr' "
        "AND payload_json LIKE '%cloud_vision_escalation%'"
    ).fetchone()[0])
    assert payload["kind"] == "cloud_vision_escalation"
    assert payload["pages"] == [1]


def test_no_escalation_when_no_diacritic_pages(tmp_path):
    src = tmp_path / "scan.pdf"; src.write_bytes(b"%PDF-1.4 stub")
    conn = _setup_db(tmp_path, file_path=src)
    d = OCRmyPDFDigitizer(
        run_fn=lambda _: _completed(0),
        extract_fn=_fake_extract_pages([_ENGLISH_PAGE, _ENGLISH_PAGE]),
    )
    report = digitize_holding(conn, 1, d,
                              cfg=DigitizeConfig(archival_dir=tmp_path / "archival"))
    assert report.escalation_pages == []
    assert report.queued_escalation is False


def test_escalation_disabled_by_config(tmp_path):
    src = tmp_path / "scan.pdf"; src.write_bytes(b"%PDF-1.4 stub")
    conn = _setup_db(tmp_path, file_path=src)
    d = OCRmyPDFDigitizer(
        run_fn=lambda _: _completed(0),
        extract_fn=_fake_extract_pages([_IAST_PAGE]),
    )
    report = digitize_holding(
        conn, 1, d,
        cfg=DigitizeConfig(archival_dir=tmp_path / "archival", escalate=False),
    )
    assert report.queued_escalation is False


def test_non_local_backend_not_escalated(tmp_path):
    """A Cloud-Vision (or ABBYY) result is already high-recall — don't
    re-route it. Only `local_kinds` backends escalate."""
    holding_file = tmp_path / "o.pdf"; holding_file.write_bytes(b"%PDF stub")
    export = tmp_path / "abbyy.pdf"; export.write_bytes(b"%PDF abbyy")
    conn = _setup_db(tmp_path, file_path=holding_file)
    d = ABBYYImportDigitizer(
        source_pdf=export, extract_fn=_fake_extract_pages([_IAST_PAGE]),
    )
    report = digitize_holding(conn, 1, d,
                              cfg=DigitizeConfig(archival_dir=tmp_path / "archival"))
    assert report.queued_escalation is False


def test_foreign_diacritic_count_surfaced(tmp_path):
    """valid-IAST filter (§4.8c): non-IAST substitutions counted in the report.
    Clean Tesseract diacritics produce zero."""
    src = tmp_path / "scan.pdf"; src.write_bytes(b"%PDF-1.4 stub")
    conn = _setup_db(tmp_path, file_path=src)
    d = OCRmyPDFDigitizer(
        run_fn=lambda _: _completed(0),
        extract_fn=_fake_extract("Bodhicaryāvatāra Nāgārjuna śūnyatā " * 50),
    )
    report = digitize_holding(conn, 1, d,
                              cfg=DigitizeConfig(archival_dir=tmp_path / "archival"))
    assert report.foreign_diacritics == 0


# ── ABBYY import path — manual on Mac (§4.8b) ────────────────────────────


def test_abbyy_import_records_external_pdf_and_text(tmp_path):
    src_holding_file = tmp_path / "original.pdf"
    src_holding_file.write_bytes(b"%PDF-1.4 stub")
    conn = _setup_db(tmp_path, file_path=src_holding_file)

    abbyy_export = tmp_path / "abbyy_out.pdf"
    abbyy_export.write_bytes(b"%PDF-1.4 abbyy-pdfa")

    d = ABBYYImportDigitizer(
        source_pdf=abbyy_export,
        extract_fn=_fake_extract("Crisp ABBYY text with diacritics ā ś ṇ ṃ. " * 40),
    )
    cfg = DigitizeConfig(archival_dir=tmp_path / "archival")
    report = digitize_holding(conn, 1, d, cfg=cfg)

    assert report.digitizer_used == "abbyy_import"
    assert report.archival_pdf_path.exists()
    assert report.archival_pdf_path.name == "abbyy_out.pdf"
    assert report.text_status == "ocr_good"


def test_abbyy_import_without_source_falls_back_to_holding_file(tmp_path):
    """M3 audit semantics: if no `source_pdf` override is configured,
    the digitizer processes the holding's own file_path. Useful when an
    operator wants to re-ingest the holding's existing file as-is."""
    src = tmp_path / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 stub")
    conn = _setup_db(tmp_path, file_path=src)
    d = ABBYYImportDigitizer(
        source_pdf=None,
        extract_fn=_fake_extract("ABBYY-quality text " * 50),
    )
    report = digitize_holding(conn, 1, d,
                              cfg=DigitizeConfig(archival_dir=tmp_path / "archival"))
    assert report.digitizer_used == "abbyy_import"
    assert report.archival_pdf_path.name == "scan.pdf"


def test_abbyy_import_logs_substitution_when_source_differs(tmp_path):
    """M3 audit trail: when the operator points ABBYY at a separate
    export, the orchestrator queues an `archival_source_substitution`
    review item so the substitution is visible, never silent."""
    import json
    holding_file = tmp_path / "original.pdf"
    holding_file.write_bytes(b"%PDF-1.4 stub")
    abbyy_export = tmp_path / "abbyy_out.pdf"
    abbyy_export.write_bytes(b"%PDF-1.4 abbyy-pdfa")
    conn = _setup_db(tmp_path, file_path=holding_file)

    d = ABBYYImportDigitizer(
        source_pdf=abbyy_export,
        extract_fn=_fake_extract("Crisp ABBYY text " * 50),
    )
    digitize_holding(conn, 1, d,
                     cfg=DigitizeConfig(archival_dir=tmp_path / "archival"))

    row = conn.execute(
        "SELECT payload_json FROM review_queue "
        "WHERE item_type = 'low_confidence_extraction'"
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["kind"] == "archival_source_substitution"
    assert payload["digitizer_used"] == "abbyy_import"


# ── WebDAV-aware staging (§6, §13) ───────────────────────────────────────


def test_local_copy_dir_stages_file_off_webdav(tmp_path):
    """§6: copy a file locally before heavy processing — never stream
    repeatedly over the network share."""
    webdav_src = tmp_path / "webdav" / "scan.pdf"
    webdav_src.parent.mkdir()
    webdav_src.write_bytes(b"%PDF-1.4 stub")

    conn = _setup_db(tmp_path, file_path=webdav_src)

    seen_paths: list[Path] = []

    def _run(cmd):
        # The two positional file args are src then dst, just before any
        # `--redo-ocr` / `--force-ocr` flag is appended. Identify the src
        # by suffix so this test doesn't depend on argv ordering details.
        srcs = [Path(a) for a in cmd
                if a.endswith(".pdf") and not a.endswith(".pdfa.pdf")]
        seen_paths.extend(srcs)
        return _completed(0)

    d = OCRmyPDFDigitizer(run_fn=_run, extract_fn=_fake_extract("ok " * 200))
    local_dir = tmp_path / "local_stage"
    digitize_holding(
        conn, 1, d,
        cfg=DigitizeConfig(archival_dir=tmp_path / "archival",
                           local_copy_dir=local_dir),
    )
    # The path handed to ocrmypdf must be under the local stage dir, not
    # the WebDAV mount.
    assert seen_paths[0].is_relative_to(local_dir)
    assert (local_dir / "scan.pdf").exists()


# ── Candidates query ─────────────────────────────────────────────────────


def test_find_re_ocr_candidates_returns_poor_and_image_only(tmp_path):
    db_path = tmp_path / "cands.db"
    init_db(db_path).close()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO edition (title) VALUES ('T')")
    for status in ("ocr_good", "ocr_poor", "image_only", "native", "none"):
        conn.execute(
            "INSERT INTO holding (edition_id, form, file_path, text_status) "
            "VALUES (1, 'electronic', ?, ?)",
            (f"/{status}.pdf", status),
        )
    conn.commit()
    ids = find_re_ocr_candidates(conn)
    statuses = [
        conn.execute("SELECT text_status FROM holding WHERE id=?", (i,)).fetchone()[0]
        for i in ids
    ]
    assert set(statuses) == {"ocr_poor", "image_only", "none"}


# ── digitizer_kind lookup seed (§12 rule 4) ──────────────────────────────


def test_digitizer_kind_lookup_seeded(tmp_path):
    """Open-vocabulary lookup table — §12.4: new Digitizers added as data,
    not migrations. The FK from `holding.digitizer_used` must resolve."""
    init_db(tmp_path / "seed.db").close()
    conn = sqlite3.connect(tmp_path / "seed.db")
    codes = {r[0] for r in conn.execute("SELECT code FROM digitizer_kind")}
    assert {"ocrmypdf_tesseract", "abbyy_import"} <= codes
