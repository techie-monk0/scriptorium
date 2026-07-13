# catalogue-app — native iOS reader/library client (implementation & test plan)

**`catalogue-app`** (the native sibling of **`catalogue-webui`** and **`catalogue-pwa`**) is a native
SwiftUI app for iPhone **and** iPad that reproduces the **PWA's look and feel** and, unlike the PWA,
reads books **in-app** — via the embedded **`octavo`** reading SDK (PDFKit + native EPUB) plus its
**`postilla`** handwriting/annotation extension — instead of handing off to a third-party reader
over WebDAV.

> **Naming.** `catalogue-app` is the whole native client = the library-catalogue frontend (its own
> browse/search/detail screens, Tiers 1–3) **plus** the in-app reader. The reader — and *only* the
> reader — embeds `octavo` (engine) + `postilla` (annotations); those two are catalogue-free SDKs and
> know nothing about the catalogue. So: **catalogue-app hosts octavo + postilla; the rest of the app
> is its own.**

This plan is grounded in the **shared frontend contract** (`private/frontend/frontend_contract.md`).
The whole point of that contract is that a native client **reuses Tier 1 + the reader
sync-of-record, reimplements Tier 2 in Swift, builds Tier 3 in SwiftUI, and *hosts* the
`octavo`/`postilla` SDK for the reader** — nothing below the renderer is re-invented, and the
renderer itself is now the shared octavo engine rather than bespoke per-platform code.

```
 Tier 1  /api/v1/* JSON over HTTP        ← REUSE AS-IS (server already ships it)
 Tier 2  presenter + view-models (pure)  ← REIMPLEMENT in Swift (library-core.js is the spec)
 Tier 3  UI renderer                      ← NEW SwiftUI (library-ui-dom.js is the reference look)
 Reader  byte handle + position + sync    ← HOST octavo-swift + postilla; supply the ports
                                            (Source/ReadingStore/AnnotationStore/Capabilities)
```

**Build/run caveat (applies to every task below):** Swift is scaffolded in this repo under
`ios/`, but it is **compiled, run, and tested in Xcode on a Mac with an Apple Developer account**.
The agent environment cannot build or run it. All test tasks below run via `xcodebuild test` /
Xcode locally, not in CI here (until a macOS runner exists).

---

## 1. Project layout

```
ios/Catalogue/
  Catalogue.xcodeproj
  Sources/
    Core/            # Tier 2 port — pure Swift, no SwiftUI/UIKit imports
      Models.swift          # Codable structs for every /api/v1 shape + replica row
      LibraryCore.swift     # searchVM/browseVM/contentVM/detailVM/settingsVM/navVM
      Refs.swift            # Ref enum + refFromUrl parse
      Protocols.swift       # PROTOCOLS, protocolVisible (mirror domain/protocols.py)
      SearchNormalize.swift # NFKD strip-marks lowercase (match domain/search.py)
    Design/          # palette.json port + shared visual primitives
      Palette.swift         # generated from theme/palette.json (see §3)
      Tokens.swift          # semantic color accessors (bg/fg/surface/link/…)
      Typography.swift, Spacing.swift
    Data/            # platform adapter (the Tier 2 adapter protocol)
      CatalogueAPI.swift    # URLSession client, base URL = Mac LAN address
      ReplicaStore.swift    # cached replica (GRDB/SQLite or file) + ETag/304
      ContentIndex.swift    # offline FTS (SQLite FTS5) behind the facade seam
      FileCache.swift       # on-demand book-byte cache (mirrors FileStore)
      ReadingStore.swift    # per-publication reading state — stores an octavo Locator (octavo §3)
      ReaderSync.swift      # GET/POST /sync/reader LWW merge
    UI/              # Tier 3 — SwiftUI screens (the new work)
      Shell.swift, Nav.swift                  # floating menu / tab bar
      HomeView.swift, ShelfView.swift         # Netflix-style shelves + Dock magnify
      SearchView.swift, BrowseView.swift, ContentView.swift
      DetailView.swift                        # book detail (cover + dl + Read controls)
      SubjectView.swift, SettingsView.swift
    Reader/          # thin HOST of octavo-swift + postilla (NOT a from-scratch reader)
      ReaderView.swift        # SwiftUI container that mounts an octavo Navigator
      ReaderPorts.swift       # wires Source/ReadingStore/AnnotationStore/Capabilities → Data/*
      # the engine (octavo-swift: PdfKitNavigator/EpubWebNavigator) and the ink/marks layer
      # (postilla: PencilKit input + perfect-freehand-Swift) arrive as SwiftPM deps, not hand-rolled
  Tests/
    CoreTests/        # unit (XCTest, pure)
    DataTests/        # unit + contract decode
    UITests/          # XCUITest system flows
    PerfTests/        # XCTest measure / Instruments
  Fixtures/           # captured real /api/v1 JSON + sample pdf/epub
```

