# Architecture

A bird's-eye view of the catalogue: what the pieces are, how they fit together, and
where data flows. This is the map — it summarizes each area in plain English and links
to the detailed design doc when you want to go deeper. Read it top-to-bottom once and
you'll know where everything lives.

> **What the catalogue is.** A local-first catalogue for a personal book library
> (mostly Buddhist classical texts and their modern editions). It follows **FRBR**: an
> abstract *Work* (a text) is realized by published *Editions* (books), of which the
> library holds physical or digital *Holdings* (copies). Everything lives in one SQLite
> file you own — no server and no cloud required.

---

## 1. The big picture

Three things stacked on top of each other: **apps** people use, the reusable
**library** underneath, and your **SQLite file** at the bottom. Every arrow points
downward — nothing lower ever reaches up.

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  PEOPLE / DEVICES                                                 │
  │    browser · phone (installed PWA) · terminal                    │
  └───────────────┬─────────────────────────────┬───────────────────┘
                  │ HTTP (JSON)                  │ in-process calls
                  ▼                              ▼
  ┌─────────────────────────────┐   ┌───────────────────────────────┐
  │  APPS                        │   │  catalogue-cli                │
  │  catalogue-webui  ──────────►│   │  (batch / admin ops)          │
  │  (Flask UI + HTTP API)       │   └───────────────┬───────────────┘
  │  catalogue-pwa (offline)     │                   │
  └───────────────┬─────────────┘                    │
                  │                                   │
                  ▼                                   ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  THE LIBRARY  (the reusable `catalogue.*` packages)              │
  │                                                                  │
  │     populate  ──►  services  ──►  access-api  ──►  db-store      │
  │                                       │                          │
  │                             (the ONE door to the DB)             │
  └───────────────────────────────────────┬─────────────────────────┘
                                           │ read-only + read-write
                                           ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  YOUR DATA  (you provide it; nothing is uploaded)               │
  │    catalogue.db  ·  cover art  ·  OCR/extract caches  ·  files   │
  └─────────────────────────────────────────────────────────────────┘
```

The key idea: **every touch of the database goes through `access-api`.** The apps, the
CLI, and the ingest pipelines never open the database themselves — they ask `access-api`,
which is the only package that holds a connection. That one chokepoint is what makes
authorization, integrity, and the read/write split possible (sections 4–5).

---

## 2. Package and layer structure

The code is a **uv monorepo** of small packages with **one-way dependencies**. A package
may only import from packages to its right in this chain; "importing upward" is a build
error, mechanically enforced by import-linter.

```
   apps                library packages (catalogue.*)              data
 ┌───────────┐   ┌──────────────────────────────────────────┐   ┌──────────┐
 │ webui     │   │                                          │   │          │
 │ pwa       │──►│ populate ─► services ─► access-api ─► db-store │─►│ SQLite   │
 │ cli       │   │                            ▲          ▲   │   │  file    │
 └───────────┘   │                            │          │   │   └──────────┘
                 │                       contracts ──────┘   │
                 │            (shared types + authz + vocab, │
                 │                    NO behaviour)          │
                 └──────────────────────────────────────────┘

   depends-on ──►        (arrows never point backwards)
