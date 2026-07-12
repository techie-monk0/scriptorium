# Integrations

How the catalogue connects to the other projects it works with. The catalogue is the
system of record for **identity** (what a book *is*); other tools do specialized work
(OCR, RAG) and lean on that identity. Each integration is a small, stable contract — this
page names the projects and points at the authoritative contract docs rather than
restating them.

A downstream tool integrates in one of two ways, depending on how much it needs:

- **The access API** — to actually *read or write* catalogue entities (editions, works,
  people, holdings). This is the full surface the built-in apps use.
- **The identity read-contract** — to just *follow what a book is* (its stable `pub_id`)
  without importing catalogue code. Lighter; this is the OCR/RAG case.

## Read & write through the access API

Everything that touches the database goes through one package, `catalogue.access_api`, and
an external tool integrates the same way the apps do. `bind(principal, policy, db)` hands you
per-entity **readers** and **writers**: reads are side-effect-free previews, writes are a
`plan → apply` two-step, and the objects that cross the boundary (DTOs, `Ref`, `Impact`, the
error taxonomy) are serializable — so an in-process consumer and a future HTTP consumer code
against the same shapes. Use this when a tool needs the entities, not just their identity.

- **The engine** (gateway, reader/writer split, plan→apply, integrity):
  [`access/entity_api_model.md`](access/entity_api_model.md).
- **The client-facing contract** you code against (entity read shapes, `Ref`, `Impact`,
  errors, authz): [`contracts/data_model.md`](contracts/data_model.md).

## OCR — `scholia-rag-ocr`

The catalogue does not OCR scans itself — that's a separate project,
[`techie-monk0/scholia-rag-ocr`](https://github.com/techie-monk0/scholia-rag-ocr). The two
connect through the catalogue's stable identity contract: the catalogue exposes each scanned
holding's file under a stable `pub_id`, the OCR pipeline processes it and stamps its output
with that same `pub_id`, and the catalogue (and the downstream RAG) reads the result back.

- **Catalogue side of the contract** — what identity the catalogue publishes and the read
  discipline consumers must follow:
  [`access/external_tool_dependency_contract.md`](access/external_tool_dependency_contract.md).
- **OCR output side** — how to read the `doc.json` / handoff manifest the pipeline produces:
  the OCR repo's
  [`docs/INTEGRATIONS.md`](https://github.com/techie-monk0/scholia-rag-ocr/blob/main/docs/INTEGRATIONS.md)
  (the "Consuming doc.json" client guide).
