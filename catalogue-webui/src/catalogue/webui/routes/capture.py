"""Phone capture (§3b, §14) — barcode / CIP / no-ISBN intake.

The §14 capture contract surface: the barcode/manual form, the idempotent JSON
scan path, batch + CSV bulk import, the on-device CIP (copyright-page OCR) intake,
and the no-ISBN title/author lookup. Each scan is staged durably BEFORE any
best-effort network verdict, so a scan is never lost ([[capture-not-in-catalogue-log]],
[[cip-phone-capture]]). The JSON path (`capture_one_json`) is also reused by the
device-local `/api/v1/capture` endpoint — exposed via `ctx`.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from flask import jsonify, render_template, request, g

from catalogue.services.isbn import normalize_isbn, validate_isbn13
from ._shared import _acc


# §14 capture integration contract — single source of truth, bumped together on
# both sides if the request/response schema changes.
# v2 (§14.6/§14.7): /capture response gains the cross-format verdict
# (in_catalogue/matched_by/editions); new GET /capture/find for no-ISBN
# title/author lookup. Both additive — v1 clients keep working.
# v3 (§14.9): new POST /capture/cip — the phone OCRs the copyright page (and,
# when there's no ISBN, the title/back pages) on-device and posts the recognized
# TEXT; the server parses the CIP block and returns the same cross-format verdict.
# Additive — v2 clients keep working.
# v4 (§14.10): /capture + /capture/cip accept an optional "intent" field. intent=="wishlist"
# routes the scan into the wishlist (books wanted but not yet owned) instead of capture_staging,
# returning the created wishlist item; absent/"catalogue" is the unchanged default. The catalogue
# path also gains an acquisition loop (a positive verdict flips a matching wishlist item to
# 'acquired'), surfaced as `fulfilled_wishlist_item`. All additive — v3 clients keep working.
CAPTURE_CONTRACT_VERSION = "4"
_CAPTURE_SOURCES = {"ios", "web", "csv", "manual", "pwa"}
# A capture `source` (ios/web/pwa…) maps to the wishlist source 'scan' unless it already names a
# wishlist source; keeps the two source vocabularies from leaking into each other.
_WISHLIST_SCAN_SOURCES = {"manual", "isbn", "cip", "scan"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _validate_isbn13_reason(raw: str) -> tuple[str, str | None]:
    """Return (clean_isbn, error_reason). reason ∈ {None,'format','length',
    'checksum'} per §14.2. `raw` is the value as received from the client."""
    if not isinstance(raw, str) or not raw:
        return "", "format"
    # §14.2: digits only. A scanner may emit hyphens; the contract says
    # client sends digits only, but the server is authoritative — strip and
    # flag anything truly non-numeric (letters, symbols) as 'format'.
    if any(c for c in raw if not (c.isdigit() or c in "- ")):
        return "", "format"
    clean = normalize_isbn(raw)
    if len(clean) != 13:
        return clean, "length"
    if not validate_isbn13(clean):
        return clean, "checksum"
    return clean, None


def _capture_stage_isbn(
    db, isbn: str, source: str, scanned_at: str | None = None,
) -> tuple[int, bool]:
    """Insert one §14-conformant capture row, deduping on the open ('raw')
    row for the same ISBN (§14.2 idempotency / §14.5 dedup). Returns
    (staging_id, duplicate). On a duplicate hit the existing `scanned_at`
    is preserved — the first capture's time-of-scan is the canonical one.

    Concurrency: relies on the `capture_staging_raw_isbn_uq` partial
    unique index. INSERT OR IGNORE serializes concurrent POSTs at the DB
    layer; a SELECT-then-INSERT race window is closed even when two
    requests run on separate connections under WAL.
    """
    return _acc(db).capture.stage_isbn(isbn, source, scanned_at)


def _extract_isbns_from_csv(raw: str, *, limit: int) -> list[str]:
    """Pull candidate ISBNs from arbitrary CSV/text. Strategy: per line,
    pick the first run of digits that looks plausibly ISBN-ish (≥10 digits
    after normalize). Anything else on the line (timestamps, titles,
    extra columns) is ignored. Empty lines / comments skipped."""
    import re as _re
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Find the longest digit run on the line (handles `isbn,when` and
        # quoted CSV equally well).
        runs = _re.findall(r"[\d-]+", line)
        for r in runs:
            digits = "".join(c for c in r if c.isdigit())
            if len(digits) >= 10:
                out.append(digits)
                break
        if len(out) >= limit:
            break
    return out


def register(app, ctx):
    # ── Phone capture (Step 3b) ──────────────────────────────────────────
    # Designed for a Bluetooth/HID barcode scanner (§3b): the ISBN input
    # is autofocused; the scanner types digits + Enter; Enter submits the
    # form. Manual typing works identically. Photo upload + free-text note
    # are the fallback when there is no ISBN or the lookup misses.
    @app.get("/capture")
    def capture_form():
        not_in_catalogue, added = _capture_not_in_catalogue(g.db)
        return render_template(
            "capture.html", saved=None,
            not_in_catalogue=not_in_catalogue, added=added,
        )

    def _capture_meta(metadata_json) -> dict:
        """Normalize a scan's stored `metadata_json` to the {title, authors,
        publishers} shape the matcher expects. Covers both the OpenLibrary lookup
        shape (`publishers` list) and the CIP-parse shape (`publisher` scalar)."""
        try:
            md = json.loads(metadata_json) if metadata_json else {}
        except Exception:
            md = {}
        if not isinstance(md, dict):
            md = {}
        pubs = md.get("publishers")
        if not pubs and md.get("publisher"):
            pubs = [md["publisher"]]
        return {"title": md.get("title"),
                "authors": [a for a in (md.get("authors") or []) if a],
                "publishers": [p for p in (pubs or []) if p]}

    def _capture_not_in_catalogue(db, limit: int = 200) -> tuple[list[dict], list[dict]]:
        """The log the operator asked for: every phone (`ios`) capture whose
        cross-format verdict was NOT 'already in catalogue' at capture time,
        newest first. Each row carries the ISBN, any OCR text that was sent (CIP
        pages land in `free_text_note`), and the time of capture (the phone's
        `scanned_at`, falling back to the server's `created_at`).

        Returns `(still_missing, added)`: a scan moves to the `added` bucket once
        the catalogue holds the book — its staging row was resolved here, OR the
        book is now catalogued (matched cross-edition by ISBN or by title+author,
        all local — see `intake_match.editions_now_holding`). Different printings
        of one title carry different ISBNs that public sources often don't link,
        so an exact-ISBN-only re-check missed copies catalogued under another
        ISBN; the title+author match catches those."""
        from catalogue.services import intake_match
        rows = _acc(db).capture.not_in_catalogue(limit)
        still_missing: list[dict] = []
        added: list[dict] = []
        for r in rows:
            meta = _capture_meta(r[5])     # normalized {title, authors, publishers}
            rec = {"id": r[0], "isbn": r[1], "ocr": r[2],
                   "captured_at": r[3], "status": r[4],
                   "title": meta.get("title"), "editions": []}
            try:
                editions = intake_match.editions_now_holding(
                    db, isbn=r[1], meta=meta)
            except Exception:
                editions = []
            # Volume number travels with the title via the edition_link macro
            # (edition_volume helper) — no per-row enrichment needed here.
            rec["editions"] = editions
            # Held now (cross-edition match) OR resolved here (CIP-only / manual
            # resolves the match path can't recover) → it's been added.
            is_added = bool(editions) or r[4] == "resolved"
            (added if is_added else still_missing).append(rec)
        return still_missing, added

    def _wishlist_scan(db, *, isbn=None, cip_text=None, source="scan") -> tuple[dict, int]:
        """Route a scan into the wishlist (intent=="wishlist"). Reuses the shared resolver+persist
        path so a scanned wishlist add behaves exactly like the typed /api/v1/wishlist add."""
        from .wishlist import add_from_input
        out = add_from_input(
            db, source=source, isbn=isbn, cip_text=cip_text,
            isbn_lookup=app.config.get("ISBN_LOOKUP"),
            work_key_fetch=app.config.get("ISBN_WORK_KEY_LOOKUP"))
        v = out["verdict"]
        return {
            "status": "ok", "intent": "wishlist", "wishlist_item": out["item"],
            # The scanner shows "Added to wishlist" / "Already owned" / "Already on wishlist" off these.
            "added": out["added"], "owned": out["owned"], "duplicate": out["duplicate"],
            "isbn": (out["item"] or {}).get("isbn") or isbn,
            "in_catalogue": v.get("in_catalogue", False), "matched_by": v.get("matched_by"),
            "editions": v.get("editions", []), "uncertain": v.get("uncertain", []),
        }, 201

    def _fulfil_wishlist(db, verdict, isbn):
        """Acquisition loop: when a capture verdict says the catalogue now holds the book, flip a
        matching live wishlist item to 'acquired' (pinning the fulfilling edition). Returns the
        wishlist item id that was closed, or None. Best-effort — never fails the (committed) scan."""
        if not verdict.get("in_catalogue") or not verdict.get("editions"):
            return None
        try:
            acc = _acc(db)
            m = acc.wishlist.match(isbn=isbn or None, ol_work_key=verdict.get("work_key"),
                                   title=verdict["editions"][0].get("title"))
            if m is None:
                return None
            acc.wishlist.mark_acquired(m.id, verdict["editions"][0]["id"])
            db.commit()
            return m.id
        except Exception:
            return None

    @app.get("/capture/version")
    def capture_version():
        # §14: clients display this in settings so a contract mismatch is
        # detectable. Bump CAPTURE_CONTRACT_VERSION when §14 changes.
        return jsonify({"contract_version": CAPTURE_CONTRACT_VERSION})

    def _capture_one_json(payload: dict) -> tuple[dict, int]:
        """One scan, §14.2 schema. Returns (body, http_status)."""
        if not isinstance(payload, dict):
            return {"status": "invalid", "reason": "format"}, 422
        raw = payload.get("isbn")
        source = payload.get("source") or "ios"
        if source not in _CAPTURE_SOURCES:
            source = "ios"  # open vocabulary (§5); unknown values normalize
        # §14.2: `scanned_at` is the client's time-of-scan. Stored as-is;
        # ill-formed values become NULL so we never reject the scan over a
        # timestamp the contract treats as informational.
        scanned_at_raw = payload.get("scanned_at")
        scanned_at = scanned_at_raw if isinstance(scanned_at_raw, str) else None
        clean, reason = _validate_isbn13_reason(raw if isinstance(raw, str) else "")
        if reason is not None:
            return {"status": "invalid", "reason": reason}, 422
        # §14.10: intent=="wishlist" routes the scan into the wishlist instead of capture_staging.
        if payload.get("intent") == "wishlist":
            return _wishlist_scan(g.db, isbn=clean,
                                  source=source if source in _WISHLIST_SCAN_SOURCES else "scan")
        staging_id, duplicate = _capture_stage_isbn(g.db, clean, source, scanned_at)
        g.db.commit()
        # §14.6 cross-format verdict (contract v2). The scan is already durably
        # staged + committed above, so this best-effort lookup can NEVER lose a
        # scan — any exception/timeout is swallowed and the four original keys
        # are returned unchanged (old apps ignore the extras).
        verdict = {"in_catalogue": False, "matched_by": None, "editions": [], "uncertain": []}
        checked = False
        try:
            verdict = _capture_catalogue_verdict(g.db, clean)
            checked = True
        except Exception:
            pass
        # Persist the verdict so the not-in-catalogue capture log (on /capture)
        # can list this scan without re-running the network lookups. NULL stays
        # NULL when the verdict could not be computed (offline/timeout).
        if checked:
            _capture_record_verdict(g.db, staging_id, verdict)
            # Already in the catalogue → nothing to resolve; clear it from the inbox at the
            # source so an owned-book scan never lands in the Capture worklist/pill. Best-effort.
            if verdict.get("in_catalogue"):
                try:
                    _acc(g.db).capture.resolve(staging_id)
                    g.db.commit()
                except Exception:
                    pass
        # Also fetch + store the book's metadata so the capture log can show a
        # TITLE beside the ISBN (the bare barcode path used to store only the
        # number). Best-effort: the scan is already committed, so an offline /
        # timeout / 404 lookup just leaves the row title-less.
        _capture_store_metadata(g.db, staging_id, clean)
        return {
            "status": "ok",
            "staging_id": staging_id,
            "isbn": clean,
            "duplicate": duplicate,
            "in_catalogue": verdict.get("in_catalogue", False),
            "matched_by": verdict.get("matched_by"),
            "editions": verdict.get("editions", []),
            "uncertain": verdict.get("uncertain", []),
            # §14.10 acquisition loop: a positive verdict means the catalogue now holds this book —
            # close out any matching wishlist item (None when nothing was wishlisted).
            "fulfilled_wishlist_item": _fulfil_wishlist(g.db, verdict, clean),
        }, 201

    def _capture_store_metadata(db, staging_id: int, isbn: str) -> None:
        """Best-effort: look the ISBN up (the config-swappable `ISBN_LOOKUP`,
        OpenLibrary in production) and store the result on the staging row so the
        capture log shows a title. Only fills an EMPTY metadata column — never
        overwrites a CIP/web record — and never raises (the scan is already
        committed; offline/timeout simply leaves the row title-less)."""
        lookup = app.config.get("ISBN_LOOKUP")
        if not lookup or not isbn:
            return
        try:
            meta = lookup(isbn)
        except Exception:
            meta = None
        if not meta:
            return
        try:
            _acc(db).capture.set_metadata_if_empty(staging_id, meta)
            db.commit()
        except Exception:
            pass

    def _capture_catalogue_verdict(db, isbn: str) -> dict:
        """Cross-format "already in catalogue?" verdict for one ISBN, with
        work-key write-through: once an ISBN resolves to an OL work key, stamp it
        onto any matched edition that lacks one so the cluster self-densifies and
        future scans match instantly via the local DB (no network)."""
        from catalogue.services import intake_match
        v = intake_match.catalogue_verdict(
            db, isbn,
            ol_work_key_fetch=app.config.get("ISBN_WORK_KEY_LOOKUP"),
            isbn_lookup=app.config.get("ISBN_LOOKUP"),
        )
        wk = v.get("work_key")
        if wk and v.get("editions"):
            acc = _acc(db)
            for e in v["editions"]:
                acc.editions.writes.set_ol_work_key(e["id"], wk, only_if_empty=True)
            db.commit()
        # Drop the internal work_key before returning to the client.
        return {"in_catalogue": v["in_catalogue"], "matched_by": v["matched_by"],
                "editions": v["editions"], "uncertain": v.get("uncertain", [])}

    def _capture_record_verdict(db, staging_id: int, verdict: dict) -> None:
        """Stamp a computed cross-format verdict onto its staging row so the
        not-in-catalogue capture log can be rendered without re-querying. Best
        effort: a write failure here must never fail the (already committed)
        scan."""
        try:
            _acc(db).capture.set_in_catalogue(staging_id, bool(verdict.get("in_catalogue")))
            db.commit()
        except Exception:
            pass

    @app.get("/capture/find")
    def capture_find():
        """No-ISBN intake: does the catalogue already hold a book by this title or
        author, and in what form? (§14.7) Reuses the metadata search (title aliases
        + contributor names, diacritic-insensitive) — purely local, no network.
        Returns the same {editions:[{id,title,forms}]} shape the scan path uses."""
        from catalogue.services import search as SEARCH
        from catalogue.services import intake_match
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"matches": []})
        seen: dict[int, dict] = {}
        # Title hits OR contributor (author/translator) hits — union, AND within
        # neither (a single free-text needle), deduped by edition.
        for row in (SEARCH.find_books(g.db, book_title=q)
                    + SEARCH.find_books(g.db, persons=[q])):
            eid = row["edition_id"]
            if eid not in seen:
                seen[eid] = {
                    "id": eid,   # same key as the scan verdict's editions[] (one app model)
                    "title": row["title"] or f"edition #{eid}",
                    "authors": row.get("authors", []),
                    "forms": intake_match.forms_for_edition(g.db, eid),
                }
        return jsonify({"matches": list(seen.values())[:25]})

    @app.post("/capture/cip")
    def capture_cip():
        """Copyright-page (CIP) intake (§14.9, contract v3). The phone OCRs the
        copyright page on-device — and, when there's no ISBN, the title/back pages
        too — and posts the recognized TEXT here (Option A: no images leave the
        phone). We parse the CIP block (`cip.parse_cip`) and return the SAME
        cross-format "already in catalogue?" verdict the barcode path uses
        (`intake_match.cip_verdict`), staging the capture durably first so a no-ISBN
        book is never lost.

        Body: {"pages":[{"label":"title|copyright|back"|null,"text":"…"}],
               "scanned_at":"…","source":"ios"}. A bare "text"/"cip_text" string is
        also accepted. Response mirrors /capture's keys + a `parsed` echo of what
        the CIP parser understood (title/authors/isbns/year/publisher).
        """
        from catalogue.services import cip, intake_match
        if not request.is_json:
            return jsonify({"status": "invalid", "reason": "format"}), 422
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"status": "invalid", "reason": "format"}), 422

        # Gather page texts into one block. parse_cip's `_select_cip_block` isolates
        # the CIP within a larger dump, so we hand it everything we recognized.
        texts: list[str] = []
        pages = body.get("pages")
        if isinstance(pages, list):
            for p in pages:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    texts.append(p["text"])
                elif isinstance(p, str):
                    texts.append(p)
        for key in ("text", "cip_text"):
            if isinstance(body.get(key), str):
                texts.append(body[key])
        combined = "\n\n".join(t for t in texts if t and t.strip())
        if not combined.strip():
            return jsonify({"status": "invalid", "reason": "empty"}), 422

        source = body.get("source") or "ios"
        if source not in _CAPTURE_SOURCES:
            source = "ios"  # open vocabulary (§5); unknown values normalize
        scanned_at_raw = body.get("scanned_at")
        scanned_at = scanned_at_raw if isinstance(scanned_at_raw, str) else None

        # §14.10: intent=="wishlist" resolves the CIP into a wishlist item (the resolver parses the
        # CIP itself) instead of staging it for desktop resolution.
        if body.get("intent") == "wishlist":
            out, status = _wishlist_scan(g.db, cip_text=combined, source="scan")
            return jsonify(out), status

        rec = cip.parse_cip(combined)
        parsed = {
            "kind": getattr(rec, "kind", "none") if rec else "none",
            "title": getattr(rec, "title", None) if rec else None,
            "authors": list(getattr(rec, "authors", None) or []) if rec else [],
            "isbns": list(getattr(rec, "isbns", None) or []) if rec else [],
            "year": getattr(rec, "year", None) if rec else None,
            "publisher": getattr(rec, "publisher", None) if rec else None,
        }

        # Stage durably BEFORE the best-effort verdict so a scan is never lost
        # (mirrors the §14.6 ordering on /capture). With an ISBN, reuse the deduped
        # ISBN insert; otherwise keep the raw text + parsed record for desktop
        # resolve, exactly like the no-ISBN form path.
        primary_isbn = parsed["isbns"][0] if parsed["isbns"] else None
        if primary_isbn:
            staging_id, duplicate = _capture_stage_isbn(
                g.db, primary_isbn, source, scanned_at)
        else:
            staging_id = _acc(g.db).capture.insert(
                form="physical", raw_isbn=None, free_text_note=combined,
                metadata_json=json.dumps(parsed), source=source, scanned_at=scanned_at)
            duplicate = False
        g.db.commit()

        verdict = {"in_catalogue": False, "matched_by": None,
                   "editions": [], "uncertain": [], "isbn": primary_isbn}
        if rec is not None:
            try:
                verdict = intake_match.cip_verdict(
                    g.db, rec,
                    ol_work_key_fetch=app.config.get("ISBN_WORK_KEY_LOOKUP"),
                    isbn_lookup=app.config.get("ISBN_LOOKUP"),
                )
            except Exception:
                pass

        # Keep the OCR text + parsed record on the row (the ISBN path stages via
        # _capture_stage_isbn, which only stores raw_isbn) and stamp the verdict,
        # so the not-in-catalogue capture log shows the pages that were sent.
        # COALESCE preserves the no-ISBN path's already-stored note/metadata.
        try:
            _acc(g.db).capture.fill_note_meta_and_stamp(
                staging_id, combined, json.dumps(parsed), bool(verdict.get("in_catalogue")))
            g.db.commit()
        except Exception:
            pass

        return jsonify({
            "status": "ok",
            "staging_id": staging_id,
            "isbn": verdict.get("isbn") or primary_isbn,
            "duplicate": duplicate,
            "in_catalogue": verdict.get("in_catalogue", False),
            "matched_by": verdict.get("matched_by"),
            "editions": verdict.get("editions", []),
            "uncertain": verdict.get("uncertain", []),
            "fulfilled_wishlist_item": _fulfil_wishlist(
                g.db, verdict, verdict.get("isbn") or primary_isbn),
            "parsed": parsed,
        }), 201

    @app.post("/capture/batch")
    def capture_batch():
        # §14.3: per-item results in the same order as the input. Build only
        # the per-row results — no early-abort on a single invalid scan.
        if not request.is_json:
            return jsonify({"status": "invalid", "reason": "format"}), 422
        body = request.get_json(silent=True) or {}
        scans = body.get("scans")
        if not isinstance(scans, list):
            return jsonify({"status": "invalid", "reason": "format"}), 422
        per_item = [_capture_one_json(s) for s in scans]
        results = [body for body, _ in per_item]
        # Mixed/at-least-one-ok → 201 (per-item status in body says which
        # failed). All-invalid (and non-empty) → 422 so the client doesn't
        # interpret a fully-rejected batch as success.
        any_ok = any(code == 201 for _, code in per_item)
        status = 201 if any_ok or not per_item else 422
        return jsonify({"status": "ok", "results": results}), status

    @app.post("/capture")
    def capture_submit():
        # §14.2: JSON body → contract path; form/multipart → existing web
        # form path (photo + note + ISBN). The two paths never mix.
        if request.is_json:
            body, status = _capture_one_json(request.get_json(silent=True) or {})
            return jsonify(body), status

        raw_isbn = (request.form.get("isbn") or "").strip()
        note = (request.form.get("note") or "").strip()
        photo = request.files.get("photo")

        # 1. Validate ISBN-13 locally; only proceed to lookup if the
        #    checksum passes. Invalid digits → fall through to manual path.
        clean_isbn = normalize_isbn(raw_isbn) if raw_isbn else ""
        isbn_valid = bool(clean_isbn) and validate_isbn13(clean_isbn)
        metadata: dict | None = None
        if isbn_valid:
            try:
                metadata = app.config["ISBN_LOOKUP"](clean_isbn)
            except Exception:
                # Resolver must never bubble — fall back is the contract.
                metadata = None

        # 2. Save the photo to a LOCAL upload dir. Never write to a WebDAV
        #    mount (§6, §13).
        image_path: str | None = None
        if photo and photo.filename:
            safe = _SAFE_NAME_RE.sub("_", Path(photo.filename).name)[:80]
            local_name = f"{uuid.uuid4().hex}_{safe}"
            dest = Path(app.config["UPLOAD_DIR"]) / local_name
            photo.save(dest)
            image_path = str(dest)

        # 3. If everything is empty, refuse — keeps the staging table tidy.
        if not (clean_isbn or note or image_path):
            return _capture_response(
                error="Provide an ISBN, a photo, or a note.",
                metadata=None, status=400,
            )

        # §14.5 dedup: an open ('raw') row for the same ISBN already exists?
        # Update it with any newly-attached photo/note/metadata instead of
        # inserting a duplicate. Rows without an ISBN can never collide on
        # the partial unique index.
        acc = _acc(g.db)
        existing_id = acc.capture.open_raw_id(clean_isbn) if clean_isbn else None

        if existing_id is not None:
            acc.capture.merge_attachments(
                existing_id, image_path, note or None,
                json.dumps(metadata) if metadata else None)
            saved_id = existing_id
        else:
            saved_id = acc.capture.insert(
                form="physical", raw_isbn=clean_isbn or None, image_path=image_path,
                free_text_note=note or None,
                metadata_json=json.dumps(metadata) if metadata else None, source="web")
        g.db.commit()
        return _capture_response(
            saved_id=saved_id,
            metadata=metadata,
            isbn=clean_isbn or None,
            isbn_valid=isbn_valid,
            note=note,
        )

    # ── Bulk import (Barcode to PC CSV mode, §3b) ────────────────────────
    # Keystroke mode needs no extra endpoint — it lands in the /capture
    # form like a hardware wedge. This endpoint handles the CSV/list mode:
    # scan everything in one sitting, export, paste or upload here.
    @app.get("/capture/import")
    def capture_import_form():
        return render_template("capture_import.html", report=None)

    @app.post("/capture/import")
    def capture_import_submit():
        # Accept either a file upload or a textarea of pasted lines. Be
        # tolerant of CSV shapes — Barcode to PC may emit `isbn` alone or
        # `isbn,timestamp` etc. We take the first run of digits per line.
        raw = ""
        f = request.files.get("file")
        if f and f.filename:
            raw = f.read().decode("utf-8", errors="replace")
        if not raw:
            raw = request.form.get("lines") or ""

        max_rows = int(app.config.get("IMPORT_MAX_ROWS", 1000))
        candidates = _extract_isbns_from_csv(raw, limit=max_rows)

        report = {
            "scanned": 0, "imported": 0, "invalid": 0,
            "duplicate_in_batch": 0, "ids": [],
        }
        seen_in_batch: set[str] = set()
        for candidate in candidates:
            report["scanned"] += 1
            clean = normalize_isbn(candidate)
            if not clean:
                report["invalid"] += 1
                continue
            if clean in seen_in_batch:
                # Same code scanned twice in one batch — record once,
                # surface the count so the user knows.
                report["duplicate_in_batch"] += 1
                continue
            seen_in_batch.add(clean)

            valid = validate_isbn13(clean)
            metadata = None
            if valid:
                try:
                    metadata = app.config["ISBN_LOOKUP"](clean)
                except Exception:
                    metadata = None
            else:
                # §14.4: CSV imports go through the SAME validation path as
                # POST /capture, which 422s invalid scans. Count and skip;
                # do NOT silently stage a bad checksum as if it were valid.
                report["invalid"] += 1
                continue

            new_id = _acc(g.db).capture.insert(
                or_ignore=True, form="physical", raw_isbn=clean,
                free_text_note="bulk import (Barcode to PC CSV mode)",
                metadata_json=json.dumps(metadata) if metadata else None, source="csv")
            if new_id is not None:
                report["imported"] += 1
                report["ids"].append(new_id)
            else:
                # Open row for this ISBN already exists — partial unique
                # index ignored the insert. Surface as a duplicate, not a
                # silent loss.
                report["duplicate_in_batch"] += 1
        g.db.commit()

        if (request.accept_mimetypes.best_match(
                ["text/html", "application/json"]) == "application/json"
                or request.headers.get("X-Requested-With") == "shortcut"):
            return jsonify(report)
        return render_template("capture_import.html", report=report)

    def _capture_response(*, saved_id=None, metadata=None, error=None,
                          status=200, **fields):
        """Content-negotiated: HTML for the browser, JSON for an iOS
        Shortcut / curl using Accept: application/json."""
        wants_json = (
            request.accept_mimetypes.best == "application/json"
            or request.is_json
            or request.headers.get("X-Requested-With") == "shortcut"
        )
        if wants_json:
            payload = {
                "ok": error is None,
                "saved_id": saved_id,
                "metadata": metadata,
                "error": error,
                **{k: v for k, v in fields.items() if v is not None},
            }
            return jsonify(payload), status
        not_in_catalogue, added = _capture_not_in_catalogue(g.db)
        return render_template(
            "capture.html",
            saved=saved_id, metadata=metadata, error=error,
            not_in_catalogue=not_in_catalogue, added=added,
            **fields,
        ), status

    # Expose the idempotent JSON scan path to the /api/v1/capture endpoint.
    ctx.capture_one_json = _capture_one_json
