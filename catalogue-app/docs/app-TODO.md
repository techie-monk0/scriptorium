# catalogue-app — TODO

Living tracker of what's built vs. what's left. Companion to `STATUS.md` (point-in-time green status)
and `ios_native_plan.md` (the plan). Branch: `ios-catalogue-app`.

Legend: `[x]` done & verified · `[~]` partial / scaffolded · `[ ]` not started.

---

## ✅ Done (verified: ~114 Swift tests + 4 Python; iOS-sim builds; runs against the live server)

### App (catalogue-app)
- [x] **Step 1** — SwiftPM scaffold; `Palette.swift` generated from `palette.json` via `gen.py` (drift-tested). *(U7)*
- [x] **Step 2** — Codable models for every `/api/v1` + replica shape; `CatalogueAPI`. *(U1, U3)*
- [x] **Step 3** — Tier-2 port (`LibraryCore` view-models) proven byte-equal to real `library-core.js` via Node goldens. *(U2, U4, U5)*
- [x] **Step 4** — SwiftUI shell (TabView) + Home/Search/Browse/Content/Detail/Subject/Settings.
- [x] **Step 5** — replica-served offline Search/Browse, `ReplicaStore`, `FileCache`, `ContentIndex` facade. *(U6)*
- [x] **Step 6** — reader hosts octavo `PdfKitNavigator`; Locator position persists/restores. *(U8)*
- [x] **Step 7** — postilla annotations: `ReaderSync` (AnnotationStore over `/sync/reader`), highlight round-trip rendered as PDFAnnotations.
- [x] **Runnable app bundle** — `CatalogueApp-XC/` (XcodeGen); launches in simulator, loads real shelves.
- [x] **Abstract `ServerEndpoint`** — LAN / tunnel / NAS / Direct strategies + auth-header hook + persistable descriptor; Settings server picker with a `/health` probe.

### Cross-surface consistency (one shared Tier-2 layer; web/PWA/app)
Tracked in **`private/plans/frontend_tiers_and_home_upgrade.md`** — built + test/goldens-green, but the
SwiftUI rendering + web/PWA JS still need a **simulator/browser pass** (not yet visually verified).
- [x] **Home onto the tiers** — `homeVM` (shared, parity-locked) computes the rails; web + PWA + iOS all render it.
- [x] **Shared component contract** — `APP_SECTIONS` nav manifest (rename Search→Books, Browse→Search), cover contract (`BOOK_COVER_ASPECT` / `SERIES_COVER_STYLES`, `setStyle`/`shelfTitles`), `BookCover`/`SeriesCover`, `SEARCH_FIELDS` + `BOOK_DETAIL_SECTIONS` → iOS `BookDetailsPane`.
- [x] **Search = one box + 4-way mode selector** (Edition title/number · Work · Person · Subject/Series) over the shared `browseReplica`.
- [x] **One shared matcher** — `searchReplica`/`browseReplica`/`suggestReplica`/`subjectVM` (JS→Swift, parity-locked); PWA migrated off its own fold/search/home/subject; digraph fold + offline authority (`nameKey` ordinals + baked office↔incumbent).

### SDKs (repo root)
- [x] **octavo-swift** — `Octavo` (Locator, ports, Navigator), `PdfKitNavigator`, adapters. *(30 tests)*
- [x] **postilla-swift** — `Postilla` (annotation/ink model, LWW SyncEngine, ports), `PostillaUI` (PencilKit input, FreehandRenderer). *(27 tests)*

---

## 🔜 To do

### Reader (highest-value gaps)
- [ ] **EPUB**: octavo `EpubWebNavigator` is a WKWebView skeleton — implement open/goTo/next/prev/search/outline + CFI↔Locator. Decide engine (epub.js-in-WKWebView vs Readium). Unblocks `S8`.
- [ ] **Ink on PDF**: wire PencilKit capture → store as ink `Annotation` → render via `FreehandRenderer` onto the page. (postilla *stores* canonical ink already; on-PDF render not wired into `ReaderView`.)
- [ ] **Text-anchored highlight rects**: base `Decoration` carries no rect, so highlights draw as a page band. Extend so a mark anchors to its actual selection rect(s).
- [ ] **In-reader search UI** (octavo `search` exists; no UI) and **outline/TOC** navigation.
- [ ] **HTTP range streaming** for large PDFs (currently whole-file download into `FileCache` on first open).
- [ ] **Recognition** (postilla `Recognizer` port + Apple Vision adapter) — handwriting→text; ship experimental, Devanagari xfail.
- [ ] **Export / Share**: `annotated.pdf`. **Decided approach** (deferred, from the update-model work): flip the shared chrome-VM `export` capability on for iOS, wire an Export/Share control to `GET /holding/<id>/annotated.pdf` (reuses the tested PyMuPDF server flatten; works over the tunnel) → present in an iOS share sheet. Native `PostillaExport`/`PdfFlatten` (offline flatten) is the follow-up. No code yet.

