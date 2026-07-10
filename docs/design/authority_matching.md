# Authority & name matching — design decisions and roadmap

How the catalogue resolves works and people to external authorities (BDRC, 84000,
Wikidata, VIAF, …), how it disambiguates names, and what is built vs. planned.
Companion to `catalogue_plan.md` (master plan) and `whats_next.md` (punch list).

Status legend: **[built]** shipped + tested · **[partial]** wired but limited ·
**[planned]** designed, not yet built.

---

## 1. Two distinct lookups (do not conflate)

The catalogue answers two different questions with two different engines:

1. **Work → author/translator** — *who wrote/translated this text*. For a classical
   Indian/Tibetan work this is a scholarly-canonical fact belonging to the **work**,
   not to whatever a publisher printed on a translated edition's title page.
   Engine: `catalogue/work_authority.py` (`WorkAuthorityResolver`). **[built]**
2. **Edition → bibliographic fields** — verifying the *published book's* inferred
   title / authors / translators / publisher / year against bibliographic records,
   keyed by ISBN (else title+publisher). Engine: `catalogue/edition_verify.py`
   (`EditionVerifier`). It is a per-field **diff**, not an auto-merge. **[built]**

A translated classical text whose ISBN record lists the translator as "author" is
**expected signal** (it means the book is a translation), not an error to fix.

## 2. Shared architecture

- Pluggable **`Source`** ABC + `@register_source` registry + `default_sources()`,
  mirroring `verify.py`'s verifier-chain idiom. Add a source = one class; the engine
  never changes.
- Sources MUST degrade to `[]` on any network/parse failure — never raise.
- Per-source results cached in `resolver_cache` via
  `work_canonical_resolver.cached_rows(...)` (key = hash of namespace+source+query;
  `conn=None` disables; `write=False` = cache-only/offline; `cache_empty=False` =
  don't persist an empty result, so a transient miss isn't poisoned permanently).
- All name/title comparison is on the **fold-key** (`db.fold_key`: NFKD strip +
  digraph collapse) so diacritic/digraph variants collapse, plus `difflib` ratio
  for fuzzy title scoring.

## 3. Sources and ladders

