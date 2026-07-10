# Catalogue data model & API contract

A reference for building a **client** against the catalogue. It describes the entities, how they
relate, the objects that cross the API boundary, and the read/write semantics you code against. It
is deliberately free of server internals — nothing here assumes a particular database, language, or
transport. Whether you call the API in-process or over HTTP, the shapes and rules below are the
contract.

> The catalogue describes a library of books (mostly Buddhist classical texts and their modern
> editions). The model follows **FRBR**: an abstract *Work* (a text) is realized by published
> *Editions* (books), of which the library holds physical/digital *Holdings* (copies).

---

## 1. Core concepts

- **Entities are aggregates, not tables.** You read and write whole entities (an Edition, a Person)
  through that entity's surface. You never assemble one from joins; the API owns the shape.
- **Every cross-reference is a `Ref`, not a bare id** (§4). A `Ref` carries an identity
  **fingerprint** so a client can detect when the thing it references has changed underneath it.
- **The boundary objects are serializable values** — `DTO`s (what you read), `Ref`, `Impact` (what a
  write will do), and a fixed `Error` taxonomy. The same shapes are produced by the server and
  consumed by every client, so a preview rendered in a terminal, a web modal, or a mobile sheet is
  the *same value*, not re-derived per client.
- **Writes are a two-step `plan → apply`** (§6). A *plan* is a side-effect-free preview (an
  `Impact`); *apply* executes it. This lets a client show "what will happen" and get confirmation
  before anything changes.
- **Reads and writes are separately authorized** (§7). Previewing a change needs only READ; applying
  it needs WRITE.

---

## 2. The entity model

Seven **root** entities. Each owns some **parts** (things that live and die with it) and has
**edges** to other roots (shared references). Edges are many-to-many unless noted.

```
                 work_author          edition_author / edition_translator
        Person ───────────────► Work ◄──────────────────────── Edition ──────► Holding
          ▲                      │  ▲   edition_work             │  (1:N, owned)    │
          └──────────────────────┘  │                            │                  ▼
                                     │                       edition_subject     File backing
        Subject ─── work_subject ────┤                            │              (+ provenance)
        Tradition ─ work_tradition ──┤                            │
        Collection ─ collection_member┘                           ▼
                                                               Subject
```

| Root | What it is | Owns (parts) | Edges (to other roots) |
|---|---|---|---|
| **Work** | An abstract text (FRBR Work) — a classical/source work | aliases (titles), canonical ids | authors → Person, subjects → Subject, traditions → Tradition, collections → Collection, editions → Edition, work↔work relations, commentary |
| **Edition** | A published manifestation — one book | alternate ISBNs, volume-set grouping | works → Work, authors/translators → Person, subjects → Subject, **holdings** (owned, 1:N), commentary-on → Work |
| **Person** | An author / translator / contributor | aliases, external authority ids | works → Work, editions → Edition |
| **Holding** | A copy the library holds — one file/manifestation instance of an Edition | its **file backing** + **provenance** (digitization history) | edition (its parent) |
| **Subject** | A topical or series heading | — | works, editions |
| **Collection** | A named grouping of works (e.g. a series/set) | — | works |
| **Tradition** | A lineage/tradition tag | — | works |

**Key relationship notes a client must respect:**

- A **Work appears in many Editions** and an Edition may contain **several Works** (`edition_work` is
  N:N). "All editions of a text" is the Work→editions edge.
- A **Holding belongs to exactly one Edition** (it is an owned part, 1:N). Deleting an Edition
  removes its Holdings.
- **Authors live on the Work; translators live on the Edition** (FRBR split). An Edition may also
  carry edition-level authors. All of these reference shared **Person** roots — deleting an Edition
  never deletes a Person.
- A **Holding has one logical content** but its bytes are reached through a **backing** (a local
  file today; the model allows other backings such as an HTTP/object-store resource). Treat the
  file path as *one* way to reach the holding, not the holding's identity.