---

## 2. Reuse vs build (scope contract)

| Layer | Source of truth | iOS action |
|---|---|---|
| Tier 1 data | `routes/api.py` (`/api/v1/*`), `/sync/reader`, `/holding/<id>/file`, `/holding/<id>/annotated.pdf` | **Reuse** — only write Codable models |
| Tier 2 presenter | `static/js/library-core.js` | **Reimplement** 1:1 in Swift (small: query handling, replica grouping, offline/live selection, normalization) |
| Adapter protocol | contract §"Adapter protocol" | Implement `CatalogueAPI` (data/nav/prefs/openBook/isOffline) |
| Tier 3 renderer | `static/js/library-ui-dom.js` + `shelf.css`/`pwa.css` | **New SwiftUI** matching the look |
| Design tokens | `theme/palette.json` (master) | **Generate** Swift color spec from it (§3) |
| Nav icons | SF Symbol names already in `SF_ICONS` | `Image(systemName:)` — direct |
| Reader | **`octavo`** SDK + **`postilla`** (Locator, ports, DecorationHost) | **Host** octavo-swift/postilla; supply `Source`/`ReadingStore`/`AnnotationStore`/`Capabilities` ports |
| Offline content FTS | `content-index` bundle + `match_fts` semantics | **New** SQLite FTS5 behind same facade |

---

## 3. Design-system port (the "same look and feel")

The PWA's look is fully specified by data, so parity is mechanical, not eyeballed:

1. **Palette** — `theme/palette.json` is the language-neutral master with `light`/`dark` themes and
   20 tokens (`bg fg muted border surface surface-2 link brand nav-hover nav-active-bg
   nav-active-fg card-border subtle-fg btn-* accent ok warn`). **Extend `theme/gen.py`** to emit a
   `Palette.swift` (a `[Theme: [Token: Color]]`) alongside `tokens.css`, so the existing drift test
   keeps web + iOS in lockstep from one source. Theme follows OS unless `theme` pref pins
   light/dark; apply before first view (`.preferredColorScheme`).
2. **Typography** — `-apple-system` is literally the iOS system font; map PWA 16px/1.45 to
   `.body`, tabular numerals (`.monospacedDigit()`) for counts/progress.
3. **Shelves / cover grid** — reproduce `shelf.css`: horizontal scroll rails, `spine` (≈46px
   spines w/ deterministic hash-jitter) vs `cover` (140px posters) modes from the `shelfArt` pref,
   Dock-style magnification about bottom-center, series "set-tile" → expand drawer (collage/cover/
   fan). SwiftUI `ScrollView(.horizontal)` + `matchedGeometryEffect`/`scaleEffect` driven by scroll
   offset.
4. **Navigation** — the contract says a native client reimplements Tier 3 against the same
   `navVM`, "choosing the form natural to its platform (e.g. a tab bar)." Decision needed (see
   open questions): a `TabView` (most iOS-native) vs. porting the floating-arc FAB for 1:1 PWA
   feel. Honor `protocolVisible` so sections gate identically (`default`/`local`/`desktop`).
5. **Adaptive iPhone/iPad** — `NavigationSplitView` on iPad (sidebar/columns, Apple-Music-like
   master-detail), stacked `NavigationStack` on iPhone; size classes drive grid columns.

---

## 4. Screens (parity with the PWA's five features + Home)

