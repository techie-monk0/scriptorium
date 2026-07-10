# Deletions log

Tests removed in the Step-4 audit pass because they tested the
implementation, not the plan. Each had no plan-level invariant beyond
what a sibling unit test or a system test already covers.

| Test (file::name) | Why removed | Plan coverage now lives in |
|---|---|---|
| `test_search.py::test_pipeline_has_four_named_replaceable_stages` | Asserted attribute names (`normalize/expand/match/rank`). A faithful re-implementation could rename them; the plan invariant is "search is a composable pipeline with a no-op expansion hook," which is observable as search behavior. | `tests/system/test_search.py` (search behavior + expansion no-op observable end-to-end) |
| `test_search.py::test_expansion_is_swappable_without_touching_callers` | Constructed `SearchService(expand=fake)` and asserted via attribute observation. Tests the swap mechanism, not the deferred feature it enables. | `tests/system/test_search.py::test_phonetic_query_does_not_match_wylie_body` indirectly verifies the no-op-expansion contract. The day the live expander lands, a system test will cover its behavior; the swap mechanism itself doesn't need its own test. |
| `test_search.py::test_rank_stage_is_invoked_and_replaceable` | Constructed `SearchService(rank=custom_rank)` and asserted via a side-effect dict. Same anti-pattern as above. | If a real ranker ever ships, its behavior gets a system test. |
| `test_web.py::test_search_service_is_swappable_on_the_app` | Replaced `app.config["SEARCH"]` to verify routes call the configured service. Reached past the public HTTP surface. | `tests/system/test_search.py` verifies search behavior. |

## What I also caught while doing this

`test_search.py::test_normalizer_matches_resolver_fold` was REWRITTEN
rather than deleted. The original asserted `normalize_query == fold_key`
— locking in a bug. §4.5's worked example
(`tathagatagarbha` ↔ `tathāgatagarbha`) is incompatible with `fold_key`'s
digraph collapse (`th→t`). The system test `test_search.py::
test_bare_latin_query_finds_diacriticked_body` failed; investigation
exposed that the search-time normalizer must align with FTS5's index
fold (diacritic-strip only), not the resolver's match key (aggressive
collapse). `search_normalize` was introduced as a distinct function.
The unit test now asserts they are DIFFERENT for digraph cases.

This is exactly what the audit promised: system tests target the plan;
when they catch an internal disagreement, the implementation moves.

## 2026-06-09 — Dashboard redesign (5-feature hub)

The dashboard was redone as a clean hub of 5 features (Browse / Search / Review /
Scan / Capture), mobile-first responsive. Several pages were removed entirely;
their tests are deleted or re-pinned to the surviving surface:

- **`tests/system/test_needs_work.py`** — DELETED. The `/needs-work` three-tier
  page and `/needs-work/match` were removed (clutter; not one of the 5 features).
- **`test_open_file.py`** — `/holdings` list page removed. The shared-shell + inline
  edition-card wiring it pinned is re-pinned against `/library` (Browse), which uses
  the same `_book_browser.html` shell and `_library_detail.html` (same markers).
- **`test_review_workflow.py`** — `test_ocr_override_*` no longer observes via the
  removed `/holdings` page; it now asserts the `holding.text_status` the override
  writes. The dashboard assertion now checks the hub surfaces a Review entry point.
- **`test_catalogue_web.py`** — the standalone `/catalogue`, `/catalogue/works`,
  `/catalogue/authors` browse pages were removed (folded into unified Search at
  `/find`). `test_catalogue_page_renders`, `test_browse_works_az`,
  `test_browse_authors_az` DELETED; the two diacritic-fold search tests re-pinned to
  `/find`; the sandbox-banner test reads `/library` instead of `/catalogue`.
- Smoke-route lists (`test_web.py`, `test_step3a.py`) drop `/holdings`, add `/find`.

## 2026-06-22 — `/find` ("Browse") entry point removed

The unified type-grouped `/find` surface (and `/find/suggest`, `/api/v1/find`) was
removed — the Search page (`/search`, the `_book_browser`-backed metadata search)
covers it, and `/find` was no longer in the nav. The grouped-search BEHAVIOUR still
lives in the domain (`search.aggregate_search` / `search.suggest`); tests that hit
the removed HTTP endpoints were re-pinned to those domain functions:

- **`test_api_feature_json.py`** — `test_api_find_*` (3) DELETED (`/api/v1/find` gone).
- **`test_dashboard_redesign.py`** — `test_find_groups_results_by_type`,
  `test_find_chip_filters_to_one_group`, `test_find_suggest_prefixes_each_match_with_its_type`,
  `test_find_empty_query_is_calm` re-pinned to `search.aggregate_search`/`suggest`.
  `test_find_by_internal_number` was already domain-level (unchanged).