### Entity fields (the read shapes)

The API returns **DTOs** — frozen records. The fields below are the stable, client-relevant set;
a DTO may expose more over time (additive only). Native-script title fields (Sanskrit/Tibetan) and
review/workflow fields exist on some entities and are additive.

- **Edition**: `id`, `title`, `subtitle`, `isbn`, `year`, `publisher` (also: volume designation &
  volume-set grouping, language, alternate ISBNs).
- **Holding**: `id`, `edition_id`, `text_status` (§9), plus its **file backing** (a locator + a
  content fingerprint) and its **provenance** sub-record (how it was digitized).
- **Person**: `id`, `primary_name`, `dates`, `external_id` (authority id, e.g. BDRC/Wikidata/VIAF),
  `verification_status`.
- **Work**: `id`, canonical classification (root vs commentary, canonical system/number); its
  **titles** are aliases (a Work has no single title column — it has title aliases in several
  scripts/schemes).
- **Subject**: `id`, `name`, `kind` (`topic` | `series`).
- **Collection** / **Tradition**: `id`, `name`.

---

## 3. Identity, references & lifecycle

### `Ref` — a revalidatable reference
```
Ref { kind: string, id: integer, fingerprint: string | null }
```
`kind` is the entity type (`"edition"`, `"work"`, `"person"`, `"holding"`, `"subject"`,
`"collection"`, `"tradition"`). `fingerprint` is a stable function of the entity's identity
(e.g. an edition's title+ISBN, a holding's content hash). When you hold a `Ref` and later act on
it, the server **revalidates the fingerprint**; if it no longer matches, you get a `StaleWrite`
rather than a silent write to the wrong thing. **Always round-trip the `Ref` you were given** into
the write you build from it.

### Identity stability & deletion semantics (important)
- **Root ids are never reused.** Once an Edition/Work/Person/Subject/Collection/Tradition has an id,
  that id will never refer to a different entity — even after deletion. A stored reference can become
  *stale* (the entity is gone) but can never silently **re-point** at a new, unrelated entity.
- **Deleting a root is recoverable and hides it from reads.** A deleted root disappears from all
  read results (you will get `null` / an empty list / `NotFound`), but it is retained server-side and
  can be restored. Build clients to treat "not found in reads" as deleted-or-absent, not as
  "id available for reuse."
- **Holdings are removed outright** when deleted (directly, or because their Edition was deleted).
  Their files are moved to a recoverable trash, not the catalogue's concern to a client.
- A client should **not cache entity lists indefinitely** assuming ids stay live; re-read, or use the
  `Ref` fingerprint to detect change.

---

## 4. Reading

Read surfaces return DTOs (or iterators of DTOs) and never mutate. Reads only ever show **live**
(non-deleted) entities.

- **By id**: `get(ref|id) -> DTO | null`.
- **By relationship / navigation**: e.g. holdings of an edition, editions of a work, subjects of a
  work. An entity's edges are exposed as typed collections so you traverse the graph without joins.
- **Find (query)**: a `Query` value — filters + sort + **cursor/keyset pagination** (not unbounded
  lists) — and **full-text search** over edition text. A `Query` is itself a serializable contract:
  the client builds it, the server runs it (same plan-vs-execute split as writes). Always paginate;
  do not assume a result set fits in one response.

---

## 5. Writing — `plan → apply`

Every mutation is two steps:

```
plan = <entity>.plan_<op>(ref, …)     # READ: returns an Impact; changes NOTHING
# inspect plan.blocks / plan.orphans / plan.file_ops … render it, get confirmation
result = <entity>.apply(plan)         # WRITE: executes atomically, returns the Impact applied
```

- A **plan** is a serializable **`Impact`** (§6) describing exactly what `apply` would do. It is
  side-effect-free and only needs READ rights — so you can preview a destructive action and show the
  user its blast radius before they commit.
