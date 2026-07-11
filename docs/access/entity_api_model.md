# Entity API model — architecture of the DB access layer (2026-06-24)

**Status: design (locked where marked).** The architecture for `access-api`: the **one** API
all clients must use to read and write the catalogue, modelled as **entity aggregates** with
**integrity built into every write**. One generic engine + small per-entity declarations + a set
of **serializable contracts shared by server and client** (Python server, Python webui/cli,
HTTP/PWA, future Swift).

Companion specs: `docs/access/scan_ocr_provenance_model.md` (the Holding sub-aggregate). Memory:
[[abstract-protocol-layers]], [[sqlite-id-reuse-hazard]].

---

## 0. Implementation status (updated 2026-06-24)

The design below is **partly built**. Shipped behind `bind()` + policy gate + RO/RW + plan→apply,
full suite + import-linter green:

- **Entities:** `Holding` (read / `set_text_status` update / delete with its non-FK closure),
  `Edition` (delete: holdings cascade + cover-art purge + semantic-orphan detection), `Person`
  (read + soft-delete with the `person_identity_ok` guard as a `Ref` fingerprint + second-order
  authorless-work orphans), `Work` (read + soft-delete + **`merge`** with `LinkRepoint` edge
  re-pointing, over the **owning-entity non-FK registry** that fixes the `purge_work_refs`
  over-purge), and the leaf roots `Subject`/`Collection`/`Tradition` (read + delete via a generic
  engine). `scan_ocr` read path exists.
- **`Session`/UoW** (multi-aggregate, one transaction); the serializable `Impact`/`Ref`/`Orphan`
  contracts; `OrphanPolicy` (FLAG/GC/REFUSE); the authz contracts + error taxonomy.
- **Soft-delete** is the chosen root lifecycle — **RESOLVED, not AUTOINCREMENT** (§6).
- **Access layer and storage implementation are split** via a per-entity port–adapter (§2.1).

Not yet built: `OrphanSweep`/`verify`, the formal `IntegrityGate`, `Rev`/`Query`, provenance v4 +
the `Backing` port, and the import-linter `forbidden` ratchet. (The owning-entity non-FK registry
exists for `work`/`person`/`edition`/`holding` review-item ownership; the `services`-side
`purge_work_refs` over-purge fix + the `sweep_dangling_refs` backfill remain Phase-4 tail items.)

---

## 1. Principles

1. **Entities are aggregates, not tables.** Each root owns its own fields, its **edges** to other
   roots, and its **lifecycle consequences**. A client never assembles an Edition from six tables;
   it asks the Edition module.
2. **FK closure is free; non-FK closure is the job.** Foreign keys are forced ON
   (`db.py:223`, self-verified) and every `edition_id`/`work_id`/`person_id` FK declares
   `ON DELETE CASCADE`, so the relational graph self-cleans (verified: zero dangling FK rows).
   The integrity work is the **shadow graph** — ids stored *outside* an FK (JSON blobs, id-keyed
   filenames, hash-keyed caches, undo snapshots) that no cascade can see. (See §6.)
3. **Integrity is a precondition of every write**, not a test run later. Every mutation is
   `plan → check → apply`; `apply` refuses if the plan would dangle a ref or rebind a recycled id.
4. **One contract, both sides.** The shapes a mutation produces (notably `Impact`) are
   **serializable** and live in `contracts`, so the server computes them, the webui renders them,
   the PWA receives them as JSON, and a future Swift client decodes the same thing. The API's
   boundary object — what "deleting this orphans X and trashes 2 files" *is* — is shared, not
   re-derived per client.
5. **Generic engine, declarative entities.** Adding an entity is *declaring its shape*
   (fields, edges, non-FK refs, orphan rule, identity fingerprint); the generic engine supplies
   load / iterate / plan / apply / sweep / integrity. Bespoke per-entity code shrinks to a spec.