- **`test_catalogue_web.py`** — the two diacritic-fold tests re-pinned from
  `/api/v1/find` to `search.aggregate_search`.
- **Smoke route lists** (`test_web.py`, `test_step3a.py`) drop `/find`.

## 2026-06-22 — Authority diffs surface removed (cleanup phase 3b)

The `/catalogue/verify` (authority-diff triage) and `/catalogue/review` (promoted-
edition review) surface was removed entirely, along with the edition authority-signal
card that linked to it. Deleted: `routes/catalogue_review.py`, templates
`catalogue_verify.html`/`catalogue_review.html`/`_review_detail.html`/`_authority_signal.html`,
the `/edition/<id>/authority-signal` route, and the verify-triage functions in
`domain/catalogue_review.py` (`iter_editions_for_review`, `categorize_payload`,
`authority_signal`, `gather_verify_diffs`, `backfill_empty`, `dismiss_*`, `accept_*`,
`merge_person`, `main`). The curation API (`draft_*`, `parse_works_form`, `apply_draft`,
`get_review`/`set_review`) stays — it still powers the `/edition/<id>/works` + review-card.

- **`test_catalogue_review_triage.py`** — trimmed to the surviving `set_review`/`get_review`
  test; the categorizer/triage tests were deleted with the functions.
- **`test_catalogue_web.py`** — `_queue_diff`, `test_verify_triage_*`, `test_review_page_lists_unreviewed`,
  `test_single_edition_deeplink`, `test_review_books_page_points_to_persons_first` DELETED;
  `test_verdict_card_saves` trimmed to its DB assertions; the sandbox-banner test reads `/review`;
  the persons-picker test checks the new intro blurb instead of the dropped "Step 1 of 2" cue.
- **`test_works_attach_search.py`** — `test_review_pane_declutters_record_into_details` DELETED
  (it pinned the removed `_review_detail.html`); the attach-search feature stays covered by
  `test_works_card_has_attach_search`.

## 2026-06-22 — TOC-pattern proposal-promotion UI removed (cleanup phase 3c)

The `book_toc_pattern` "Auto-detect proposals" promotion UI (already chip-less) was
removed: the `/review-queue` segment master-detail view, the `/review-queue/<id>/promote`,
`/revert`, `/payload` routes, the `/review-queue/segment/<seg>/promote|revert` batch
routes, and the `_proposal_detail.html` / `_proposal_edit.html` templates. `/review-queue`
now serves only the flat queue list + per-item detail (authority/OCR resolution) + staging.

The promotion DOMAIN logic (`catalogue.domain.promote`: `promote_proposal`, `revert_proposal`,
`segment_counts`, `proposal_summary`, `bucket`, …) is KEPT — it's the documented step-5
mechanism and is exercised directly as scaffolding by several test files. `book_toc_pattern`
ingest queuing (`process.py`) and its consumers (`work_detect`, `edition_structure`) are
also untouched (they feed the live Books review).

- **`test_promote_workflow.py`** — DELETED (it drove the removed `/review-queue` promote UI).
- **`test_promote.py`** — `test_web_accept_runs_ingest_verify_when_enabled` DROPPED (hit the
  removed route); the ~19 domain promote/revert/bucket/segment tests stay.
- **`test_edit_ui.py`** — the "Proposal payload editor" tests (`/review-queue/<id>/payload`)
  DROPPED; the holding/edition edit-card tests stay.

## 2026-06-22 — run-once CLI scripts deleted (cleanup phase 5)

Deleted run-once backfills / one-off dry-run report CLIs with no live importer and
no external consumer (verified against sibling repos isbn-scanner, CorpusRAG,
tibetanGlossary): backfill_capture_titles, backfill_page_text, collapse_redundant_works,
orphan_works_report, fix_shared_isbns, isbn_backfill_report, db_cruft_report,
person_candidates, and the CIP-report cluster (cip_report, cip_sanskrit_report,
cip_wylie_report, sanskrit_title_report + domain/cip_report_common).

KEPT (still live): edition_dedup (imported by domain/match.py), rehash (settings/
reconcile/relink/mount), backfill_ol_work_key (sweep), subject_backfill (subjects),
work_dedup (detect), books_by_subject (used by ../tibetanGlossary), and the live CIP
intake path domain/cip.py + /capture/cip (used by ../isbn-scanner) — NOT the cip_* reports.

- **`test_backfill_capture_titles.py`, `test_fix_shared_isbns.py`, `test_person_candidates.py`** — DELETED with their CLIs.
- **`test_page_text_cache.py`** — `test_backfill_page_text_resumable` dropped (the other page-text-cache + backfill_ol_work_key tests stay).