- **`apply`** needs WRITE rights. It **re-checks** the plan against current state (so a change since
  you planned is caught, not clobbered → `StaleWrite`), runs **atomically** (all-or-nothing), and
  returns the `Impact` it applied as a receipt.
- An `Impact` carrying **`blocks`** is **not appliable** — `apply` will reject it. Check
  `blocks` (or an `appliable` flag) before offering "confirm."

Operations follow this shape: **create**, **update** (incl. rename), **delete**, **merge** (fold one
entity into another, re-pointing its edges), **relink**. (Delete is fully available today; create /
update / merge are rolling out per entity.)

### Atomic multi-entity operations — Session
Real operations span several entities (importing a book touches an edition + its holdings + works +
persons; a merge touches several roots). A **Session** stages multiple planned `Impact`s and commits
them as **one transaction** — all succeed or none do:

```
session = open_session()
session.stage(editions.writes, editions.writes.plan_delete(edRef))
session.stage(holdings.writes, holdings.writes.plan_set_text_status(hRef, "ocr_good"))
combined = session.impact()      # one merged Impact across everything staged
session.commit()                 # atomic; or session.rollback()
```

### Concurrency & replay
- **Optimistic concurrency**: a write planned against one version of an entity is rejected with
  `StaleWrite` if the entity changed in between (via the `Ref` fingerprint / a per-row `rev`). Handle
  it by re-reading and re-planning.
- **Idempotency**: a write may carry an idempotency key so that a replayed request (e.g. an offline
  client flushing a queue) applies **once**. Re-sending the same keyed write returns the original
  result instead of duplicating it.

---

## 6. `Impact` — the anatomy of a change

`Impact` is the single object that describes a mutation, for both preview and execution. Render it to
show a user "what this will do"; pass it back to `apply` to do it.

```
Impact {
  op:            "create" | "update" | "delete" | "merge" | "relink" | "session"
  target:        Ref                      # the entity being changed
  changes:       { field: value }         # for create/update — the new values
  cascades:      [ Ref ]                   # owned parts that will be removed with the target
                                           #   (e.g. an edition's holdings)
  orphans:       [ Orphan ]                # roots left unanchored by this change (§ below)
  ref_purges:    [ RefPurge ]              # derived/secondary data removed alongside (caches, etc.)
  file_ops:      [ FileOp ]                # file effects (e.g. a holding's file moved to trash)
  link_repoints: [ LinkRepoint ]          # edges re-pointed instead of dropped (e.g. on merge)
  blocks:        [ Block ]                 # reasons this plan CANNOT be applied (empty ⇒ appliable)
}
```

### Orphans & `OrphanPolicy`
Some changes would leave another root **unanchored** — e.g. deleting the only Edition that contained
a Work leaves that Work with no edition. The plan reports each such case as an **`Orphan`**:

```
Orphan { ref: Ref, reason: string, decision: "gc" | "flag" | "refuse" }
```

The **client chooses the policy** when planning, and the server applies it:
- **`flag`** (default) — keep the orphan, surface it for human review.
- **`gc`** — delete the orphan as part of this change.
- **`refuse`** — refuse the whole change (it becomes a `block`, making the plan un-appliable).

So a client deleting an edition can decide, up front, whether stranded works should be kept-and-
flagged, garbage-collected, or treated as a hard stop.

---

## 7. Authorization

```
Principal { id, roles, scopes }          # who is acting
Action    { resource, verb, mode }       # what they want: e.g. ("edition","delete",WRITE)
mode ∈ { READ, WRITE }
Policy    : decides allow/deny per (Principal, Action)
```

- Every operation declares an `Action`; the server checks the supplied `Policy` before dispatch and
  raises `Denied` (HTTP 403) on refusal.
- **READ vs WRITE is the coarse axis**: `plan_*` and all reads need **READ**; `apply` (and `restore`)
  need **WRITE**. So previewing a delete and performing it are independently authorizable — a
  read-only client can show impact previews but never execute them.