### Sync / annotations
- [x] **Reconcile `/sync/reader`** — `ReaderSync`/`BookmarkSync` map postilla ↔ the legacy `{rev, ops}` record; the reader syncs marks to the server/web (no longer in-memory-only).
- [x] **Reader annotation freshness (cross-device)** — incremental `?since=<rev>` delta pull merged in place (never repositions), driven on foreground + a light poll while open, so a mark made on another device appears without close+reopen. Optimistic local render so a mark shows instantly (even offline).
- [x] **Cross-device reading position** — `PositionSync` mirrors the local position to `/holding/<id>/position`; on open, an advisory "Resume · <where> (another device)" pill offers a jump (never auto-jumps).
- [ ] **Durable offline reader outbox** — reader pushes are immediate + optimistic, but a highlight made **offline** isn't yet queued to disk for retry on reconnect (in-memory only for the session). Wire a persistent op-queue (mirror the PWA IndexedDB outbox / postilla `SyncEngine`).
- [~] **PWA/web reader cross-device marks** — a **⟳ Refresh** button in the reader chrome (both `reader.js` + `reader.html`) + a tab-return trigger re-pull marks and repaint via `reader-core.js reloadMarks()` (`overlay.load()`+`repaint()`). Still open: *automatic* while-open refresh beyond that, and folding the button into the shared `readerChromeVM` spec (iOS auto-refreshes instead). See `private/frontend/sync_architecture.md`.

### Freshness / update model (shared `syncVM` + `SyncEngine`)
- [x] **One update model** — `SyncEngine` (transport-agnostic; `PullTransport` now, SSE **push seam** via `SyncTransport.subscribe` later) revalidates registered ETag resources (replica + starred) on appear/foreground/online/manual; `dataRevision` bumps repaint open screens. Fixes the "new editions invisible until relaunch" staleness.
- [x] **Portable pull-to-refresh + freshness chip** — Tier-2 `syncVM` (golden-locked JS↔Swift) → `SyncStatusPill` + `catalogueRefreshable()` on iOS; PWA status chip + `visibilitychange`/`focus` refresh + pull-to-refresh; web Home repaints on tab-return. Android = implement the ports + render `syncVM`.
- [ ] **Push transport** — server `/api/v1/events` (SSE) emitting a resource-changed ping + an `EventStreamTransport` implementing `SyncTransport.subscribe`; drops in with zero changes to `SyncEngine`/`syncVM`/UI.

### Offline
- [x] **Use the replica in the live app** — `AppModel` platform is `OfflineFirstData` + `ReplicaStore`; Search/Browse/Detail are replica-served (offline-first, live fallback), Home reads the same replica.
- [ ] **Offline content FTS5**: real SQLite-FTS5 engine behind the `ContentIndex` facade + `/api/v1/content-index` download; match server `match_fts` semantics. *(U11)*
- [x] **Home "Recently opened / added" rails** — `homeVM` composes recent/added/subject/series from the replica + `ReadingStore.recent()`.
- [x] **Cover image caching/perf** — `CoverImageLoader` (in-memory `NSCache` + de-duplicated, lifecycle-independent fetch + 256 MB disk `URLCache`) replaces `AsyncImage`; fixes re-fetch-on-scroll and blank covers. `CachedImage` used by every cover surface.

### UI / UX
- [ ] **iPad adaptive** — `NavigationSplitView`, size-class grid columns.
- [~] **Shelf fidelity** — Dock-style magnify (`visualEffect`, distance-based) + series set-tile → drawer + `SeriesCover` styles DONE; honoring `shelfArt` (spine vs cover) still open. *(needs sim check)*
- [ ] **Settings** — explicit endpoint **kind picker** + **auth-header fields** (e.g. CF-Access service tokens) instead of URL-only inference.
- [~] **Subject page** — shared `subjectVM` (children shelves + breadcrumbs) built + parity-locked, used by the PWA; iOS `SubjectScreen` still on the live `/api/v1/subject` — wire it to `subjectVM` for offline + parity.
- [ ] **Diagnostic empty/error states** app-wide (done for the Settings probe; Home still shows a bare "No shelves yet.").

### Testing
- [ ] **XCUITest S-suite (S1–S10)** — add a UI-test target to `CatalogueApp-XC/project.yml` (app bundle now exists) + a seeded-server harness `(T3)`.
- [ ] **Perf P-suite (P1–P9)** on a real device.
- [ ] **Live fixtures (T1)** captured from a seeded server (currently hand-authored).
- [ ] **octavo parity tests** vs an `octavo-web` reference renderer (not built); **postilla ink parity** (PS-Parity) vs web render + flattened PDF.

### Other SDK work
- [ ] **octavo-web** reference renderer (pdf.js/epub.js) — needed to generate cross-binding parity goldens and to dogfdood the web reader on the SDK.
- [ ] **postilla-export** sub-package (PDF flatten / W3C Web Annotation JSON).

### Android (future — `catalogue-app/android/`)
- [ ] Decide **port-with-parity (Kotlin, share contract + goldens)** vs **Kotlin Multiplatform (share one Kotlin core)** — write the decision doc first.
- [ ] Tier-2 Kotlin port (passes the same `goldens.json`), `Palette.kt` from `gen.py`, `octavo-kotlin` / `postilla-kotlin` bindings, Compose UI.

### Distribution / ops
- [ ] Signing team for device; app icon + launch screen; TestFlight / App Store.
- [ ] Server ops note: bind Flask `0.0.0.0` for LAN device access; tunnel auth if `library.example` is behind Cloudflare Access.

### Docs
- [ ] Trim `ios_native_plan.md` §7 — SDK-level reader tests (U8/U9/U10/S5…) now live in octavo/postilla packages.
- [ ] (Offered) KMP-vs-native-port decision doc before Android starts.