```

| Package | Import path | What it does |
|---|---|---|
| **contracts** | `catalogue.contracts` | Shared data types (DTOs, `Ref`, `Impact`), open vocabularies, and the authorization contracts (`Principal`, `Policy`, `Action`, `Denied`). Pure types, no behaviour. |
| **db-store** | `catalogue.db_store` | Lowest data layer: connections (read-only vs read-write), schema, migrations, vocab seeding, the integrity guard. |
| **access-api** | `catalogue.access_api` | The one and only API for touching the database — per-entity reader/writer surfaces behind a policy gateway. |
| **services** | `catalogue.services` | Business logic: cataloguing, classification, authority resolution, dedup, covers, search, export. |
| **populate** | `catalogue.populate` | The pipelines that fill the DB: scan sweeps, staging→load, batch imports. |
| **test-kit** | `catalogue.test_kit` | Test seams: entity factories, an in-memory DB, and a fake repository so client tests run without a real DB. |
| **catalogue-webui** | — | Flask web UI **and** the HTTP/JSON API the PWA consumes. |
| **catalogue-cli** | — | Batch/admin operations (dedup, backup, content-index build). |
| **catalogue-pwa** | — | Installable, offline-first phone app; talks to the webui's HTTP API. |

### Technical details — enforcing the one-way rule

The dependency direction is enforced by an **import-linter `forbidden` ratchet**: anything
above `access-api` (services, populate, apps) is forbidden from importing `db_store` or
opening a connection directly, so `access-api` cannot be bypassed. `access-api` itself
depends only on `db_store` + `contracts`. Full package split and phasing: the private
repo-modularization plan; the client-facing layer map is `contracts/data_model.md` §11.

---

## 3. The entity model

The catalogue models **seven root entities**. A *root* is something with its own identity
and lifecycle. Each root owns some **parts** (things that live and die with it) and has
**edges** to other roots (shared references, many-to-many unless noted).

```
                     work_author              edition_author / edition_translator
       ┌────────┐  ───────────────►  ┌──────┐   ◄──────────────────  ┌─────────┐
       │ Person │                    │ Work │      edition_work       │ Edition │
       └────────┘  ◄───────────────  └──────┘   ──────────────────►  └────┬────┘
            ▲          (translators/authors are Persons)                  │ 1:N (owned)
            │                           ▲  ▲  ▲                           ▼
            │                           │  │  │                     ┌──────────┐
            │      work_subject ────────┘  │  │                     │ Holding  │
       ┌─────────┐                         │  │                     └────┬─────┘
       │ Subject │◄── edition_subject ─────┼──┼──────── (Editions)       │ owns
       └─────────┘                         │  │                          ▼
       ┌───────────┐  work_tradition       │  │                   ┌───────────────┐
       │ Tradition │──────────────────────►┘  │                   │ File backing  │
       └───────────┘                          │                   │ + provenance  │
       ┌────────────┐ collection_member       │                   │ (scan/OCR)    │
       │ Collection │────────────────────────►┘                   └───────────────┘
       └────────────┘
```

In words:

- A **Work** is an abstract text. A **Work appears in many Editions**, and an Edition may
  contain **several Works** (`edition_work` is N:N). "All editions of a text" is the
  Work→editions edge.
- An **Edition** is one published book. It **owns its Holdings** (1:N) — delete the
  Edition and its Holdings go with it.
- A **Holding** is one copy the library actually has. It owns a **file backing** (the bytes
  on disk, reached by path + content hash) and a **provenance** record (how it was
  digitized). A Holding belongs to exactly one Edition.
- **Authors live on the Work; translators live on the Edition** — the FRBR split. Both
  reference shared **Person** roots, so deleting an Edition never deletes a Person.
- **Subject**, **Collection**, and **Tradition** are lightweight tags that group Works
  (and Subjects also tag Editions).

| Root | What it is | Owns | Edges |
|---|---|---|---|
| **Work** | Abstract text (FRBR Work) | title aliases, canonical ids | authors→Person, subjects, traditions, collections, editions, work↔work, commentary |
| **Edition** | One published book | alt-ISBNs, volume-set | works, authors/translators→Person, subjects, **holdings** (owned), commentary-on→Work |
| **Person** | Author / translator / contributor | aliases, external authority ids | works, editions |
| **Holding** | A copy the library holds | **file backing** + **provenance** | its parent Edition |
| **Subject** | Topic or series heading | — | works, editions |
| **Collection** | Named grouping of works | — | works |
| **Tradition** | Lineage / tradition tag | — | works |

**"Authorities"** are not a separate entity. Authority control — resolving a name or text
to an external authority (BDRC, 84000, Wikidata, VIAF) — is a property of **Person**,
**Subject**, and **Tradition**: each carries external authority ids and goes through a
matching/dedup process. See `design/authority_matching.md`, `design/person_resolution.md`,
and `design/authority_dedup_model.md`.

### Technical details — where the full model lives

This section is a summary. The two authoritative references:

- **`contracts/data_model.md`** — the client-facing model: every entity's read fields
  (DTOs), `Ref` semantics, deletion/identity rules, vocabularies. Read this to build
  against the API.
- **`access/entity_api_model.md` §3 (the aggregate roster)** — the server-side view: each
  root's owned parts, edges, and **identity fingerprint**, plus lifecycle consequences.
- **`design/frbr_data_model.md`** — the FRBR Work/Edition/Person split and the DDL behind
  it. Companion domain docs: `design/commentary_relationships_model.md` (root-text ↔
  commentary), `design/multi_work_segmentation.md`, `design/title_recognition.md`.

---

## 4. Reads vs. writes, and authorization

Reads and writes are **physically different surfaces**, which is what makes authorization
simple. Every entity in `access-api` splits into a **reader** (handed an OS-enforced
**read-only** connection) and a **writer** (a read-write connection). A reader literally
cannot write, even with a bug; a caller who is only allowed to read is never given a
writer at all.

```
                       bind(principal, policy, db_path)
                                    │
                   ┌────────────────┴────────────────┐
                   ▼                                  ▼
          ┌─────────────────┐               ┌──────────────────┐
          │  READER surface │               │  WRITER surface   │
          │  needs: READ    │               │  needs: WRITE     │
          │  RO connection  │               │  RW connection    │
          │  plan_* / lists │               │  apply()          │
          └────────┬────────┘               └────────┬─────────┘
                   │  (physically cannot write)       │
                   ▼                                  ▼
              read-only conn ─────────────────► SQLite file ◄── read-write conn

   viewer  → reader only
   editor  → reader + writer
   api-key → reader + writer, scoped to specific entities
