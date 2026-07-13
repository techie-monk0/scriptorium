# octavo-swift — iOS/macOS binding (implementation & test plan)

The Swift binding of **`octavo`** (`octavo.md`): the SwiftPM package the **`catalogue-app`**
(`../../../catalogue-app/docs/ios_native_plan.md`, impl step 6) hosts for in-app reading. It implements the
host-neutral **octavo contract** in Swift — `Locator`, the `Source`/`ReadingStore` ports, the
`Navigator` protocol, `Capabilities`, the `DecorationHost` seam — and ships the per-platform
**Navigator** engines: **`PdfKitNavigator`** (PDFKit) and **`EpubWebNavigator`** (epub.js in
WKWebView). It is the iOS counterpart of `octavo-web`; the two must produce **identical Locators**
for the same input (the cross-binding parity guarantee).

This is the **base** binding — annotations/handwriting are `postilla-swift` (`postilla-swift_plan.md`),
which depends on this package's `DecorationHost` seam.

**Build/run caveat (every task):** Swift is authored in-repo but **compiled, run, and tested in
Xcode on a Mac**; the agent environment cannot build it. All `xcodebuild test` tasks run locally
until a macOS runner exists.

---

## 1. Package layout

SwiftPM package, product/import root **`Octavo`** — a sibling of `catalogue-app`, **not** under it
(the SDK must be embeddable by anyone). `catalogue-app` adds it as a local SwiftPM dependency now,
a versioned one post-extraction.

```
octavo-swift/                       # → SwiftPM package "Octavo"
  Package.swift
  Sources/
    Octavo/                         # the binding (mirror of octavo-core, in Swift)
      Locator.swift                 # Codable Locator {publicationId, format, locations, text?}
      Publication.swift             # {format, metadata?, resources}; format sniffing (magic bytes)
      Source.swift                  # PORT: read(range)->bytes · length · contentType (async)
      ReadingStore.swift            # PORT: getPosition · setPosition(Locator) · recent(n)
      Capabilities.swift            # canAnnotate · canExport · …
      Navigator.swift               # PROTOCOL: open · goTo · next/prev · search->[Locator] · outline
      DecorationHost.swift          # seam postilla-swift plugs into (apply/clear decorations)
      Octavo.swift                  # façade: Octavo.open(source:format:readingStore:host:…)
    OctavoPDFKit/                   # Navigator impl — PDF
      PdfKitNavigator.swift         # PDFView host; page<->Locator; PDFSelection search; outline
    OctavoEPUB/                     # Navigator impl — EPUB
      EpubWebNavigator.swift        # epub.js in WKWebView; CFI<->Locator; JS bridge
      epubjs-bridge/                # vendored epub.js + a thin message bridge (shared w/ web intent)
    OctavoAdapters/                 # reference ports (optional, also used by tests/examples)
      FileSource.swift              # native disk — the zero-transfer fast path
      HttpRangeSource.swift         # HTTP range requests, 1MB-chunk lesson baked in
      MemoryReadingStore.swift      # in-memory ReadingStore (tests/examples)
  Tests/
    OctavoTests/                    # unit (pure)
    OctavoParityTests/              # golden-corpus parity vs octavo-web
    OctavoPerfTests/                # XCTest measure / Instruments
  Examples/
    OctavoDemo/                     # minimal embed (the integration tutorial as runnable code)
  Fixtures/                         # golden corpus (sample pdf + epub) + expected Locators/TOC
```

`OctavoPDFKit` / `OctavoEPUB` are separate SwiftPM targets so an integrator who only needs PDF
doesn't pull WKWebView/epub.js.

---

## 2. Scope — reuse vs build

| Piece | Source of truth | Swift action |
|---|---|---|
| `Locator`, `Publication`, format sniff | `octavo.md` §3 + `octavo-core` (web) | **Mirror** as Codable; round-trip parity with web JSON |
| `Source`/`ReadingStore`/`Capabilities` ports | contract | **Define** as Swift protocols (async/await) |
| `Navigator` protocol | contract | **Define**; `goTo(locator)` ⇄ `onLocationChanged` invariant |
| `PdfKitNavigator` | new (PDFKit) — *the fast disk-access path* | **Build**: `PDFView`, `PDFDocument(url:)`/`(data:)`, `PDFSelection` search, `outlineRoot` TOC |
| `EpubWebNavigator` | reuse epub.js (parity w/ PWA CFI pipeline) | **Build**: WKWebView + epub.js + JS↔Swift bridge; CFI is the Locator location |
| `HttpRangeSource`/`FileSource` | `octavo.md` §3 reference adapters | **Build**: range math + 1MB chunking; `FileSource` zero-copy |
| `DecorationHost` | seam for postilla | **Define** only (impl is postilla-swift) |