**Work → author ladder** (in `work_authority.default_sources`):
`84000` (local TEI snapshot → Toh# + author + translators) → `Wikidata`
(P50 author, multilingual labels) . `BdrcWorkSource` contributes a catalog id.
**VIAF is intentionally absent here** — it is a person/name authority with no
work→author relation, so it cannot answer "who wrote this work". **[built]**

**Person → authority-id chain** (in `verify.default_verifiers`):
`BdrcVerifier` (BDRC/84000, curated for the canon) → `WikidataPersonVerifier`
(broad, cross-linked) → `ViafPersonVerifier` (modern/Western catcher). First match
wins; ids namespaced `bdr:P…` / `wikidata:Q…` / `viaf:…` (`PERSON_ID_PREFIXES`).
**[built]**

VIAF *contains* classical figures too, but BDRC/Wikidata are richer for them
(Wylie/native script/dates), so VIAF only earns its keep as the modern fallback.

## 4. Confidence policy (heeds M4: naive single-authority match is unreliable)

`WorkAuthorityResolver` verdicts:
- **verified** — ≥2 sources agree on an author (by fold-key), OR a single
  strong-title canon-catalog hit (Toh/number) that carries an author. Auto-applied.
- **candidate** — one source, decent title match. Queued for one-click review
  (`review_queue` item_type `work_authorship`).
- **none** — nothing matched the title well enough.

`apply_to_work` only acts on **verified** by default; it fills
`work.canonical_system/number` and links `work_contributor` via
`promote.get_or_create_person`. **[built]**

## 5. Three-state person status

`person.verification_status`: **provisional** (typed/extracted, unchecked) →
**verified** (matched an authority, `external_id` set) | **confirmed_local**
(a human confirmed NO authority record exists — e.g. a modern self-published
author — so THIS row is canonical). `verify.confirm_local(db, pid)` sets the third
state; the verify walk processes only `provisional`. This lets authority-absent
moderns leave the worklist, which `external_id IS NULL` alone could never express.
**[built]**

## 6. Honorific / title handling (matching only, never storage)

`catalogue/honorifics.py`. Titles are matching noise: "Geshe Lhundub Sopa" and
"Lhundub Sopa" are one person. `strip_honorifics(name)` removes them for
**match/search only** — stored `primary_name`/aliases keep the source spelling.
Lists in `vocab.json` under `_honorific` / `_office` (underscore = config, skipped
by `db.load_vocab`; built-in fallback in the module). **[built]**

**Critical exceptions — titles that ARE the identity, never stripped:**
- **Office + ordinal**: "14th Dalai Lama" ≠ "7th Dalai Lama"; "16th Karmapa" ≠
  "17th". Office words (dalai/panchen/karmapa/trizin…) are a separate `_office`
  list and never stripped; when paired with an ordinal (14th / fourteenth / XIV)
  `strip_honorifics` returns the name unchanged. (`has_ordinal` uses plain
  NFKD-lower, NOT fold_key — fold_key's digraph collapse mangles ordinals.)
- **Lineage/role titles like "Lotsawa"** (translator) are NOT in the strip list:
  "Ra Lotsawa" stripped to bare "Ra" (a clan name) would be *worse*. Treated like
  an office — identity-bearing.

`verify._tokens` subtracts honorific tokens from the overlap check (fixed a real
false-positive: "Lama Zopa" vs "Lama Yeshe" shared the token "lama").

## 7. BDRC type filtering

BDRC's `BLMP` template matches a name across ALL resource types and returns a
name-ranked list, so the top hit is often the wrong type (a *work* ranks above the
*person* for "Nagarjuna"). `work_canonical_resolver._query_live` filters the result
list to the caller's entity type (`entity_type(bid) == want`) BEFORE taking the top
hit. LiveResolver cache `version` bumped 2→3 to invalidate poisoned entries.
**[built]**

**SPARQL fallback [planned]:** client-side filtering is conclusive *as long as a
right-typed hit appears anywhere in BLMP's 20-row window*. If a person genuinely in
BDRC never appears in that window (ranking/truncation failure), escalate to the BDRC
**SPARQL endpoint** with a server-side type constraint (`?s a bdo:Person`). Build
this the first time a known-present person is missed by the BLMP filter — not before.

## 8. The duplicate-name / homonym problem

Name alone is **unsolvable** (standard entity-resolution fact). Example: multiple
**"Ra Lotsawa"** — *Ra* is a clan, *Lotsawa* a translator title, and the Ra lineage
produced several translators (Dorje Drak, Chörab, Yeshé Sengé…). A bare colophon
"Ra Lotsawa" cannot identify which.

How everyone disambiguates — by adding signals, never by name alone:
- **Stable IDs** (VIAF / BDRC P-numbers / Wikidata QIDs) — resolve once, then
  reference the ID forever.
- **Dates** (b./d./fl.) — secondary key; we have `person.dates`.
- **Context = the work the person is attached to** — the strongest signal we have.
  "Ra Lotsawa, translator of Vajrabhairava (Toh 468)" is unambiguous.
- **Blocking + scoring** (Fellegi–Sunter record linkage) then **human adjudication**
  for the residue (our review queue).

Current mitigation: `verify_person` matches via a **ladder** — original name first
(a title/epithet may distinguish), then `strip_honorifics` fallback for recall.
**[built]** This handles title-distinguished homonyms; true same-after-strip
homonyms need the work signal (next section).

## 9. Person + work joint resolution **[planned — next build]**

The decisive fix for Ra-Lotsawa-class ambiguity. **A new pass over
`work_contributor`** (not folded into the person walk):

For each (person, work) edge:
1. Resolve the **work** → its canonical id (e.g. Toh 468) and that authority
   record's named author/translator person id.
2. If the work names a person id whose labels **strongly** match the local person
   name → bind the local person to that specific id. *The work picks the person.*
3. **Conflict policy:** strong work match + **weak** name match → do NOT auto-bind;
   emit a `review_queue` item with both candidates for one-click human decision
   (heeds M4). Auto-bind only on a strong name match.
4. No work-derived person signal → fall back to today's name-only person chain.

Decisions locked: **separate pass over `work_contributor`**; **weak name match →
queue for review**.

**Prerequisite discovered:** the work sources must surface a **per-contributor
external id**, not just a name. Today `WorkAuthorityRecord.authors/translators` are
plain name strings — the Wikidata source already fetches each author's Q-id (P50)
but discards it; 84000 has names only. To bind a person to a specific id via the
work, extend the record to carry `[(name, external_id?)]` per contributor (Wikidata
fills the id; 84000 leaves it None and relies on a downstream name→id person lookup).
This is the first build step of §9.

## 10. Roadmap (planned, unbuilt)

- **Person+work joint resolution pass** (§9) — next.
- **BDRC SPARQL fallback** (§7) — when BLMP filtering proves non-conclusive.
- **Pandit Project source** — authority for classical *Sanskrit* authors/works
  (panditproject.org); slots in as a `WorkAuthoritySource`.
- **Person type-ahead `/persons/search`** — combobox over `person` + `person_alias`
  by fold-key, plus an explicit "add new" path; replaces the silent
  `LIMIT 100 ORDER BY primary_name` dropdown on the edition card.
- **Merge tool** — reconcile duplicate persons (repoint `work_contributor` /
  `edition_work`, move aliases, delete loser). Needed because cross-script forks
  will happen; also the right home for honorific-aware dedup (NOT pushed into
  `promote.get_or_create_person`, which stays honorific-sensitive on the stored-name
  path).
- **Two integration hooks** (both deferred): (1) promotion-time auto-apply of
  verified work-authors in `promote.promote_proposal`; (2) `EditionVerifier` diff
  panel in `web.py /edition/<id>/card`. The work-authorship CLI walk is the manual
  batch form of hook (1).

## 11. How to run the checks (existing catalogue; back up the DB first)

- People vs BDRC→Wikidata→VIAF:
  `python3 -m catalogue.verify catalogue-db/catalogue.db --kind person`
  (`--offline` cache-only; `--limit N` to trial).
- Works → catalog id vs BDRC/84000:
  `python3 -m catalogue.verify catalogue-db/catalogue.db --kind work`.
- Works → author/translator vs 84000+Wikidata:
  `python3 -m catalogue.work_authority catalogue-db/catalogue.db`
  (`--offline` / `--limit`).

Both CLIs use `init_db()` (idempotent migrations) so an older live DB gets the
`verification_status` column automatically.