Home (Recently opened + Recently added + subject shelves), Search, Browse, Content (full-text),
Book detail (cover + Translators/Subjects/ISBN/Published/Works + per-holding Read), Subject page,
Settings (theme, shelfArt, offline-content download). Each is a Tier-3 view bound to the matching
Tier-2 view-model. No Favorites yet (not in current code; it's only in the older `frontend_plan.md`
— defer unless requested).

---

## 5. Reader (the native advantage) — host `octavo-swift` + `postilla`

The reader is **not** built from scratch against a bespoke "reader contract"; it is a thin SwiftUI
**host of the `octavo` reading SDK** (`../../../octavo-postilla/octavo/docs/octavo.md`) and its annotation extension
**`postilla`** (`../../../octavo-postilla/postilla/docs/postilla.md`). The catalogue is octavo's **first consumer**: the
iOS app supplies the SDK's *ports*, octavo supplies the engine, and dogfooding it as an outside
integrator is exactly how octavo proves it is host-free. This **supersedes** the earlier
"implement against the reader contract, not a shared renderer" framing — the renderer *is* shared
now, and that is the whole point of octavo.

```
 octavo-swift   PdfKitNavigator (PDFKit) · EpubWebNavigator (epub.js-in-WKWebView)   ← the engine
 postilla       PencilKit-input ink + highlight/underline/strike/note over DecorationHost
 iOS app        a SwiftUI container + the PORTS (this is the "layer above the SDK")
```

**Dispatch & open** stay the integrator's job (octavo dispatches `pdf` vs `epub` internally once
opened): from a `holding` (`kind ∈ {pdf,epub}`), the app hands octavo a **`Source`** port and calls
`Octavo.open`.

**Ports the iOS app provides (the "layer above the SDK"):**
- **`Source`** — `FileSource` over the on-demand `FileCache` byte cache (zero-transfer fast path),
  else `HttpRangeSource` over `GET /holding/<id>/file` (HTTP range → stream large PDFs). The engine
  never learns the storage backend — the kDrive-doesn't-leak seam is **octavo's**, not ours to
  re-solve.
- **`ReadingStore`** — the app's `ReadingStore.swift`/`ReaderSync.swift`, now persisting an octavo
  **`Locator`** (below) instead of a bespoke `{kind,page}` blob. `recent(n)` (Locators by
  `opened_at`) feeds the Home shelf.
- **`AnnotationStore`** (postilla port) — `ReaderSync.swift` over `GET/POST /sync/reader`: LWW
  upsert keyed by client **UUID**, tombstones honored, scoped per publication. The **structured
  store is the source of truth**, not the file bytes — so marks made on web appear here and vice
  versa, and an LLM layer can read them as data.
- **`Capabilities`** — from `GET /api/v1/health` (`can_edit`/`can_download` gate the postilla
  tools; a read-only server hides them).
- **`Recognizer`** (optional postilla port) — an Apple Vision/PencilKit on-device adapter if/when
  recognition is wired; **advisory only** (raw ink stays the source of truth).

**Position is a `Locator`.** octavo's `Locator`
`{ publicationId, format, locations:{ page? | cfi?, progression, position? }, text? }` is the single
position model. PDF uses `page`, EPUB uses `cfi`, `progression` is the universal fallback, and
`text` (before/highlight/after) lets a bookmark survive re-pagination. Restore on open, save on
`onLocationChanged`.

**Handwriting is postilla's, under its canonical-ink rule.** Capture via PencilKit
(`PKStrokePoint(location, force)` → `[x,y,pressure]`) as an **input layer only**; store the raw
points; render with the **perfect-freehand-Swift** port — **not** PencilKit's native ink — so a
stroke is pixel-identical on web, on device, and in the flattened PDF. Anchoring: PDF rect[] /
ink points are page-relative `0..1`; EPUB marks anchor by `cfi_range` (ink best-effort by
block-CFI). Ink drawn natively round-trips back to web, which re-renders the same points with
perfect-freehand.

**Export still available** via postilla-export: `GET /holding/<id>/annotated.pdf` (flattened
**copy**, original untouched) for sharing out — native keeps the portability the PWA path had.