```

Writes never happen in one shot. Every mutation is a **`plan → apply`** two-step, so a
client can preview the full blast radius and confirm before anything changes:

```
   plan_delete(ref)            apply(plan)
        │  READ                     │  WRITE
        ▼                           ▼
   ┌──────────┐   returns   ┌──────────────────────────────────────┐
   │  build   │────────────►│  re-check (TOCTOU) → validate/normalize│
   │  Impact  │  an Impact  │  → idempotency → optimistic rev        │
   └──────────┘  (preview)  │  → integrity gate → one transaction    │
        │                   │  → bump rev, write undo + audit        │
        ▼                   └──────────────────────────────────────┘
   inspect / render:                       │
     cascades  "removes 2 holdings"        ▼
     orphans   "Work #5 left with 0 eds"   applied (atomic, recorded)
     file_ops  "2 files → trash"
     blocks    → if non-empty, STOP
```

The **`Impact`** — that preview of "what this write will do" — is a serializable value in
`contracts`. The server computes it, the web UI renders it, the PWA receives it as JSON,
and a future Swift client would decode the same thing. "What deleting this does" is one
shared value, not re-derived per surface.

### Technical details — integrity and soft-delete

- **FK closure is free; non-FK closure is the job.** Foreign keys are forced ON with
  `ON DELETE CASCADE`, so the relational graph self-cleans. The real work is the **shadow
  graph** — ids stored *outside* a foreign key (JSON blobs, id-keyed cover filenames,
  hash-keyed caches, undo snapshots). Each entity **declares** those in a registry, matched
  by the **owning** entity (not any embedded id), and `apply` purges the ones it owns in the
  same transaction.
- **Root ids are never reused.** Deleting a root (Edition/Work/Person/Subject/Collection/
  Tradition) **soft-deletes** it — sets a `deleted_at` tombstone, hides it from reads, and
  **freezes its id forever**. So a stale reference can only ever *dangle* (a clean
  `NotFound`/`StaleWrite`), never silently re-point at a different new entity. Holdings, by
  contrast, are **hard-deleted** (their content hash already guards id reuse) and their
  files move to a recoverable trash.
- **`Ref` fingerprints** revalidate identity at the moment of use — the last line of
  defense against acting on something that changed underneath you.

Full design: **`access/entity_api_model.md`** (the engine — gateway, plan→apply, registry,
soft-delete, ports/adapters) and `contracts/data_model.md` §§3–8 (the client-facing
`Ref`/`Impact`/error/authz contract).

---

## 5. How data gets in — ingest & OCR provenance

Books enter the catalogue through the **populate** pipelines. Files are discovered on disk,
staged, loaded into entities, then resolved against authorities. Scanned books additionally
carry a full **provenance** trail — where the text came from and how good it is.

```
   files on disk                                            the catalogue
   ┌────────────┐   sweep    ┌─────────┐   load    ┌──────────────┐  resolve  ┌──────────┐
   │ books/     │──────────► │ staging │─────────► │ Editions +   │─────────► │ authority│
   │ (PDF/EPUB) │  detect +  │ (raw    │  create   │ Holdings +   │  match to │ ids on   │
   └────────────┘  extract   │ records)│  entities │ Works        │  Person/  │ persons/ │
         │                   └─────────┘           └──────┬───────┘  Work      │ works    │
         │  scanned?                                      │                    └──────────┘
         ▼                                                ▼
   ┌───────────────────────┐                    ┌───────────────────────┐
   │  OCR route            │  produces          │  Holding provenance   │
   │  (Surya / re-OCR)     │───────────────────►│  born-digital vs      │
   │  page text + layout   │                    │  scanned · quality ·  │
   └───────────────────────┘                    │  re-OCR history       │
                                                └───────────────────────┘