6. **Reads and writes are physically separate surfaces — so authorization is trivial.** Every
   entity module splits into a **reader** (`reads.py` — iterators, `plan_*` previews) and a
   **writer** (`writes.py` — `apply`, mutations). A reader is handed an OS-enforced **read-only**
   connection and needs only the `READ` action; a writer gets a read-write connection and needs
   `WRITE`. So "can this caller do this?" is answered at the *surface* — viewer→reader only,
   editor→reader+writer, an api-key scope can narrow to specific entities — never per-statement,
   and a read path *physically cannot* write even with a bug. (Detail in §5 / §9.)

## 2. Layered structure

```
contracts/                       # shared, serializable, NO behaviour
  dto/        Edition, Work, Person, Subject, Holding, Collection, Tradition + edge types
  Ref            (kind, id, fingerprint) — a revalidatable entity reference
  Impact         (the serializable plan a mutation will execute)
  OrphanDecision / IntegrityViolation
  Principal / Policy / Action / AccessMode / Denied        (authz, from the read/write design)

access-api/                      # the ONE DB-touching package (as built)
  gateway.py    bind(principal, policy, db_path) -> Access; RO/RW conns; policy gate; commit/rollback
  session.py    Session — multi-aggregate unit of work (one transaction)
  registry.py   per-entity non-FK reference declarations (holding hash-caches; edition cover-art keys)
  _files.py     shared filesystem effects (trash-to-.trash)
  _leaf.py      generic leaf engine: LeafSpec + LeafReader/Writer + LeafStore(port)/SqliteLeafStore
  holdings/     reads.py · writes.py · store.py (HoldingStore port + SqliteHoldingStore) · __init__ (Repo)
  editions/     reads.py · writes.py · store.py (EditionStore port + SqliteEditionStore) · __init__ (Repo)
  subjects.py · collections.py · traditions.py   # one LeafSpec each
  scan_ocr.py   # the Holding provenance sub-aggregate (read path)
  (planned: persons/ · works/ · engine = IntegrityGate / OrphanSweep / verify)
```

`access-api` depends only on `db_store` + `contracts`. Everything above (services, webui, cli,
populate) talks **only** to `access-api` — enforced by the import-linter `forbidden` ratchet
(repo plan §"Rolling it out").

## 2.1 Access layer vs. storage implementation (port–adapter)

The entity API is **storage-agnostic**. Each entity splits into two halves across a hard seam, so
*what the API does* is independent of *how the bytes are stored*:

- **Access layer** (`reads.py` / `writes.py`, or the generic `_leaf.py`): the policy gate, the
  `plan→apply` orchestration, `Impact` computation, fingerprint/orphan logic, the registry whitelist.
  It holds **no SQL and no connection**.
- **Storage implementation** — a per-entity **`Store` port** (an ABC of pure data operations:
  `get`, `list_by_*`, `delete_fields`, `current` (the write-side recheck read), and staged mutations
  `update`/`delete`/`tombstone`/`purge_cache`) plus a concrete **adapter**. `SqliteHoldingStore` /
  `SqliteEditionStore` / `SqliteLeafStore` implement the ports over the gateway's RO/RW connections
  (and own incidental I/O, e.g. cover-art file enumeration).

The reader/writer take an **injected** store; the Repo defaults to the SQLite adapter. So a different
backing — an **in-memory fake** for `test-kit` (§11), or an **HTTP adapter** to a remote access-API —
is a drop-in with **no change to the access layer**. Convention: store reads use the RO connection;
staged mutations use RW and **do not commit** — the access layer's `Session`/`apply` owns the
transaction via `Access.commit()` / `Access.rollback()` (the single unit-of-work seam an alternate
backend rebinds; `_stage(impact)` is handed no raw connection).

This is the same abstraction as a **Holding's file being one pluggable *backing*** of the abstract
holding (filesystem now, HTTP/object-store later), applied uniformly to every entity's persistence.
The provenance/`scan_ocr` increment will add the explicit `Backing` port for a holding's *bytes* —
the same shape as these entity `Store`s. ([[abstract-protocol-layers]]: client-supplied strategy ABC
+ protocol-agnostic executor.)