---

## 6. Offline

- **Replica** — `GET /api/v1/replica` (ETag/304) → local SQLite/file; Search & Browse served from
  it (mirror the PWA), Content hits `/content` live unless the offline index is enabled.
- **Content index** — `GET /api/v1/content-index` (gzipped FTS5 SQLite) behind a `ContentIndex`
  facade `{available,status,load,enable,disable,search}`; native runs the **same `match_fts`
  query semantics** (NFKD normalize, whole-query phrase, `ORDER BY bm25`) so offline == online.
- **File cache** — on-demand per-holding byte cache (mirrors `FileStore`).

---

## 7. TEST PLAN

Three suites. Targets: **CoreTests/DataTests** = unit (fast, no network, no UI),
**UITests** = system/integration (XCUITest against a live seeded server), **PerfTests** =
performance (XCTest `measure` + Instruments). All run via `xcodebuild test` on a Mac.

### 7.1 Unit tests (XCTest — pure, deterministic)

Goal: the **reused/ported logic is provably equivalent to the web**, independent of UI and server.

- **U1 — Codable contract decode.** For every `/api/v1` shape + replica row + `/sync/reader`
  payload, decode a captured real-server fixture (`Fixtures/`) into Swift models and assert all
  fields populate; assert unknown-field tolerance and missing-optional handling. (Guards drift
  between server JSON and Swift structs.)
- **U2 — Tier-2 view-model parity.** `searchVM/browseVM/contentVM/detailVM/settingsVM/navVM`
  given fixture adapter data produce the expected structs: Search→cards, Browse→groups w/ hits
  `{type,label,sublabel,ref}`, Detail fields, offline/error encoded as **fields not exceptions**.
  Where feasible, snapshot the JS `library-core.js` output for the same input and assert the Swift
  output equals it (golden files generated by a tiny Node script committed under `Fixtures/`).
- **U3 — Ref/URL mapping.** `refFromUrl` parses web URLs → `{kind:edition|work|person|subject|url}`;
  `nav.hrefFor(ref)` maps to native routes; work/person → null where the PWA returns null.
- **U4 — Protocol visibility.** `protocolVisible('default'|'local'|'desktop', ctx)` matches
  `domain/protocols.py` truth table; a section declaring nothing stays visible; Review/Scan gate
  on `desktop`, mount-roots on `local`.
- **U5 — Search normalization parity.** `SearchNormalize` == `domain/search.py`: NFKD, strip
  combining marks, lowercase, collapse whitespace. Diacritic cases (`bodhicaryavatara` →
  `Bodhicaryāvatāra`), mixed scripts; table-driven against a fixture of (input, expected) pairs
  exported from the Python normalizer.
- **U6 — Replica grouping / offline selection.** Browse groups derived from the cached replica
  match the live `/find` grouping; offline-vs-live selection logic picks the cached path when
  `isOffline()`.
- **U7 — Palette port.** Every token in `palette.json` resolves to the exact Swift `Color`
  (hex round-trip) for both themes; `auto` theme removes the pref (follows OS). Drive from the
  same JSON the generator reads so this fails if someone hand-edits `Palette.swift`.
- **U8 — ReadingStore.** `recordOpen` stamps `opened_at`; `setLocation` persists PDF page / EPUB
  cfi; `recent(n)` orders by `opened_at` desc; round-trip encode/decode of the exact contract
  shape; corrupt/missing record tolerated.
- **U9 — Reader sync LWW merge.** Apply two divergent op streams (same UUID, different
  `updated_at`) → last-write-wins; a tombstone (`deleted_at`) removes a mark; `since=<rev>`
  returns only newer rows incl. tombstones; idempotent re-apply is a no-op. (This is the
  correctness core of cross-device annotations.)
- **U10 — Annotation coord/ink codec.** PDF rect `[[x,y,w,h],…]` and ink
  `{strokes:[{points:[[x,y,pressure],…],width,color,mode}]}` encode/decode losslessly; coords stay
  0..1; an ink stroke drawn in PencilKit serializes to the same shape the web reads.
