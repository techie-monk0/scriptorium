# Docs index

Documentation for the library-cataloging catalogue, grouped by area. New here? Read
**[ARCHITECTURE.md](ARCHITECTURE.md)** for the overall map, then **[USAGE.md](USAGE.md)** to run it.

## By area

### `access/` + `contracts/` — the entity-API engine + data/access model
The access-API (`catalogue.access_api`) architecture every app layer goes through, and the client-facing contract it exposes. **This is where the first-class entities (works, editions, persons, authorities…) are defined.**
- [contracts/data_model.md](contracts/data_model.md) — **start here**: client-facing data model — the entity model, read shapes, references/lifecycle, `Impact`, errors, vocab.
- [access/entity_api_model.md](access/entity_api_model.md) — the engine behind it: aggregate roster, gateway, plan→apply, soft-delete, non-FK registry, ports/adapters.
- [access/scan_ocr_provenance_model.md](access/scan_ocr_provenance_model.md) — the scan/OCR provenance slice (the Holding sub-aggregate).
- [access/external_tool_dependency_contract.md](access/external_tool_dependency_contract.md) — how consumers depend on the catalogue's external read-contract.

### `design/` — domain models + algorithms
[design/frbr_data_model.md](design/frbr_data_model.md) (Work/Edition/Person, FRBR) · [design/authority_dedup_model.md](design/authority_dedup_model.md) · [design/authority_matching.md](design/authority_matching.md) · [design/person_resolution.md](design/person_resolution.md) · [design/commentary_relationships_model.md](design/commentary_relationships_model.md) · [design/multi_work_segmentation.md](design/multi_work_segmentation.md) · [design/title_recognition.md](design/title_recognition.md)

## Top level
- [ARCHITECTURE.md](ARCHITECTURE.md) — the overall map: packages, entities, relationships, reads/writes, ingest, search, surfaces. Start here to orient.
- [USAGE.md](USAGE.md) — entry point / how to run. (The front door.)
- [INTEGRATIONS.md](INTEGRATIONS.md) — how the catalogue connects to other projects (OCR `scholia-rag-ocr`, …); points to the contract docs.