- Typical role mapping: **viewer → READ only**, **editor → READ + WRITE**; an API-key scope can
  narrow WRITE to specific entities. A client authenticates and presents a `Principal`; the
  deployment supplies the concrete `Policy`.

---

## 8. Errors

A fixed, serializable taxonomy — every client handles failure the same way. Each maps to an HTTP
status for transport clients.

| Error | Meaning | HTTP |
|---|---|---|
| `NotFound` | the referenced entity does not exist (or is deleted) | 404 |
| `ValidationError` | input failed validation/normalization (bad code, malformed ISBN, missing field) | 400/422 |
| `Conflict` | the change conflicts with a uniqueness/state constraint | 409 |
| `IntegrityViolation` | the plan would break integrity (also raised when applying a blocked plan) | 409 |
| `StaleWrite` | the entity changed since you planned (fingerprint/`rev` mismatch) — re-read & re-plan | 409 |
| `Denied` | the policy refused this action for this principal | 403 |

---

## 9. Open vocabularies

Several fields draw from small **open code sets** (codes + human labels) rather than free text. Treat
them as open (new codes may appear); fetch the current set rather than hard-coding where possible.

- **`text_status`** (a holding's text layer): `none`, `image_only`, `ocr_poor`, `ocr_good`, `native`.
- **`holding_type`** (format): `pdf`, `epub`, `physical`.
- **`form`**: `electronic`, `physical`.
- **`work_type`**: `root`, `commentary`.
- **work↔work relation**: `commentary_on`, `comments_on`, `sub_comments_on`, `cites`, `summarizes`.

---

## 10. Worked example — deleting an Edition

```
# 1. Plan (READ). Decide what should happen to any stranded works.
plan = editions.plan_delete(edRef, orphan_policy = FLAG)

# 2. Inspect / render the Impact to the user:
#    plan.cascades      → "removes 2 holdings"
#    plan.orphans       → "Work #5 would have no other edition (flagged)"
#    plan.file_ops      → "2 files moved to trash"
#    plan.blocks        → if non-empty, show why it can't proceed and stop
if plan.blocks: show(plan.blocks); return

# 3. Confirm, then apply (WRITE). Atomic; returns the applied Impact as a receipt.
try:
    receipt = editions.apply(plan)
except StaleWrite:
    # the edition changed since step 1 — re-read and re-plan
    ...
except Denied:
    # principal lacks WRITE on edition.delete
    ...
```

After this, the edition no longer appears in reads (it is deleted-but-recoverable; its id is never
reused), its holdings are gone, and any flagged orphan work is surfaced for review.

---

## 11. Architecture at a glance (the layers)

> You don't need any of this to **use** the API — it's background, so you can see where your client
> sits and why the guarantees in §3 hold. Kept deliberately basic.

### The layer stack
```
┌───────────────────────────────────────────────────────────────────────┐
│  YOUR CLIENT      webui · CLI · HTTP/PWA · mobile                       │
│                   builds Query / plan, renders DTO & Impact            │
└──────────────▲───────────────────────────────────┬────────────────────┘
               │  serializable contracts            │  get / plan → apply
               │  (DTO, Ref, Impact, Error)          ▼
┌───────────────────────────────────────────────────────────────────────┐
│  ACCESS LAYER  (the entity API)                                        │
│    · authorization — Policy gate (READ vs WRITE)                       │
│    · plan → apply orchestration, Impact computation, orphan rules      │
│    · NO SQL, NO database knowledge                                     │
└───────────────────────────────┬───────────────────────────────────────┘
                                 │  calls an abstract STORE (a "port")
                                 ▼
┌───────────────────────────────────────────────────────────────────────┐
│  STORE PORT   (abstract interface: get / list / update / delete …)     │
│        ▲  one port, swappable implementations  ▲                       │
│   ┌────┴─────┐   ┌──────────────┐   ┌──────────────┐                    │
│   │  SQLite  │   │  in-memory   │   │ HTTP adapter │   ← pick one       │
│   │ adapter  │   │ fake (tests) │   │ (remote API) │                    │
│   └────┬─────┘   └──────────────┘   └──────────────┘                    │
└────────┼───────────────────────────────────────────────────────────────┘
         │  read-only / read-write connections
         ▼
   ┌──────────────┐
   │  Catalogue   │   today: one SQLite database file
   │   database   │
   └──────────────┘
```
**Why the split:** the access layer holds *what the API does* (rules, policy, previews); the store
holds *how data is stored*. Because the store is an interface (a **port**) with interchangeable
implementations (**adapters**), the catalogue can run against a local SQLite file today and a remote
service later **with no change to your client** — you only ever bind to the contracts in §1–§8.

### Terms, one line each
- **SQLite** — the storage engine in use today: the entire catalogue is a single file. An
  implementation detail; your client never talks to it directly.
- **Store (port)** — an abstract interface listing the data operations an entity needs. Defines
  *what* storage must do, not *how*.
- **Adapter** — a concrete implementation of a port. The SQLite adapter runs queries; tests use an
  in-memory fake; a remote deployment could expose an HTTP adapter. Same port, different backing.
- **Connection** — a live link to the database (read-only or read-write — below).
- **Transaction** — a group of changes that apply all-or-nothing (below).

### Connections — read-only vs read-write
```
   READ / preview  ──►  READ-ONLY  connection  ──►  DB     (writing here is impossible)
   WRITE / apply   ──►  READ-WRITE connection  ──►  DB
```
Reads run on a connection that **physically cannot write**, so a read or preview path can never
mutate data even with a bug. Writes use a separate read-write connection. This is the storage-level
mirror of the READ/WRITE authorization in §7 — the same boundary, enforced twice.

### Transactions — atomic `apply` & `Session`
```
  apply(plan)  =  one transaction          Session  =  many plans, one transaction
    ┌─ BEGIN ──────────────┐                 ┌─ BEGIN ─────────────────────┐
    │  re-check the target  │                 │  stage plan 1               │
    │  make the changes     │                 │  stage plan 2 …             │
    └─ COMMIT ─────────────┘                 │  (one combined Impact)      │
         │  on any error:                     └─ COMMIT ────────────────────┘
         └─► ROLLBACK  (nothing changes)           │  on any error: ROLLBACK
```
A single `apply` is one transaction; a `Session` wraps several staged changes in **one** transaction
so they land together or not at all (§5). On any error everything rolls back — the database is never
left half-changed. Side effects on files (e.g. moving a deleted holding's file to trash) happen only
*after* a successful commit, so a rollback leaves files untouched.

### How one write flows through the layers
```
 client │  plan_delete(edRef, FLAG)
        ▼
 access │  authorize READ → compute Impact (cascades / orphans / file_ops)
 layer  │      using store READS over the READ-ONLY connection
        ▼
        │  ── returns Impact ──►  client renders the blast radius, user confirms
        ▼
 client │  apply(plan)
        ▼
 access │  authorize WRITE → re-check target → orchestrate the changes
 layer  ▼
 store  │  SQLite adapter runs the statements on the READ-WRITE connection
        ▼
        │  COMMIT → then file effects → return the applied Impact (receipt) ──► client
```

---

### Summary of guarantees a client can rely on
1. **Stable references** — ids never re-point; a stale `Ref` fails loudly, never mis-resolves.
2. **Preview before commit** — every write has a side-effect-free `Impact` plan.
3. **Atomicity** — a single `apply`, or a `Session`, is all-or-nothing.
4. **Recoverable deletes** — deleting a root hides it but is reversible; ids are not recycled.
5. **One error taxonomy & one set of boundary shapes** across every client and transport.
