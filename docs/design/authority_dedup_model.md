# Authority-driven person dedup — design model

*Captured 2026-06-05. How to automatically collapse duplicate `person` records that
refer to the same real person, across multiple external authorities (Wikidata, BDRC,
VIAF, DILA, …), with deep cross-source traversal. Design only — not yet built.*

---

## 1. The problem

Binding does **not** dedupe. When a provisional record is bound to an authority id,
nothing checks whether another record already carries that id (or an equivalent one).
So spelling/title variants each get bound separately and accumulate as duplicates.

**Live example — Tsongkhapa = `wikidata:Q323439`** has **6 separate records** all bound
to the same Wikidata person:
`#3 Tsongkhapa · #92 Je Tsongkhapa · #184 Lama Jey Tsongkhapa · #321 Lama Tsong Khapa ·
#491 Tsongkhapa Lobzang Drakpa` (plus ~5 more still-provisional variants). Corpus-wide,
**62 person rows share an external_id with at least one other record** (Q323439 ×6,
Q6538581 ×4, Q3104465 ×4, several ×3, many ×2).

Each real person should be **one** record. Records sharing an authority id are duplicates
to be merged into one canonical record (the others' names kept as aliases — the merge
"keep name as alias" checkbox already does this).

> Related: `purge_suspect_matches` currently treats a shared `external_id` as a
> *false-positive* signature (a scar from the old BDRC fuzzy-match disaster) and clears
> both. That logic predates trustworthy (Wikidata) binding; with manual/precise binding,
> a shared id means **same person → merge**, not **bad match → purge**.

---

## 2. The model: identity = a SET of authority keys

Stop thinking "a person has *an* id." Think "a person owns a **set** of authority keys."
The schema already stores this:

- `person.external_id` — the hub id (usually `wikidata:Q…`)
- `person_external_id(scheme, value)` — harvested cross-links (`bdrc:P…`, `viaf:…`, `dila:…`)

So a record carries a key-set, e.g. `{wikidata:Q323439, bdrc:P64, viaf:…}`.

> **Two records are the same person iff their key-sets are CONNECTED** — directly (a
> shared key) or transitively (one record's key cross-links to the other's).

This reframes dedup as a **connected-components / union-find problem over authority keys**
— the clean way to express the deep, cross-source traversal.

---

## 3. Deep traversal = transitive closure over cross-links