- **U11 — Offline FTS parity.** The native `match_fts` query over a small fixture FTS5 DB returns
  the **same ordered eids** as the server for a set of queries (whole-query phrase, bm25 order).

### 7.2 System / integration tests (XCUITest + live seeded server)

Goal: real screens, real HTTP, real reader, end to end. Spin up the Flask app with the existing
`tests/system` `app_env` + `seed` fixtures (person→work→edition→holding with a real file) on a
loopback port; point the app's base URL at it; drive the UI.

- **S1 — Cold launch & replica load.** Launch → replica fetches (or 304s) → Home shows Recently
  added; assert shelves render and a cover image loads.
- **S2 — Navigation matrix.** Visit every section (Home/Search/Browse/Content/Detail/Subject/
  Settings); assert the nav highlights the active section; iPad split-view vs iPhone stack both
  reachable (run the suite on an iPhone and an iPad simulator).
- **S3 — Search → detail → read.** Type a diacritic-folded query → tap a result → Book detail
  renders cover + metadata + per-holding Read control → open reader → first page renders.
- **S4 — Content (full-text) search.** Query hits `/api/v1/content`; snippets render with
  `[match]` highlight markers and `…` elision.
- **S5 — Reader round-trip (the headline test).** Open a PDF → add a highlight + a PencilKit ink
  stroke → assert `POST /sync/reader` succeeded → fetch `GET /sync/reader?since=` from a second
  client (or re-launch) → the marks reappear at the same page-relative coords. Then
  `GET /holding/<id>/annotated.pdf` and assert the bytes are a valid PDF containing the marks
  (server-side PyMuPDF path already tested in `tests/system/`; here assert the app triggers it).
- **S6 — Position restore.** Read to page N, background the app, relaunch → reopens at page N;
  Home "Recently opened" lists the edition first.
- **S7 — Offline mode.** Toggle airplane mode (network condition) after replica+file cached →
  Search/Browse/Detail still work from the replica; opening a cached book works; an uncached book
  surfaces a graceful offline state (field, not crash); enable offline content index → Content
  search works with no network and equals the online result for the same query.
- **S8 — EPUB path.** Open an EPUB holding → renders, position saves as `{kind:'epub', cfi}`,
  a highlight anchors by `cfi_range` and round-trips.
- **S9 — Capability/health.** `GET /api/v1/health` capability flags (`can_edit`, `can_download`)
  correctly gate annotation tools (read-only server → tools hidden).
- **S10 — Theme & shelfArt.** Switch theme light/dark/auto and shelfArt spine/cover in Settings →
  UI updates live and the pref persists across relaunch.

### 7.3 Performance tests (XCTest `measure` + Instruments, on device)

Goal: it must feel like the PWA's smooth Apple-Music shelves, and the in-app reader must beat the
handoff path on time-to-first-page. Run on a **real device** (not just simulator) for FPS/memory,
with explicit budgets that fail the test if exceeded.

- **P1 — Shelf scroll FPS.** Scroll a home full of cover rails; assert sustained 60 fps (no
  dropped-frame spikes) via `XCTOSSignpostMetric`/Instruments Animation Hitches. Budget: <1%
  hitch time.
- **P2 — Magnification jank.** The Dock-style magnify animation holds frame rate during fast
  scrub. Budget: no hitch > 16ms.
- **P3 — Replica load/parse.** `measure` time to fetch+decode+index a large replica
  (e.g. 5k–20k editions). Budget: cold parse under a target (e.g. < 1.5s for 10k) and a 304
  re-launch under ~100ms. Memory ceiling asserted.
- **P4 — Search latency.** Keystroke→results over the large replica (in-memory/SQLite) under
  ~50ms per query at 10k editions; type-ahead stays responsive.