The engine **never** learns where bytes come from (the kDrive-doesn't-leak seam) and **never**
contains a catalogue route literal — those arrive through the injected `Source`.

---

## 3. Public API (Swift mirror of `octavo.md` §4)

```swift
let reader = try await Octavo.open(
    source: FileSource(url: cachedURL),        // or HttpRangeSource(url:) over /holding/<id>/file
    format: .pdf,                              // or sniffed from the Source
    readingStore: myReadingStore,              // PORT — persists a Locator
    capabilities: .init(canAnnotate: true),
    host: pdfContainerView)                    // a UIView/NSView
reader.onLocationChanged { loc in myReadingStore.setPosition(id, loc) }
try await reader.goTo(Locator(locations: .init(page: 42)))
let hits = try await reader.search("dependent origination")   // -> [Locator]
let toc  = reader.outline()
reader.decorations.apply(...)                  // DecorationHost — postilla plugs in here
```

Shape is the idiomatic mirror of the web/TS API; the **contract** (`octavo.md`) is the source of
truth, this binding a thin tested reflection of it.

---

## 4. Boundary enforcement (the "host-free" guarantee, Swift side)

The Python import-linter + JS eslint guards in `octavo.md` §5.1 don't see Swift. Two cheap
equivalents in this package's CI:

1. **No catalogue dependency:** the `Octavo` package declares **zero** dependency on the
   `catalogue-app` target; a CI step asserts `swift package show-dependencies` contains no
   catalogue/app target. (The dependency arrow is one-way: app → Octavo, never the reverse.)
2. **No hardcoded host routes:** a grep test asserts `Sources/Octavo*/` contains **no** literal
   `/api/v1`, `/holding/`, `/sync/reader` — those must arrive through an injected `Source`/adapter,
   never as a constant in the engine.

---

## 5. Test plan (mirrors the repo's unit / system / perf split)

### 5.1 Unit (`OctavoTests` — pure, no network/UI)
- **OS-U1 Locator round-trip.** Encode/decode every `Locator` shape (pdf `page`, epub `cfi`,
  `progression`, `text` context) losslessly; **byte-parity with the web JSON** for the same Locator
  (golden file from `octavo-web`).
- **OS-U2 Format sniff.** Magic-byte/extension sniffing picks `pdf` vs `epub` on the corpus; ambiguous
  input resolves per the contract.
- **OS-U3 Source range math.** `HttpRangeSource` issues correct `Range:` headers, honors 1MB
  chunking, reassembles; `FileSource` returns exact byte windows; both report `length`/`contentType`.
- **OS-U4 ReadingStore conformance.** Parametrized contract test over `MemoryReadingStore` **and** a
  real adapter: `setPosition`/`getPosition` round-trip a `Locator`; `recent(n)` orders by recency;
  corrupt/missing record tolerated. (This is `catalogue-app` test **U8**, now owned here.)
- **OS-U5 goTo ⇄ onLocationChanged.** `goTo(loc)` then the next emitted location resolves to the same
  Locator (page/CFI/progression consistency) for both navigators.

### 5.2 Cross-binding parity (`OctavoParityTests`)
- **OS-P-Parity.** Over the **golden corpus** (`Fixtures/`), assert `search(q)` returns the **same
  ordered Locators** and `outline()` the **same TOC** as `octavo-web` produced for the identical
  query (web exports goldens; Swift asserts equality). This is the guarantee that a
  bookmark/citation made on web resolves natively and vice-versa.

### 5.3 System (`OctavoTests`/Example via XCUITest)
- **OS-S1.** `OctavoDemo` opens each corpus book from `FileSource` **and** `HttpRangeSource`, renders
  first page, navigates next/prev, runs a search and jumps to a hit, restores position across reopen.
  (Feeds `catalogue-app` **S3/S6**.)
- **OS-S2.** EPUB path: open epub via `EpubWebNavigator`, position saves/restores as a `cfi` Locator,
  outline renders. (Feeds `catalogue-app` **S8**.)

### 5.4 Performance (`OctavoPerfTests` — `measure` + Instruments, on device)
- **OS-PF1 First-page time:** range-stream vs whole-file; add a **`FileSource` zero-transfer
  baseline** to quantify the native disk win vs the PWA-handoff path. (= `catalogue-app` **P6**.)
- **OS-PF2 Nth-page seek**, **OS-PF3 search latency**, **OS-PF4 memory under a large PDF** (PDFKit
  paging, not whole-file in RAM) — reuse the ceilings in the server's `test_reader_perf.py`.
  (= `catalogue-app` **P9**.)

> **Test ownership note:** the SDK-level reader tests (`catalogue-app` **U8, S3/S6/S8, P6, P9**) move
> *into this package*; `catalogue-app` keeps only host-integration tests (ports wired, screen mounts,
> end-to-end through the real server). Update `ios_native_plan.md` §7 to reference these when this
> lands.

---

## 6. Milestones (track `octavo.md` M1/M3 + `catalogue-app` step 6)

1. **OS-M1 — contract mirror + boundary.** `Octavo` target: `Locator`, ports, `Navigator` protocol,
   façade stub, §4 CI guards. No rendering yet. (Decouples cleanly from web at the contract level.)
2. **OS-M2 — PdfKitNavigator + adapters.** `OctavoPDFKit` + `FileSource`/`HttpRangeSource`; OS-U1–U5,
   OS-S1, OS-PF1–PF4 green. This is the disk-access fast path that justifies the native reader.
3. **OS-M3 — EpubWebNavigator.** `OctavoEPUB` (epub.js-in-WKWebView + bridge); OS-S2; CFI parity.
4. **OS-M4 — parity gate.** Golden corpus + `OctavoParityTests` green against `octavo-web`.
5. **OS-M5 — example + dogfood.** `OctavoDemo`; `catalogue-app` repoints its reader onto the package.

---

## 7. Open decisions

- **EPUB engine:** epub.js-in-WKWebView (CFI parity with the PWA, recommended) vs. Readium Swift
  (more native, BSD-3, bigger dep). Inherited from `octavo.md` §10; this binding implements whichever
  ships. Affects OS-S2 / `catalogue-app` S8.
- **macOS target:** ship `Octavo` for macOS too (Catalyst-free AppKit host) or iOS-only for v1.
  `NSView`/`UIView` host abstraction is cheap to keep open now.
- **Async surface:** all-`async/await` (recommended) vs. a Combine `Publisher` for
  `onLocationChanged`. Lean async + an `AsyncStream` for location changes.
- **epub.js vendoring:** share one vendored epub.js + bridge with `octavo-web`, or vendor
  independently. Sharing keeps CFI semantics identical across bindings.
