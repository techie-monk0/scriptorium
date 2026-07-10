# Docs index

Documentation for the library-cataloging catalogue, grouped by area. Start with **[USAGE.md](USAGE.md)**.

## By area

### `access/` — the entity-API engine + data/access model
The access-API (`catalogue.access_api`) architecture every app layer goes through.
- [access/entity_api_model.md](access/entity_api_model.md) — the engine: gateway, plan→apply, soft-delete, registry, ports/adapters.
- [access/scan_ocr_provenance_model.md](access/scan_ocr_provenance_model.md) — the scan/OCR provenance slice.
- [contracts/data_model.md](contracts/data_model.md) — client-facing data model (entities, contracts, errors, vocab).

### `plans/` — cross-cutting plans + roadmaps
- [plans/repo_modularization_plan.md](plans/repo_modularization_plan.md) — the uv-monorepo split (phases).
- [plans/later_tasks.md](plans/later_tasks.md) — **the live backlog** (deferred/optional follow-ups).
- [plans/cleanup_plan.md](plans/cleanup_plan.md), [plans/catalogue_plan.md](plans/catalogue_plan.md), [plans/catalogue_web_ui_plan.md](plans/catalogue_web_ui_plan.md)
- FRBR / works: [plans/frbr_migration_plan.md](plans/frbr_migration_plan.md), [plans/works_rebuild_plan.md](plans/works_rebuild_plan.md), [plans/sanskrit_works_plan.md](plans/sanskrit_works_plan.md), [plans/edition_holdings_consolidation_plan.md](plans/edition_holdings_consolidation_plan.md)
- Authority: [plans/authority_dedup_plan.md](plans/authority_dedup_plan.md)
- Reader / display: [plans/reader_module_plan.md](plans/reader_module_plan.md), [plans/reader_epub_annotations_plan.md](plans/reader_epub_annotations_plan.md), [plans/display_environment_plan.md](plans/display_environment_plan.md)

### `design/` — domain models + algorithms
[design/frbr_data_model.md](design/frbr_data_model.md) · [design/authority_dedup_model.md](design/authority_dedup_model.md) · [design/authority_matching.md](design/authority_matching.md) · [design/person_resolution.md](design/person_resolution.md) · [design/commentary_relationships_model.md](design/commentary_relationships_model.md) · [design/multi_work_segmentation.md](design/multi_work_segmentation.md) · [design/title_recognition.md](design/title_recognition.md) · [design/ocr_considerations.md](design/ocr_considerations.md) · [design/wishlist_model.md](design/wishlist_model.md)

### `frontend/` — web / PWA / native client
- Contracts/architecture: [frontend/frontend_contract.md](frontend/frontend_contract.md) (the webui↔PWA↔native capability/shape contract) · [frontend/reader_architecture.md](frontend/reader_architecture.md) (cross-surface reader: octavo engine + shared chrome + EPUB WKWebView touch model) · [frontend/api_contract.md](frontend/api_contract.md) · [frontend/pwa_architecture.md](frontend/pwa_architecture.md)
- Plans: [frontend/frontend_plan.md](frontend/frontend_plan.md) · [frontend/pwa_frontend.md](frontend/pwa_frontend.md) · [frontend/ios_frontend.md](frontend/ios_frontend.md) · [frontend/device_local_plan.md](frontend/device_local_plan.md) · [frontend/hosted_server_thoughts.md](frontend/hosted_server_thoughts.md)

### `notes/` — runbooks + working notes
[notes/whats_next.md](notes/whats_next.md) · [notes/cataloguing-state.md](notes/cataloguing-state.md) · [notes/manual-physical-entry.md](notes/manual-physical-entry.md) · [notes/multi_work_import_process.md](notes/multi_work_import_process.md)

## Top level
- [USAGE.md](USAGE.md) — entry point / how to run. (The only doc kept at the top, intentionally — it's the front door.)