- **P5 — Content FTS query.** Offline `match_fts` over a multi-hundred-MB content index: assert
  **constant memory** (page-on-demand, mirroring the PWA's OPFS engine intent) and query latency
  budget; assert it does **not** load the whole DB into RAM.
- **P6 — Reader time-to-first-page.** Open a large PDF over HTTP range from the LAN server →
  first page visible. Budget vs. the PWA-handoff baseline (which must download the whole file into
  PDF Expert/GoodReader first). This is the metric that justifies the native reader (see tradeoffs
  below) — capture it as a regression guard.
- **P7 — Annotation render scale.** Render a page carrying many ink strokes / highlights;
  pan/zoom stays 60fps; ink redraw budget held. PencilKit input latency sampled.
- **P8 — Cold app launch.** Time to first interactive frame. Budget under a target (e.g. < 800ms
  warm, < 2s cold) using `XCTApplicationLaunchMetric`.
- **P9 — Memory under big PDF.** Peak memory streaming/reading a large PDF stays bounded (PDFKit
  paging, not whole-file in RAM).

### 7.4 Test infrastructure tasks

- **T1** — Fixture capture script: hit a seeded live server, save each `/api/v1/*` + `/sync/reader`
  response under `Fixtures/` (real shapes, not hand-written).
- **T2** — Golden generator: tiny Node script runs `library-core.js` view-models over the same
  fixtures → `Fixtures/golden/*.json` for U2/U3/U5 parity asserts (keeps web the source of truth).
- **T3** — Live-server harness: a Swift `XCTestCase` setup that boots the Flask `app_env`+`seed`
  fixture (via a shell helper) on a free loopback port and tears it down; base URL injected into
  the app under test.
- **T4** — CI note: document that these run on a macOS+Xcode machine (`xcodebuild test
  -scheme Catalogue -destination 'platform=iOS Simulator,name=iPhone 15'` and an iPad destination);
  perf suite on a tethered device. Not runnable in the current agent env.

---

## 8. Implementation order (each step shippable + tested)

1. Xcode project + `Palette.swift` generation from `palette.json` (extend `gen.py`) → **U7**.
2. Codable models + `CatalogueAPI` adapter (live only) → **U1**, **T1**.
3. Tier-2 port (`LibraryCore`, refs, protocols, normalize) → **U2–U6, U11, T2**.
4. SwiftUI shell + nav + Home/Search/Browse/Content/Detail/Subject/Settings → **S1–S4, S10, S2**.
5. Replica + file cache + offline content index → **S7, P3–P5**.
6. Reader: embed **octavo-swift** (`PdfKitNavigator`) behind the `Source`/`ReadingStore` ports +
   `Locator`-based position → **U8, S3, S6, P6, P8, P9**.
7. **postilla**: DecorationHost marks + PencilKit-input ink (perfect-freehand-Swift) +
   `AnnotationStore` over `/sync/reader` + `annotated.pdf` export → **U9, U10, S5, S8, P7**.
8. Perf pass on device + adaptive iPad split-view polish → **P1, P2**.

> **Dependency / sequencing.** Steps 1–5 (the catalogue-app browse/search/offline half) stand alone
> and can start now. Steps 6–7 (the reader) **depend on `octavo-swift` + `postilla-swift` existing** —
> those packages are pre-build (`../../../octavo-postilla/README.md`). Build order across repos:
> `octavo-swift` OS-M1→M3 → `postilla-swift` PS-M1→M2 → wire them here as steps 6–7. The SDK-level
> reader tests (**U8, U9, U10, S5, P6, P7, P9**, EPUB half of **S8**) move into the SDK packages
> (`octavo-swift_plan.md` §5, `postilla-swift_plan.md` §5); this plan keeps host-integration tests.

---

## 9. Open questions (decide before/early in build)

- **Nav form:** native `TabView` (most iOS-native, recommended) vs. porting the PWA's floating-arc
  FAB for pixel-identical feel. Affects S2.
- **EPUB renderer:** *now octavo-swift's open decision*, not the app's (octavo §10: epub.js-in-
  WKWebView vs. Readium Swift — leaning epub.js-reuse for CFI parity with the PWA). The iOS app
  consumes whichever `EpubWebNavigator`/Readium-Navigator octavo ships; tracked here only because it
  affects S8.
- **Local store:** GRDB (SQLite, lets us reuse FTS5 + the content-index file directly) vs.
  SwiftData. GRDB recommended for FTS parity with the server.
- **Distribution:** personal sideload (free 7-day) vs. TestFlight vs. App Store — drives signing.
