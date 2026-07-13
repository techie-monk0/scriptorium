# catalogue-app — build status (overnight implementation)

Native iOS reader/library client + the two reader SDKs it hosts, implemented from the plans in
`catalogue-app/docs/ios_native_plan.md` and `octavo/docs/{octavo,postilla}-swift_plan.md`.

Layout: **`catalogue-app/`** holds `docs/` + `ios/` (and a future `android/` sibling). The iOS app is
`catalogue-app/ios/CatalogueApp-Pkg/` (SwiftPM package — all the code/tests) + `catalogue-app/ios/CatalogueApp-XC/` (the runnable Xcode app bundle).

## What's green (verified on this Mac — Xcode 26.4 / Swift 6.3 / iPhone 17 sim)

| Package | Command | Result |
|---|---|---|
| `octavo-swift` | `swift test` | **30 tests pass**; iOS-sim build of all targets (PDFKit/WebKit) succeeds |
| `postilla-swift` | `swift test` | **27 tests pass**; iOS-sim build (incl. PencilKit) succeeds |
| `catalogue-app/ios/CatalogueApp-Pkg` | `swift test` | **52 tests pass** |
| `catalogue-app/ios/CatalogueApp-Pkg` | `xcodebuild -scheme CatalogueUI -destination 'iOS Simulator'` | **BUILD SUCCEEDED** |
| palette lockstep | `python -m pytest tests/test_palette_master.py` | **4 pass** (incl. new Palette.swift drift guard) |

**109 Swift tests + 4 Python tests, all passing.** Run them all: `make`-free, just the three
`swift test`s above + the pytest.

## Steps 1–7 (one commit each, in `git log`)

1. **Scaffold + palette** — `theme/gen.py` extended to emit `Palette.swift` from `palette.json`
   (drift-tested both ways). `CatalogueDesign`: Tokens/Typography/Spacing/ThemePreference. *(U7)*
2. **Models + API** — Codable mirrors of every `/api/v1` + replica shape; `CatalogueAPI` URLSession
   adapter (rows→Cards like `library-web.js`, ETag/304 replica). *(U1, U3)*
3. **Tier-2 port** — `LibraryCore` view-models ported 1:1 from `library-core.js`; **proven byte-equal
   to the real JS** via a Node golden generator (`Tools/gen_goldens.mjs`). fold + protocols. *(U2,U4,U5)*
4. **SwiftUI shell** — `TabView` + Home/Search/Browse/Content/Detail/Subject/Settings, themed from the
   palette, bound to the Tier-2 view-models.
5. **Offline** — `ReplicaData` (replica-served Search/Browse, diacritic-folded), `OfflineFirstData`
   selection, `ReplicaStore` (ETag cache), `FileCache`, `ContentIndex` facade. *(U6)*
6. **Reader** — `CatalogueReadingStore` (octavo `ReadingStore`, Locator position), `HoldingBytes`
   (storage seam), `ReaderView` hosts a `PdfKitNavigator` via `Octavo.open`. *(U8)*
7. **Annotations** — `ReaderSync` (postilla `AnnotationStore` over `/sync/reader`), `PdfDecorationHost`
   (marks → PDFAnnotations), highlight round-trip in `ReaderView`.

## Deferred / known gaps (honest)

- **App bundle exists** (`CatalogueApp-XC/`, XcodeGen) and runs in the simulator. The remaining gap is
  the **XCUITest S-suite** + perf **P-suite** — add them as a UI-test target in `project.yml` now that
  there's a launchable app.
- **`/sync/reader` shape.** `ReaderSync` speaks postilla's native `{rev, ops}`; the catalogue's
  existing endpoint uses the legacy `{id, holding_id, cfi_range, page, rect, …}` record. Mapping the
  two (or adding a postilla-shaped route) is a server/client reconciliation — `ReaderView` defaults to
  the in-memory store so marks work locally meanwhile.
- **EPUB** — `octavo-swift`'s `EpubWebNavigator` is an M3 skeleton (WKWebView host; open/search TODO);
  the reader currently opens PDF holdings. (`S8` blocked on it.)
- **Ink-on-PDF render** — postilla *stores* canonical ink (raw `[x,y,pressure]`, tested) and ships
  `FreehandRenderer`; drawing captured PencilKit strokes back onto the PDF page is not yet wired into
  `ReaderView` (highlight marks are). Offline content **FTS5** index is a facade + `NoContentIndex`.
- **Live-server tests (T1/T3)** use hand-authored fixtures, not captured from a seeded server.

## Test ownership note

The SDK-level reader tests the iOS plan lists (U8/U9/U10/S5…) now live in the SDK packages
(`octavo-swift` OS-U*, `postilla-swift` PS-U*); `ios_native_plan.md` §7 should be trimmed to reference
them rather than duplicate. The catalogue-app keeps host-integration + Tier-2 parity tests.