## 3. The aggregate roster

| Aggregate (root) | Owns (parts) | Edges (to other roots) | Identity fingerprint |
|---|---|---|---|
| **Work** | aliases | authors, subjects, traditions, collections, editions (`edition_work`), work↔work `relationship`, commentary | title-fold + author set |
| **Edition** | ISBNs, volume-set | works (`edition_work`), translators, authors, subjects, **holdings**, commentary-on | title-fold + isbn |
| **Person** | aliases, external-ids | works (`work_author`), editions (translator/author) | name-fold + dates (the existing `person_identity_ok` guard) |
| **Subject** | — | works, editions | name |
| **Holding** | **File** (path/hash), **provenance** (scan_ocr digitization events) | edition, library-root | content_hash |
| **Collection** | — | works | name |
| **Tradition** | — | works | name |

`scan_ocr` (provenance) is the Holding sub-aggregate, not a peer.

## 4. Generic contracts (the reusable shapes)

- **`Ref(kind, id, fingerprint)`** — every cross-reference is a Ref, never a bare id. The
  fingerprint lets a consumer re-validate identity before acting (the [[sqlite-id-reuse-hazard]]
  guard, generalized from `person_identity_ok`).
- **`Aggregate[T]`** — `.id`, typed `.fields`, typed `.edges`, and `.refs()` → its declared non-FK
  referents. JSON-serializable (→ a DTO in `contracts`).
- **`Impact`** — the serializable preview/plan of a mutation:
  - `cascades` — rows the DB FK-cascade will remove (informational)
  - `orphans` — roots left unanchored (Work with 0 editions, Person with 0 works/editions), each
    with the `OrphanDecision` the policy chose
  - `ref_purges` — non-FK refs to delete: cover files, cache rows, `payload_json`/`promotion`
    entries (from the registry, §6)
  - `file_ops` — file trashes/moves
  - `link_repoints` — edges to re-point instead of drop (e.g. `edition_commentary_on` → merge winner)
  - `blocks` — `IntegrityViolation`s that make the plan **un-appliable**
- **`OrphanPolicy`** (strategy ABC) — `decide(orphan) -> GC | FLAG | REFUSE`. Client-supplied:
  webui FLAGs for review, a batch job GCs, an import REFUSEs. (The MoveResolver pattern,
  [[abstract-protocol-layers]].)
- **`IntegrityGate`** — runs the checks: input validation/normalization (§5), registry-derived
  `dangling_refs(conn)`, identity fingerprints, and each entity's declared **semantic-orphan rule**.
- **`Session`** — a unit-of-work over **one transaction**: several entity mutations stage into it,
  then one `commit()` (or `rollback()`) makes them all-or-nothing, producing one combined `Impact`.
  `bind()` hands out a session (§5).
- **Error taxonomy** — `NotFound | Conflict | ValidationError | IntegrityViolation | StaleWrite |
  Denied`, each with a stable code + an HTTP-status mapping, so every client (webui/PWA/cli/Swift)
  handles failure the same way. Part of the contract, like `Impact`.
- **`Rev` / `IdempotencyKey`** — a per-row `rev` stamp for optimistic concurrency (§5) and an
  optional idempotency key on writes so a replayed offline op applies once (§5).

## 5. The write contract: plan → apply (every mutation)

```python
acc = bind(principal, policy, conn)                    # gateway: RO+RW conns, policy
plan = acc.editions.plan_delete(ref, policy=orphan_policy)   # READ: computes an Impact, mutates nothing
# `plan` is a serializable Impact — inspect it, send it to the UI, authz-check it
if plan.blocks: ...                                    # integrity violations → cannot apply
result = acc.editions.apply(plan)                      # WRITE: re-checks (TOCTOU), runs atomically, records undo
```

