# External-tool dependency contract

*Normative spec for the contract between the catalogue and external tools that consume its entities
(first case: BuddhistLLM's RAG corpus). Types live in the `contracts` package
(`catalogue/contracts/external_dep.py` + `conformance.py`); the executor + `ToolRegistry`
(`catalogue/access_api/tool_policy.py`), `claim()` / `is_flagged()` / `resolve()` / `supersede()`
(`catalogue/access_api/external_deps.py`), and tool impls (`catalogue/access_api/integrations/`) live
in access-api. Design narrative + phasing: `citation_edition_contract_plan.md`. Related:
`entity_api_model.md` (§4 contracts, §6 writers), `[[abstract-protocol-layers]]`,
`[[soft-delete-decision]]`, `[[sqlite-id-reuse-hazard]]`.*

**Status (2026-07-06): the catalogue side is IMPLEMENTED** — `edition.pub_id` (schema v11, write-once
mint/immutable triggers + unique index), `edition_external_dependency` + `edition_purge_guard`
trigger (v-schema), `edition.superseded_by` forwarding (v12), `resolve`/`supersede`,
forwarding-aware `merge_into`, the `tool_policy` executor + `BuddhistLLMDependency`, `STABILITY_CHECKS`
in `integrity.py` (→ `verified_commit`), and the shared `run_stability_conformance` suite (the real
store passes it). `ocr_pipeline`'s `build_rag_manifest.py` stamps `pub_id`. **Update 2026-07-09:** the
catalogue now publishes a **versioned read-contract descriptor** (`external_read_contract.json` +
`schema_meta.external_read_contract_version`, `verify()` in CI) so consumers verify the identity
surface without shared code — see "Versioned read-contract descriptor" below. Remaining: the
**BuddhistLLM side** (store `pub_id`+`content_hash`, live-join, call `claim`), the consumer-side version
handshake in `ocr_pipeline`/`BuddhistLLM`, and cross-repo e2e.*

## Why this exists

An external tool that has consumed an edition (embedded it into a RAG corpus, cited it in answers
already given to users) holds a dependency the catalogue must respect: the edition's **identity must
never rebind to a different book**, and some catalogue operations on it warrant a warning, a
confirmation, or a refusal. This contract lets each tool **declare** those constraints without the
catalogue core knowing anything tool-specific.

## The model: identity / version / everything-else

The consuming tool stores **only**:

- **Identity** — the immutable `edition.pub_id` (UUID). The single link key. Never changes, never
  rebinds. A merged/split id leaves a **forwarding pointer** so it always resolves to the canonical
  live record — never vanishes.
- **Version** — `content_hash` / `rev`. A *staleness detector* for derived artifacts (chunks,
  embeddings), never a join key.

Everything else (paths, title, author, tradition, `text_status`) is **looked up live** by `pub_id`
via `resolve()`, never copied. This eliminates the denormalization drift class. Residual risks a key
cannot decide — correct binding at ingest, forwarding pointers on merge, text quality, and rebuilding
stale derived artifacts — are the tool's/operator's responsibility, documented in the plan §1.

## Capability / restriction / action

- **`Capability`** — the constrained operations: `PURGE`, `WITHDRAW`, `MERGE`, `SPLIT`,
  `IDENTITY_EDIT`, `DISPLAY_EDIT`. A capability no tool constrains defaults to **ALLOW**, so adding a
  capability later is safe.
- **`ExternalToolDependency.restrict(capability, entity) -> Restriction`** — one tool's stance. Pure
  policy, no I/O. Executed as a **static in-catalogue plugin** — never a live call out to the tool.
  **Multiple tools are supported**; each ships one small impl (a code subclass, or a declarative JSON
  manifest interpreted by a generic impl).
- **Registration / trust.** A `ToolRegistry` maps `tool` id → impl, **populated in code** at startup from a
  segregated `catalogue-integrations` unit (one file per tool). All tools are the owner's own and
  **trusted** — there is **no** authn, runtime self-registration API, approval gate, or sandboxing. That
  machinery is an explicit non-goal until untrusted third-party tools ever appear.
- **`Restriction`** — `severity` (`ALLOW < WARN < CONFIRM < DISALLOW`) + `reason` + side-effects
  (`force_redirect`, `enqueue_reconcile`).
- **Combine = most-restrictive-wins** — `Restriction.combine()` takes the max severity and the OR of
  side-effects across every tool an entity declares.

## Executor flow (access-api)

1. A writer (`EditionWriter` merge/delete/split) names the `Capability` and the entity `Ref`.
2. The executor reads the entity's rows in `edition_external_dependency`, resolves each `tool` to its
   registered impl, calls `restrict()`, and `combine()`s the results.
3. Apply: `DISALLOW` → raise `CapabilityRestricted`; `CONFIRM` → require operator confirmation;
   `WARN` → surface to the operator; `force_redirect` → the op must tombstone-with-forwarding-pointer,
   never hard-delete; `enqueue_reconcile` → record a change event (**deferred**, plan D7).

## Storage & lifecycle

- **`edition_external_dependency(edition_id, tool, corpus, claimed_at, detail)`** — set by
  `claim(pub_id, tool, corpus)` at ingest. **Monotonic**: never cleared, because an answer already
  given can't be recalled.
- A `BEFORE DELETE` **purge-guard trigger** on `edition` refuses a hard delete when any dependency row
  exists — the enforcement backstop while soft-delete is still inert across ~906 legacy sites.

## Stability contract (S1–S3) — the promise a consuming tool relies on

The catalogue guarantees three properties about a `pub_id`; tools depend on them and must uphold their side:

- **S1 — No rebind.** A `pub_id` never resolves to an *unrelated* edition (write-once + never reused).
- **S2 — Total resolvability.** Any `pub_id` ever handed out always resolves — to the live edition or, via a
  forwarding pointer (`merged_into`/`superseded_by`), to its canonical successor. Chains terminate; never
  dangles, never cycles.
- **S3 — Opacity (consumer-side).** A tool treats the token as opaque — never parses, casts, or infers from
  it. Whether it's an int or a UUID is the catalogue's private choice.

S1+S2 are the catalogue's promise; S3 is the tool's discipline.

**Enforced structurally** (below the ~906 legacy raw-SQL sites, so it can't drift to convention):

| Property | Mechanism |
|---|---|
| S1 write-once | `BEFORE UPDATE` trigger raises if `pub_id` changes; column `UNIQUE NOT NULL` |
| S1 no-reuse | purge-guard `BEFORE DELETE` trigger → a flagged id is frozen, never freed |
| S2 forwarding | `EditionWriter` merge/split post-condition `resolve(old).canonical == new`; `verified_commit` rolls back a write that would orphan an id |
| S2 total resolve | `resolve()` returns a non-optional record carrying `status` + `canonical_pub_id` |
| S1+S2 invariant | `integrity.py::pub_id_stable` (unique, immutable, resolves, chains terminate, no cycles) inside `verified_commit` |
| S3 opacity | the tool types the token as an opaque newtype; never fed to `int()`/regex |

**Conformance suite (cross-repo anchor).** A parametrized test in the `contracts` package takes any
`resolve()`-shaped implementation and asserts S1–S2. The catalogue runs it against its real store; each tool
runs it against its stub — both must pass in their own CI. Plus two targeted tests: an **opacity-substitution**
test (run a tool's ingest→cite flow with int, UUID, and random-string tokens; assert identical behaviour) and
a **durability canary** (a committed fixture holding a `pub_id` from an old snapshot, asserted to still resolve
against the current schema — catches a future migration silently churning ids).

## Versioned read-contract descriptor — the schema-shape handshake

S1–S3 promise the *semantics* of a `pub_id`. A separate, cheaper failure is a **shape** drift — a
column renamed or dropped from `v_holding_files`, so a consumer's raw SQL silently returns nulls or
errors. The generic schema guard (`db.py::schema_drift`) fingerprints view *names*, not view
*columns*, so it can't catch this. The fix is a **published contract descriptor + version**, verified
at each side's boundary — consistency by *checking*, not by *sharing code*.

**Two language-neutral artifacts, published by the catalogue (the system-of-record):**

| Artifact | What it is |
|---|---|
| `db_store/external_read_contract.json` | machine-readable spec of the read surface: the view + its columns, the `resolve` columns, the S1–S3 guarantees, and the compatibility policy. Committed; the version each number *means*. |
| `schema_meta.external_read_contract_version` | the version the **live DB** actually provides — stamped into every DB by `init_db`, readable with one `SELECT` over a connection the consumer already holds. |

**Consumer handshake (≈5 lines the consumer owns — no import of catalogue code):** read
`external_read_contract_version` from the DB, assert it is ≥ the version the consumer was built for,
and confirm the columns it needs are present (`PRAGMA table_info`). A **tolerant reader** selects
named columns and ignores extras, so additive growth never breaks it.

**Compatibility / bump policy.** The read surface grows **additively** within a version: new columns
may appear. **Removing or renaming a column, or weakening a guarantee, bumps the version** (and the
descriptor) — a breaking change the consumer's `>=` assert then rejects loudly.

**Provider truthfulness.** `external_contract.verify(conn)` asserts the live DB honours the published
descriptor (every declared column really exists on the view/resolve table, and the DB stamps the
descriptor's version); run in the catalogue's CI so it can never ship a descriptor that lies. This is
the column-level guarantee the name-only schema guard leaves open. (`test_external_read_contract.py`;
the pinned column set lives with `test_holding_files_view.py::GUARANTEED`.)

This composes with the conformance suite above: **descriptor + version = shape**; **S1–S3 +
`run_stability_conformance` = semantics**. A consumer that checks both is consistent with the
catalogue without depending on its code. (Live DBs stamped before this landed report version `None`
until their next `init_db`; a consumer treats `None` as "older than v1".)

## First implementation — BuddhistLLM (`tool = "buddhistllm"`)

| Capability | Restriction |
|---|---|
| `PURGE` | `DISALLOW` — the load-bearing rule (id must stay frozen) |
| `MERGE` | `WARN` + `force_redirect` |
| `WITHDRAW` | `ALLOW` (optional `WARN`) |
| `IDENTITY_EDIT` | `WARN` (content re-scan forks a new `pub_id` + `superseded_by`) |
| `DISPLAY_EDIT` | `ALLOW` (fixes propagate into citations via live-join) |

`enqueue_reconcile` is off for v1 ("do nothing" — plan D7 / `later_tasks.md`).