The hard case (user's example): **B is bound to `Y`, and `Y ↔ Z` are linked, and `A`
owns `Z`.** B and A are the same person even though they were bound to *different* ids.

Resolution:
1. **Expand** the id: `expand(Y)` = `Y` ∪ everything `Y` cross-links to = `{Y, Z, …}`.
   (Wikidata is the hub and usually returns all cross-links in one hop.)
2. **Look up** each expanded key in the identity index `key → record`.
3. `Z` hits **A** → union B and A.

Do this as a **fixed-point BFS**: keep expanding ids until no new ids appear (handles
chains and cycles). One hop through the Wikidata hub covers most cases, but the closure
is the correct general model.

---

## 4. Source-agnostic (dynamically search all sources)

Do **not** hardcode Wikidata/BDRC. The only capability each source must expose is:

> *given an id in my scheme, what other-scheme ids does it cross-link to?*

That is already the **harvest** step (`_store_external_ids` / `_harvest_extra` / the
`Verifier` chain). Therefore:

- `expand(id)` simply calls the registered harvesters and unions their results.
- **Adding a source = registering a Verifier/harvester.** The dedup picks it up
  automatically; the equivalence graph just gains new edges. The merge logic never changes.

**Key decision:** the traversal is generic; only the per-source cross-link lookup is
pluggable.

---

## 5. When: on-bind AND batch — different jobs, not alternatives

### On-bind (synchronous, LOCAL, cheap) — *prevention*
Binding B to Y already harvests Y's cross-links. Add one step: take B's expanded key-set,
query the local index (`person_external_id` + `person.external_id`) for any match; on a
hit to an existing record →
- **auto-merge** when high-confidence (exact id, or a verified cross-link), or
- **suggest** ("B and A both resolve to Z — merge?") when lower-confidence.

Stops new duplicates at the source, in context.
**Constraint:** a *single local lookup* over already-harvested keys — **no multi-hop
network traversal on the bind path** (too slow). On-bind handles the *symmetric,
immediately-visible* case.

### Batch (periodic / on-demand / sandbox-first) — *the authoritative cleanup*
A global pass: build union-find over **all** records' key-sets with transitive closure,
group into components, merge each component into one canonical record. The **deep**
traversal lives here. It catches what on-bind cannot:

- **Pre-existing duplicates** (the 62 — bound before any auto-merge existed).
- **Asymmetric / late-discovered links.** Cross-links are often one-directional: Wikidata
  `Z` lists BDRC `Y`, but BDRC `Y` doesn't list `Z`. Binding B to `Y` whose source doesn't
  reveal `Z` won't find A[Z] on-bind — but the batch, expanding from *both* sides and
  re-harvesting, will. **This is why on-bind alone is insufficient.**
- **After adding a new source** — new cross-links create new equivalences among
  already-bound records; only a re-run finds them.

### The decision rule
- **On bind** → cheap local check; auto-merge the obvious same-key case, suggest the rest.
  *Write-time guard — prevents accumulation.*
- **Batch** → run it (1) once now to collapse the existing 62; (2) whenever a new
  authority source is added; (3) on a schedule / on demand. *Reconciliation/GC — deep,
  global, transitive, reviewable.*

Same pattern as a cache filled eagerly (on-bind) but also swept periodically (batch).

---

## 6. Correctness / risks to get right

- **Relax the merge conflict-guard for EQUIVALENT ids.** Today `plan_merge` refuses if two
  records have *different* `external_id` strings. With cross-links, A[Z] and B[Y] are the
  *same* person but different strings — the guard must block only when the ids are in
  **different equivalence components**, not merely different strings. (Prerequisite for
  both on-bind and batch; this is "case #3" from earlier discussions.)
- **Canonical selection** for a component: keep the record with the most work/edition
  edges (or the verified one / lowest id); carry the **union** of all authority ids +
  aliases, and (via the checkbox) the merged-away names as aliases.
- **Bad cross-links / conflated authorities.** Authorities occasionally link wrongly, or
  one id covers two real people. Auto-merge only on **strong** keys (exact id, or a
  trusted-source cross-link); route weak signals to *suggestions*; keep a **merge log**
  for undo; run the batch **sandbox-first** so it's reviewed before touching live.
- **Provisional name-variants** (e.g. the ~5 unbound Tsongkhapas) have **no keys**, so
  authority-dedup can't see them. They need binding first (then they join the component)
  or a separate, **lower-confidence fuzzy-name** pass. Keep the two mechanisms distinct.

---

## 7. Components to build (when approved)

1. **Identity / equivalence module** — `expand(id)` (source-agnostic transitive closure
   over the harvest) + union-find over records' key-sets → components.
2. **Batch dedup** on top of it — sandbox-first, reviewable, with a merge log. *Run it on
   the existing 62 first.* (Reuses `apply_merge(..., keep_name_alias=True)`.)
3. **On-bind hook** — the local check that auto-merges/suggests, plus the conflict-guard
   relaxation.
4. **`/integrity` warning** — "N persons share / transitively share an authority identity"
   so duplicates self-surface instead of being spotted by eye.

**Recommended order:** batch + equivalence module **first** (cleans the 62, fully
reviewable in a sandbox), on-bind auto-merge **second**. This de-risks it — you see the
global result before anything merges automatically at bind time.

---

## 8. Existing code this builds on

- `person.external_id` + `person_external_id` table — the key-set storage.
- `_harvest_extra` / `verify._store_external_ids` / the `Verifier` chain — the per-source
  cross-link harvest that `expand()` reuses.
- `contributor_edit.apply_merge(pid, into_id, *, keep_name_alias=…)` — the merge primitive
  (re-points all edges, keeps names as aliases). Guard: `plan_merge` (self-merge +
  conflicting-authority — to be relaxed per §6).
- `authority_pass.py` — **prototype** that already clusters by shared authority id +
  harvested cross-links and merges each cluster into its anchor (a first cut of §7.2,
  not wired into the app).
- `catalogue/integrity.py` — where the §7.4 warning lands.
- `catalogue/sandbox.py` — the fork/promote/discard the batch runs inside.

---

## 9. Performance: cost of auto-merge on bind

**Near-zero, if implemented as §5 prescribes** — because binding already pays the only
expensive step.

### The key fact: bind already pays the costly part
Binding today already calls `_harvest_extra(ext_id)`, which resolves the id's cross-links
(Wikidata → BDRC / VIAF / DILA). **That network/cache lookup is the costly step and it
already happens on every bind** (~100 ms–2 s, network-dominated; cached repeats are fast).
Auto-merge **reuses that harvest's output** — it does not add a second network call.

### What auto-merge actually adds
1. **A local index lookup** — take B's already-harvested key-set and query
   `person_external_id` + `person.external_id` for a match. With an index on
   `person_external_id(value)` that's microseconds; even without one it's a scan of a few
   hundred small rows. **Sub-millisecond.**
2. **A local merge, ONLY when a duplicate is found** — `apply_merge` re-points a handful
   of `work_author` / `edition_translator` / `edition_work` rows, moves aliases, deletes
   the dup. All local writes on small tables. **A few milliseconds**, scaling with the
   dup's edge count (not the corpus).

So:
- **No duplicate (common case):** adds **< 1 ms** — a rounding error next to the harvest.
- **Duplicate found:** adds **a few ms** of local writes, still no network.

Auto-merge adds **< 1 %** to bind latency; the network harvest dwarfs it.

### The one thing that WOULD hurt — and why the design avoids it
The trap is multi-hop **network** traversal on the bind path: "found Z → fetch Z's
cross-links → fetch those…". Each hop is a fresh round-trip (hundreds of ms each), so a
deep synchronous closure could add **seconds** per bind. That is exactly why §5 restricts
on-bind to **a single local lookup over the keys already harvested for the id being
bound** — no extra network, no multi-hop. The deep / transitive / asymmetric-link closure
is pushed to the **batch**, where latency doesn't matter and re-harvesting is free.

### To keep it cheap in practice
- Index `person_external_id(value)` (and confirm `person.external_id` is indexed) so the
  lookup stays O(log n) as the corpus grows.
- Cap on-bind to the one-hop, already-harvested key-set; if confirming a match needs
  deeper expansion, **suggest** rather than merge synchronously and let the batch finish.
- Merge cost scales with the dup's edges, not the corpus → it stays flat as the catalogue
  grows.

**Bottom line:** auto-merge on bind is effectively free — binding already does the one
expensive thing (the cross-link harvest); everything auto-merge adds is local. The
performance risk lives entirely in transitive *network* traversal, which is why that part
belongs in the batch, not on the bind path.