- `plan_*` is a **read** (RO connection, `READ` action) — cheap, side-effect-free, authz-gated.
- `apply` is a **write** (RW connection, `WRITE` action, policy-gated). It runs a fixed pipeline:
  1. **validate + normalize** the input (NFC, ISBN checksum, required fields, vocab membership) —
     the DB never stores malformed data; centralizes today's scattered normalization (`names.py`,
     isbn).
  2. **idempotency** — if the op carries an `IdempotencyKey` already applied, return the prior
     result instead of re-applying (so the **offline PWA** replaying a queued write can't double it).
  3. **optimistic concurrency** — if the plan was built against `rev` N but the row is now N+1,
     raise `StaleWrite` instead of clobbering (two surfaces touch the data: webui + the synced
     device replica).
  4. **re-run the integrity gate** against current state (TOCTOU guard between plan and apply).
  5. **execute in one transaction**, bump `rev`, write an `undo_log` snapshot (fingerprinted, so
     undo can't mis-attach to a recycled id), and record an **audit** row (`actor` = `Principal`,
     time, op, target) — the write-side complement to OCR provenance.
- `create / update(rename) / merge / relink` follow the same plan→apply shape. `merge` re-points
  edges (it does not drop `edition_commentary_on` — finding #4); `delete` purges registry refs
  (it calls `purge_edition_art` etc. — finding #3).

**Session / unit-of-work (multi-aggregate atomicity).** Real operations span aggregates — an
import touches edition + holdings + works + persons; a cascade-merge touches several roots. Those
must be **one** transaction:

```python
with acc.session() as s:                      # one transaction, one commit
    ed   = s.editions.create(...)
    s.holdings.attach(ed, file)
    s.works.link(ed, work_ref)
    combined = s.impact()                      # the merged Impact across all staged ops
# commit on clean exit; rollback (and undo nothing — nothing committed) on exception
```

A bare `apply` is just a one-op session. **Checkpoint before destructive bulk applies** — a
session whose Impact crosses a configurable blast-radius threshold snapshots first (generalizing
the pre-migration `.bak` + `sandbox.fork` you already do).

*As built:* the session stages explicit plans —
`s.stage(acc.editions.writes, acc.editions.writes.plan_delete(ref))` — collecting deferred file-ops
and exposing `s.impact()` (the combined `op="session"` preview); it commits on clean `with`-exit and
rolls back on exception (one transaction via `Access.commit/rollback`). The `s.editions.create(...)`
/ `s.holdings.attach(...)` sugar above is the **target** ergonomics once `create`/`attach`/`link`
land; checkpoint-before-destructive is not yet built.

## 6. Integrity model — the non-FK closure (the core)

Integrity = **FK closure (DB) + declared non-FK closure (registry) + identity stability +
consume-time revalidation**, in defense-in-depth layers:

**The non-FK reference registry.** Each entity declares *every place its id appears outside an
FK*, so the shadow graph is **declared, not discovered** — a new non-FK store can't silently
re-introduce the bug. Seeded from the live audit:

| Registry entry | Kind | Remediation |
|---|---|---|
| `review_queue.payload_json` (edition/holding/work/person/file_hash) | JSON blob | FK-ify if possible, else purge + revalidate |
| `promotion.holding_id` + `work_ids`/`person_ids` JSON | mixed | FK-ify the col; purge/revalidate the JSON |
| cover/pinned art `e<id>*` files | id-keyed file | `purge_edition_art` on delete + `sweep_orphan_covers` |
| 7 hash caches (raw_extract, parsed_toc, page_text, section, classification, resolver, gloss) | hash-keyed | sweep (unbounded-growth + stale-inherit risk) |
| `undo_log` snapshots | id snapshot | fingerprint-guarded restore |
| `edition_commentary_on` | FK pair, dropped on work-merge | re-point in `merge` Impact |

**Match by the OWNING entity, not any embedded id (LOCKED — the over-purge lesson).** A payload
often embeds *secondary* ids. `review_queue` items of type `edition_metadata`/`title_proposal`
are **edition-owned** (accept mutates `edition.title`) yet also carry a `work_id` — so purging
"any item whose payload contains `work_id == wid`" on a work delete wrongly drops edition
proposals for *live* editions (≈254 false positives in the live data — the `purge_work_refs` bug,
ff422ed). The registry therefore declares, per `item_type`, its **owning `(entity, key)`**, and
purge/sweep match **only** items that entity *owns*: a work-delete drops only work-owned types
(`work_canonical`/`work_authorship`/`work_merge`), never edition-owned ones. So a registry entry
is `(item_type → owner_entity, owner_key)`, not `(table → any id-bearing column)`.

Three layers, all registry-driven:
- **(a) purge-on-delete** — the entity `Impact` enumerates registry refs **it owns** and purges
  them in the same transaction. Primary path.
- **(b) orphan-sweep** — `OrphanSweep` re-derives orphans from every registry and cleans them. The
  correct backstop for **dir-less batch paths** (`match.merge_editions`, `edition_consolidate`,
  `exclude_purge`) that hold only a DB handle — so we do **not** thread cover-dirs through every
  signature. Also catches drift.
- **(c) re-validate-at-consume** — any consumer of a stored id (`bind_person`→`accept_authority`,
  `accept_person_work`, `revert_proposal`, undo restore) checks the `Ref` fingerprint before
  acting. `dangling_refs(conn)` is the audit query behind it.

**Root fix — non-recycling identity (LOCKED).** `id INTEGER PRIMARY KEY` *without* `AUTOINCREMENT`
lets SQLite reuse a freed id, so a stale stored id silently **rebinds onto a different new entity**
(findings #1/#2/#6 — corruption). Make root ids **monotonic / never reused**. Then a stale ref can
only ever **dangle** (caught by layer c), never mis-resolve — converting the worst class from silent
corruption to a clean error. **Realized via soft-delete (below), not AUTOINCREMENT:** a tombstoned
root keeps its row, so its id is never freed and the same non-recycling guarantee holds without the
~592 MB PK rebuild.

**Shrink the shadow graph (LOCKED principle).** Prefer to **FK-ify** a non-FK id reference (give
`review_queue`/`promotion` real nullable FK columns with `ON DELETE SET NULL`, JSON becomes
display-only) so cascade absorbs it. Registry + sweep is for the *genuinely* non-FK cases
(hash-keyed caches, id-in-filename art, undo snapshots).

**Soft-delete (RESOLVED 2026-06-24 — chosen over AUTOINCREMENT).** Catalog **roots**
(edition/work/person/subject/collection/tradition) carry a nullable `deleted_at` tombstone: a delete
sets it, the row persists, and its **id is frozen** (never reused), so the recycled-id rebind class
disappears **without** the risky ~592 MB `AUTOINCREMENT` PK rebuild (an additive `deleted_at` column
is a seconds-long migration, already applied to the live DB). Reads filter `deleted_at IS NULL`;
`restore()` is a flag-flip undo. **Holding hard-deletes** (≈1-to-1 with a file in live data —
668 holdings / 667 distinct paths / **0** hashes spanning >1 edition; its `content_hash` already
guards id reuse). So **deleting an Edition** = tombstone the edition (id frozen → its cover-art /
review refs stay safe) + HARD-delete its holdings + trash their files/caches + orphan-check counting
only **live** editions; a GC'd orphan work tombstones too, while edge-links stay (filtered on read)
so `restore` is a pure flag-flip. This retires most of the AUTOINCREMENT/sweep machinery the rest of
§6 was hedging against. **Scope note:** because tombstones must be hidden, *every* read of a root
table must filter `deleted_at IS NULL`; the access-API readers do, but the ~906 legacy raw-SQL sites
do not — so soft-delete is **inert on the live app** until Phase 4 routes reads through `access-api`
(the access-API soft-delete writers aren't wired into the app yet; `services` still hard-deletes).
`wishlist_item` (schema v6, books wanted-not-owned) is also a soft-deletable root (`deleted_at` +
`rev`, read via `v_live_wishlist_item`), but exposed as a FLAT repo (`access_api/wishlist.py`, like
`CaptureRepo`) rather than a plan/apply aggregate; its `matched_edition_id` is a real FK with
`ON DELETE SET NULL` (safe — the referenced edition root soft-deletes, id frozen).

**Declarative backstops + a health command.** Push invariants the schema can hold — `UNIQUE`
(e.g. `normalized_key`), `NOT NULL`, `CHECK` — into `schema.sql`, so a bug *in* the API still
can't write a duplicate or a null-where-required. A `verify` health command runs `PRAGMA
integrity_check` + `foreign_key_check` + `dangling_refs` + `OrphanSweep` in report mode — the
consistency check that sits *beneath* the app layer.

## 7. Serving client AND server

The boundary is the **contract**, and `Impact` is its lingua franca:

- **Server (`access-api`)** computes `Impact` and applies it.
- **Python clients (webui routes, cli)** call `plan_* → apply` directly against the repositories.
- **HTTP / PWA** — the server serialises `Impact` to JSON; the browser renders the real blast
  radius ("orphans Work #5 + Person #12, purges 3 covers, trashes 2 files"), the user confirms,
  the client POSTs `apply`. Same object, no client-side re-derivation.
- **Future Swift** — decodes the same JSON `Impact`/DTOs (generated from `contracts`).

So a delete-preview is identical whether it renders in a terminal, a web modal, or an iOS sheet —
because "what this write does" is a typed, shared value, not per-surface code.

## 8. Read iterators

Readers return **iterators over aggregates and their edges**, RO-connection-backed and lazy for
large sets: `editions()`, `edition(id)` (eager-loads its edges), `editions_by_subject(id)`,
`holdings_of(edition)`, `works_orphaned()`. An aggregate exposes its edges as typed iterables
(`edition.works`, `edition.holdings`) so traversal never drops to raw SQL.

Clients also need to **find**, not just navigate, so the read side exposes a small query surface:
filter + sort + **keyset/cursor pagination** (not unbounded lists over HTTP), and **full-text
search** via the existing FTS5 index (`edition_text_fts`). A `Query` is a contract object (filters,
sort, cursor) so the PWA can build one and the server runs it — same plan-vs-execute split as writes.

## 9. Authz integration

`bind(principal, policy, conn)` returns the entity repositories wired to an OS-enforced **RO
connection** for reads/plans and an **RW connection** for applies, with every op declaring its
`Action`; the gateway checks `Policy` before dispatch (viewer→READ, editor→READ+WRITE; api-key
scopes narrow). `plan_*` needs READ, `apply` needs WRITE — so previewing a delete and performing
it are independently authorizable.

## 10. Mapping today's scattered code → the generic modules

This formalizes existing logic, it doesn't invent it:

| Today (scattered) | Becomes |
|---|---|
| `work_merge.plan_merge`, `contributor_edit.plan_merge`/`apply_merge` | the generic `plan_merge`/`apply` on Work/Person |
| `entity_undo.delete_edition`/`merge_editions`, `match.merge_editions` | Edition `plan_delete`/`plan_merge` + `apply` |
| `catalogue_review._gc_persons`/`_gc_work`, `promote.revert_proposal` | the `OrphanPolicy` GC path + `OrphanSweep` |
| `relink.MoveResolver`/`FingerprintResolver` | `OrphanPolicy` / relink strategies |
| `integrity.py` (FK/orphan tests) | the `IntegrityGate` preconditions + `dangling_refs` |
| `purge_edition_art`, `sweep_orphan_covers`, `person_identity_ok` | registry remediations + `Ref` fingerprinting |

## 11. Testing

`test-kit` ships the seams: aggregate **factories** (build a valid Edition+holdings+works graph in
one call), an **in-memory DB** fixture, and a **fake repository** implementing the same contracts
so client (webui/cli) tests run without a real DB. The 7 audit findings + the `purge_work_refs`
over-purge are the canonical regression set for the integrity engine; the error taxonomy and
`Impact` shape get round-trip (serialize→deserialize) tests so the client/server contract can't drift.

## 12. Decisions

**Locked:** non-recycling root ids — realized via **soft-delete tombstones, not AUTOINCREMENT**
(roots get `deleted_at` & freeze their id; Holding hard-deletes — §6); the **access layer is split
from storage** via a per-entity `Store` port + swappable adapter (§2.1); FK-ify-where-possible before
registry; non-FK refs must be *declared* in the registry and matched by **owning entity, not any
embedded id**; `plan→apply` for every write (validate → idempotency → optimistic-`rev` → integrity →
atomic+audited); `Impact` is the shared serializable contract; `access-api` is the sole DB-touching
package (import-linter ratchet).

**Resolved during build:** ~~soft-delete vs hard-delete~~ → soft-delete on roots, hard-delete on
Holding (§6). ~~AUTOINCREMENT migration~~ → not needed; tombstones freeze ids without a PK rebuild.

**Open (decide during build):**
1. **Edge ownership.** `edition_work` is between two roots — which side *writes* it (the other reads)?
2. **Semantic-orphan rules per entity** (Work with 0 editions = valid stub, per FRBR).
3. **`review_queue`/`promotion` FK-ification** vs. registry+sweep.
4. **Concurrency depth** — a single `rev` stamp (lost-update detection) is likely enough; full
   conflict-merge for offline edits is probably over-scope for a single user.

## 13. Rollout

The **shape of Phase 3** (build the API): generic engine + contracts (incl. `Session`, error
taxonomy, `Query`, `Rev`); declare `Holding` (with `scan_ocr`) + `Edition` first as templates (they
exercise files, provenance, the worst orphan classes, and multi-aggregate sessions); land
non-recycling ids + the ownership registry + `OrphanSweep` + the `verify` health command; then
entity-by-entity under the import-linter ratchet (Phase 4), interleaving the `services` split (C).
The robustness layers (validate/normalize, idempotency, optimistic `rev`, audit, checkpoint,
query/pagination, test factories) land **with the engine**, not bolted on later.

## 14. Follow-up — `purge_work_refs` over-purge + one-time orphan backfill (after the id-reuse rework)

Concrete tail of the id-reuse fix (`ff422ed`), to do **after** that rework lands. Both are about
pending `review_queue` items / `promotion` rows pointing at deleted entities.

1. **Bug: `dangling_refs.purge_work_refs` over-purges.** It drops any pending item whose payload
   contains `work_id == wid`, but `work_id` is only ever a *secondary* ref — the two types that
   carry it (`edition_metadata`, `title_proposal`) are **edition-owned** (accept mutates
   `edition.title`, per `work_titles._apply_title`). So a work delete/merge wrongly deletes
   edition proposals for *live* editions (~254 today). Edition + person paths purge correctly.
   **Status:** the **access-API `Work` aggregate does this correctly already** — its delete/merge go
   through the `REVIEW_ITEM_OWNERS` ownership registry (`access_api/registry.py`, §6), touching only
   work-owned types (`work_authorship`/`work_canonical`) and re-pointing rather than dropping on
   merge (regression-tested in `test_access_works.py`). What remains is the **`services`-side**
   `dangling_refs.purge_work_refs` (still on the legacy delete paths until Phase 4 routes them
   through `access-api`) — apply the same registry there: a ~3-line change, no DB mutation.

2. **One-time backfill** of already-orphaned rows (purge fires only on *future* deletes). With
   correct primary-subject matching the real counts are ~ `edition_metadata` w/ dead edition ≈7,
   `book_toc_pattern` w/ dead holding ≈2, plus `person_authority` w/ dead person + `ingest` w/ dead
   holding/edition (recheck). **Do NOT** match secondary `work_id` (the 254 false-positive). Also
   scrub `promotion.work_ids`/`person_ids` of dead ids (~307 / ~128) and ~5 `promotion` rows with a
   dead `holding_id`. **Deliver** as a `sweep_dangling_refs` CLI (dry-run default, mirrors
   `sweep_orphan_covers`), reusing the same ownership registry; snapshot the DB first. The
   validate-at-consume guards (`verify.person_identity_ok`) already neutralize the correctness risk,
   so this is **hygiene, not urgent.**
