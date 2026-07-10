"""Phone-drop ingest: consume JSON sidecars from the Books `_inbox/` folder.

The "add a book from my phone" path (hosted_server_thoughts.md §9.7): the phone
(kDrive app + an iOS Shortcut) drops a book plus a sidecar next to it —
`Book.pdf` + `Book.pdf.json` — into `<Books root>/_inbox/`. The normal sweep walk
ingests the media file (creating an edition+holding); THIS pass then reads the
sidecar, applies its metadata to that freshly-swept holding (self-associating by
filename), flags the edition for review, and — if the file is an un-OCRed scan —
runs the existing OCR pipeline. Decoupled from Mac uptime: kDrive holds the bytes
until the Mac next syncs + sweeps.

Greenfield (no prior sidecar handling). Runs as a post-pass in `sweep.sweep()` so
the holding row already exists. Idempotent: a consumed sidecar is renamed to
`*.json.done` (the `*.json` glob skips it next time); the media file stays in
place so `holding.file_path` keeps pointing at it.

Sidecar JSON (all fields optional; association is by filename stem):
    {"isbn": "...", "title": "optional override", "shelf": "...",
     "note": "free text", "cover_photo": "Book.cover.jpg", "source": "ios-shortcut"}
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .isbn import normalize_isbn
from .reconcile import _looks_offline


def _acc(conn):
    """A system Access over this connection — engine-routed edition/holding writes + the review
    queue. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(conn)

INBOX_DIRNAME = "_inbox"


@dataclass
class InboxReport:
    applied: int = 0            # sidecars consumed + metadata applied
    skipped_offline: int = 0    # media file is an un-hydrated kDrive placeholder
    skipped_unswept: int = 0    # media not yet swept into a holding (retry next run)
    ocr_run: int = 0            # image-only files sent through digitize on ingest
    errors: int = 0


def inbox_dir(mount_root) -> Path:
    return Path(mount_root) / INBOX_DIRNAME


def apply_inbox_sidecars(conn, cfg, *, digitizer=None) -> InboxReport:
    """Read every `*.json` sidecar under `<mount_root>/_inbox/`, apply it to the
    already-swept holding for its sibling media file, and consume it. The media
    file must have been swept first (same `sweep()` run walks `_inbox/`)."""
    report = InboxReport()
    root = inbox_dir(cfg.mount_root)
    if not root.is_dir():
        return report

    for sidecar in sorted(root.glob("*.json")):
        try:
            if _apply_one(conn, sidecar, report, digitizer):
                # Consume only on success so a transient miss is retried next run.
                sidecar.rename(sidecar.with_name(sidecar.name + ".done"))
        except Exception:
            report.errors += 1
            conn.rollback()
    return report


def _apply_one(conn, sidecar: Path, report: InboxReport, digitizer) -> bool:
    # `Book.pdf.json` → media sibling `Book.pdf`.
    media = sidecar.with_name(sidecar.name[: -len(".json")])

    # Placeholder / not-yet-present guard: leave the sidecar, retry next sweep.
    if not media.exists() or _looks_offline(str(media)):
        report.skipped_offline += 1
        return False

    holding = _acc(conn).holdings.reads.by_file_path(str(media))
    if holding is None:
        # The media file hasn't been swept into a holding yet — retry next run.
        report.skipped_unswept += 1
        return False
    holding_id, edition_id, text_status = holding.id, holding.edition_id, holding.text_status

    data = json.loads(sidecar.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        data = {}

    isbn = normalize_isbn(data.get("isbn") or "") or None
    title = (data.get("title") or "").strip() or None
    shelf = (data.get("shelf") or "").strip() or None
    note = (data.get("note") or "").strip() or None
    cover = (data.get("cover_photo") or "").strip() or None
    source = (data.get("source") or "ios-shortcut").strip()

    acc = _acc(conn)
    # Apply to the edition — additively (never clobber an existing ISBN).
    if isbn:
        cur = acc.editions.reads.get(edition_id)
        if cur is not None and not (cur.isbn or "").strip():
            acc.editions.writes.set_columns(edition_id, {"isbn": isbn})
    if title:
        acc.editions.writes.set_columns(edition_id, {"title": title})
    # Flag the freshly-ingested edition for operator review.
    acc.editions.writes.set_review_status(edition_id, "needs_fix")

    # Apply to the holding — shelf + a provenance note.
    if shelf:
        acc.holdings.writes.set_columns(holding_id, {"shelf_location": shelf})
    prov = _provenance_note(source, note, cover)
    if prov:
        acc.holdings.writes.append_note(holding_id, prov)

    # Queue a review item (existing 'ingest' type — no new vocabulary needed).
    acc.review.writes.enqueue("ingest", {
        "kind": "inbox_sidecar",
        "edition_id": edition_id,
        "holding_id": holding_id,
        "media": str(media),
        "sidecar": {"isbn": isbn, "title": title, "shelf": shelf,
                    "note": note, "cover_photo": cover, "source": source},
    })
    conn.commit()
    report.applied += 1

    # OCR-on-ingest: an un-OCRed scan goes through the EXISTING pipeline. Guarded
    # so an OCR failure (e.g. ocrmypdf not installed) never undoes the metadata.
    if text_status == "image_only":
        try:
            _ocr_holding(conn, holding_id, digitizer)
            report.ocr_run += 1
        except Exception:
            conn.rollback()   # drop only the OCR attempt; metadata above is committed
    return True


def _provenance_note(source: str, note: Optional[str], cover: Optional[str]) -> str:
    bits = []
    if note:
        bits.append(note)
    if cover:
        bits.append(f"cover: {cover}")
    if source:
        bits.append(f"(via {source})")
    return " ".join(bits)


def _ocr_holding(conn, holding_id: int, digitizer) -> None:
    from . import digitize as D
    if digitizer is None:
        digitizer = D.OCRmyPDFDigitizer()
    D.digitize_holding(conn, holding_id, digitizer)