```

- **Born-digital** files (clean PDFs/EPUBs) go straight through extraction.
- **Scanned** files are routed through **OCR**; the resulting page text, layout, and quality
  signals are recorded as **provenance** on the Holding, so the catalogue always knows whether
  a Holding's text is native or OCR'd, how good it is, and whether it has been re-OCR'd.

The OCR engine itself is a **separate project** —
[`techie-monk0/scholia-rag-ocr`](https://github.com/techie-monk0/scholia-rag-ocr) — which
does the scan→text work (Surya-based, layout- and script-aware for Tibetan/Sanskrit). The
catalogue consumes its output and records the provenance; it does not do OCR in-process.

### Technical details

- The pipeline stages live in `catalogue.populate` (`run_load`, `run_resolve`, and later
  steps); the per-step logic (detect, extract, digitize, OCR routing, segmentation,
  classification) lives in `catalogue.services`.
- The OCR itself runs in the dedicated
  [`techie-monk0/scholia-rag-ocr`](https://github.com/techie-monk0/scholia-rag-ocr) repo —
  the catalogue's `ocr_route` hands scans off to it and ingests the returned text + quality
  signals. OCR engineering (layout, columns, two-up scans, re-OCR) lives in that repo.
- Provenance is the **Holding sub-aggregate**, not a peer entity — its read path and model
  are specced in **`access/scan_ocr_provenance_model.md`**.
- A book that packs several texts is split via `design/multi_work_segmentation.md`.

---

## 6. Search

Search is built to actually **find** things in scholarly, multi-script text, not just match
substrings. It folds diacritics (*tathāgatagarbha* ↔ *tathagatagarbha*), collapses
name/spelling variants, and strips honorifics before matching.

```
   query ──► fold diacritics ──► collapse variants ──► strip honorifics ──► FTS5 index
                                                                              │
                                                                              ▼
                                                                      ranked Editions
```

### Technical details

Full-text search runs over an **FTS5** index (`edition_text_fts`) exposed on the read side
as a `Query` contract object (filters + sort + keyset/cursor pagination), so a client builds
a query and the server executes it — the same plan-vs-execute split as writes. The folding /
variant / honorific vocabularies are user-extensible (see below). Matching and dedup
algorithms: `design/authority_matching.md`, `design/authority_dedup_model.md`,
`design/person_resolution.md`.

---

## 7. Surfaces — the apps on top

Everything a person interacts with is built on the same library, and every write flows
through the same `access-api` door.

```
   ┌──────────┐   ┌──────────────┐   ┌──────────────────────────────┐
   │ browser  │   │ phone (PWA,  │   │ terminal (catalogue-cli)     │
   │          │   │ offline)     │   │                              │
   └────┬─────┘   └──────┬───────┘   └───────────────┬──────────────┘
        │ HTML           │ HTTP/JSON                  │ in-process
        ▼                ▼                            │
   ┌──────────────────────────────┐                   │
   │ catalogue-webui              │                   │
   │  Flask UI  +  HTTP/JSON API  │                   │
   └───────────────┬──────────────┘                   │
                   │  plan_* / apply / queries         │
                   ▼                                   ▼
             ┌──────────────────────────────────────────────┐
             │            access-api  (the one door)         │
             └──────────────────────┬───────────────────────┘
                                    ▼
                               SQLite file
```

- **catalogue-webui** — the Flask web UI, which also serves the **HTTP/JSON API** the phone
  app consumes. Same `Impact`/DTO shapes on the wire as in-process.
- **catalogue-pwa** — an installable, offline-first phone app. It queues writes offline and
  replays them; **idempotency keys** on writes make a replayed op apply exactly once.
- **catalogue-cli** — batch and admin operations (dedup, backup, content-index export),
  calling the library in-process.

Frontend architecture (reader engine, PWA offline model, cross-surface contract) is kept in
private frontend docs.

---

## 8. Where to read next

```
  ARCHITECTURE.md  (you are here — the map)
        │
        ├─ build a client?      → contracts/data_model.md
        ├─ the DB access engine → access/entity_api_model.md
        ├─ scan / OCR provenance→ access/scan_ocr_provenance_model.md
        ├─ external tools       → access/external_tool_dependency_contract.md
        ├─ the data model (FRBR)→ design/frbr_data_model.md
        ├─ authority control    → design/authority_matching.md · person_resolution.md
        │                         · authority_dedup_model.md
        ├─ domain algorithms    → design/commentary_relationships_model.md
        │                         · multi_work_segmentation.md · title_recognition.md
        └─ how to run it        → USAGE.md
```

The full index with one-line descriptions is **[README.md](README.md)**.

### Technical details — extending the vocabularies

The controlled vocabularies the catalogue matches against (honorifics, name-spelling
variants, organization markers, traditions, dropdown options) ship as defaults and are
**user-extensible without editing the shipped file** — drop a `vocab.local.json` next to
your database. See `catalogue.db_store.authority_vocab`.
