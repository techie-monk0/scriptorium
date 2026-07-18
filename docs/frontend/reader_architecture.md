# Reader architecture (web · PWA · iOS)

The reader is the **sixth cross-surface feature** and obeys the same rule as the rest of the
[frontend contract](./frontend_contract.md): a feature is defined **once** as a platform-neutral
abstraction and **rendered per toolkit**. What is unusual about the reader — and the reason it has
its own doc — is that below the shared spec it has **two rendering engines** (a native one and a
web one) that must produce the **same reading positions** so bookmarks, annotations, and progress
sync across every surface. Everything here is what we converged on after the divergences kept
recurring; treat it as the contract a change must not break.

Companion docs: [`reader_module_plan.md`](../plans/reader_module_plan.md) (the original web
`reader-core` extraction + offline-first sync data model) and
[`frontend_surface_convergence_plan.md`](../plans/frontend_surface_convergence_plan.md) (why the
surfaces drift and how the shared-VM model fixes it — the reader chrome is the proof case).

---

## Tier map (reader-specific)

```
 Tier 1    Reading data over HTTP                     GET /holding/<id>/file · POST/GET /sync/reader
 Tier 2    Shared reader view-models (pure, golden)   readerChromeVM · readerTabsVM · reflowPageText
 Tier 2.5  Reading-theme tokens                       palette.json reading_themes → reader-themes.css + ReadingPalette.swift
 Tier 3a   Reading ENGINE (per platform, ports)       octavo (native) | reader-core.js (web+PWA)
 Tier 3b   Chrome RENDERER (per toolkit)              SwiftUI ReaderView | reader.html/reader.js render els
```

The chrome (bars, buttons, panels) is a **shared spec**; the engine (how bytes become pages) is a
**port with two adapters**. Keep those two concerns separate — a control added to the spec must not
assume a specific engine, and an engine capability must be surfaced through the spec, never bolted
onto one surface's UI.

---

## Tier 2 — shared reader view-models

All three live in `static/js/library-core.js` and are ported 1:1 to Swift in
`CatalogueCore` under golden parity (`Tools/gen_goldens.mjs` → `goldens.json`, asserted in
`ViewModelParityTests` + `tests/test_frontend_command_parity.py`). The JS is the source of truth;
the Swift port must be a literal mirror or CI fails by design.

- **`readerChromeVM(format, caps, …)` → `[ReaderControl{ id, bar, overflow, active }]`** — the
  ordered control set. `bar` is `general` (leading) or `text` (trailing); `overflow` collapses into
  the `⋯` menu. One definition of *what controls exist and in what order*; each surface renders each
  `id` in its own toolkit. Current ids: `done`, `toc`, `search`, `star`, `textSmaller`/`textLarger`,
  `reflow` (PDF), **`goto`**, `theme`, `bookmarkAdd`/`bookmarkList` (overflow), and the PDF
  annotation tools (`highlight`/`draw`/`underline`/`strike`/`note`/`erase`/`annList`/`export`). A
  capability a surface can't yet back is passed `false` — the control stays in the spec and lights up
  the moment that surface declares support.
- **`readerTabsVM(replica, openOrder, activeId)` → `[ReaderTabVM]`** — the multi-book tab strip
  (ordered open set + active, skipping ids no longer live).
- **`reflowPageText(raw)` → `[paragraph]`** — the PDF "reflow to text" heuristic (de-hyphenate,
  join intra-paragraph breaks, split on blank lines). Shared so iOS and web reflow identically.

Definition of `readerChromeVM`/`ReaderControl`/`ReaderCaps`: `CatalogueCore/ReaderChrome.swift`
↔ `library-core.js`.

---

## Tier 3a — the reading engine (ports & adapters)

Two engines implement the **same contract**. Neither knows about the catalogue; concretes are named
only at each surface's composition root.

### Native — `octavo` (Swift SDK, `octavo-swift/`)

`Navigator` protocol (`Sources/Octavo/Navigator.swift`), implemented by:

- **`PdfKitNavigator`** (`OctavoPDFKit`) — native `PDFView`; continuous scroll; `pageCount`.
- **`EpubWebNavigator`** (`OctavoEPUB`) — epub.js in a `WKWebView` via `epub-bridge.js`.

Contract surface (both): `open` · `goTo(Locator)` · `next` / `prev` · `bigger` / `smaller` ·
`applyTheme(ReaderTheme)` · `search` · `outline` · `pageText` · `currentLocation` ·
`onLocationChanged`. PDF adds `pageCount`; EPUB adds `goToFraction`, `onExternalLink`, and
**`onWillJump`** (fires just before an in-content link jump so the host can record a back target —
see *Navigation history*). octavo **must never `import catalogue`**; `ReaderTheme` is a neutral
hex/`isDark` value type so themes flow in without a palette dependency.

### Web + PWA — `reader-core.js` (one engine, two hosts)

`static/reader/reader-core.js` is the JS analogue, shared by the Flask page
(`templates/reader.html`) and the PWA (`static/pwa/reader.js`). Each host supplies **chrome
elements** (`els.{tocBtn, searchBtn, themeBtn, gotoBtn, reflowBtn, bmAdd, …}`) and **adapters**
(`FileStore`/`ReadingStore`/`Net`/`bookmarks`). The engine fills a `ctrl` object mirroring the
native contract: `prev`/`next` · `bigger`/`smaller` · `current()` · `goto(loc)` · `setTheme` ·
`search` · `toc` · `reflow` · **`kind`** (`pdf`|`epub`) · **`pageCount`** · **`gotoFraction`**.

> The web/PWA reader is **web** (pdf.js/epub.js are browser-only — it cannot literally be SwiftUI).
> It converges on the native *layout* by rendering the same `readerChromeVM` bars; that is as close
> as a web surface gets, and it is the intended end state.

### `Locator` and cross-surface parity — why CFI

Position is a shared `Locator` (EPUB → **epub.js CFI**; PDF → 1-based page). iOS runs the *same*
epub.js pipeline as web/PWA precisely so the **CFIs are identical across surfaces** — that is what
lets bookmarks / annotations / reading position round-trip through `/sync/reader` and mean the same
place everywhere. This parity is the core constraint. Adopting a different EPUB engine on one
surface (e.g. Readium) would break it unless its locators were translated back to these CFIs; that
trade-off is why we kept octavo's epub.js engine rather than swapping in a native one.

---

## EPUB touch handling in a `WKWebView` (the hard-won part)

epub.js renders each spine section inside an **iframe**. In a `WKWebView`, **event listeners
attached across frames from the host page do not fire reliably**, and **SwiftUI gestures placed
over the web view compete for the same touches**. The model that actually works:

1. **Links + in-content taps → a `WKUserScript` injected into every frame**
   (`EpubWebNavigator.tapScript`, `forMainFrameOnly: false`, `atDocumentEnd`). Because it runs
   *inside* each content frame it captures taps reliably. A tapped link is reported to native
   (external → open in Safari; internal → `gotoHref` → the host frame's robust `epubGo` resolver,
   which spine-matches real books' `../text/x.xhtml` hrefs). Listeners are **non-passive** so
   `preventDefault` stops the raw sub-frame navigation that WebKit cancels with **error 102** (the
   old "TOC link does nothing" bug).
2. **Same-origin fallback** — `epub-bridge.js` also binds the same handlers via the epub.js content
   hook, coordinated with the user script by a shared `document.__octavoTapBound` flag so **exactly
   one** binds (covers the case where the user script doesn't inject).
3. **Paging = native SwiftUI swipe** (a `DragGesture` — this *does* work over the web view).
4. **Blank-tap → toggle bars = native SwiftUI `TapGesture`** — the *same* mechanism PDF uses. (A
   JS-`tap`-message → captured-`@Binding` toggle did **not** fire; the native tap does.) JS never
   pages and ignores moved touches, so the swipe and the taps don't fight, and the JS `tap` post was
   removed so it can't double-toggle.

Web/PWA do the analogous in-content link interception via the epub.js content hook (cross-frame
listeners *do* work in a real browser) plus the browser's own swipe. PDF has none of this: it is a
native `PDFView` with a SwiftUI `TapGesture` — **do not route PDF through any of the EPUB path.**

---

## Navigation history — "back to where I was" (Option A)

Record a back target **only on a jump**, never on a page turn, so the affordance returns you to the
jump **origin** — not "one page less." The reader already knows a jump from a page-flip because they
are different calls: `goTo()` (TOC, search, bookmark, in-content link, Go-to) vs `next()`/`prev()`.

- **Depth:** a **stack** — nested jumps chain; going back retargets the next origin. Reset when the
  book closes (it is view/session state, not persisted).
- **Affordance:** an **Apple-Books-style persistent pill** — it appears after a jump and **has no
  timer** (a timer hid it before you'd read the page). Tap returns + retargets; `✕` dismisses; it
  never appears from page-flipping.
- **iOS:** `backStack`/`backPill` in `ReaderView`; `EpubWebNavigator.onWillJump` feeds in-content
  link jumps. **Web/PWA:** `recordBackTarget()` at each jump site in `reader-core.js`; the
  `.reader-backpill` element.

## Go to (typed)

The `goto` control (in `readerChromeVM`, so every surface has it): **PDF → a typed page number**
(1…`pageCount`); **EPUB → a position %** (`gotoFraction` via `book.locations.cfiFromPercentage`,
since EPUB has no fixed pages). iOS uses a sheet with a number field; web/PWA a small popover. A
Go-to is a jump, so it records a back target.

## Reading themes (Tier 2.5)

White / Sepia / Gray / Night (+ **Auto** = follow device), defined once in `palette.json`
`reading_themes` and generated into `static/reader/reader-themes.css` (`body[data-reader-theme]`) and
`CatalogueDesign/Generated/ReadingPalette.swift` (drift-guarded by `tests/test_palette_master.py`).
Applied through `Navigator.applyTheme(ReaderTheme)` — PDF tints the gutter / inverts the page layer
for dark; EPUB injects fg/bg via epub.js themes, re-applied on each `relocated` to avoid flash.

## Multi-book sessions (tabs)

One live engine per surface behind a disappearable top tab strip (PDF-Expert style). Open set +
active id: `OpenSessionsStore` (iOS) / `reader-sessions.js` (web). Only the active book is mounted
(memory safety); the outgoing view tears down (`epubNavigator.tearDown()`); positions still come
from the existing `ReadingStore`, not duplicated. Tabs come from `readerTabsVM`.

---

## Persistent PDF writes — annotation flatten, outline authoring (shared mechanism)

Most reader edits are *overlays*: highlights, bookmarks, and the reading position ride alongside the
file and never change its bytes — which is what makes them cheap to sync and safe offline. A few
features instead write a change **into** the PDF so any viewer (Preview, Acrobat, Apple Books) sees
it, not just our reader: flattening annotations into the file, and **authoring a table-of-contents
outline** the user edits. These all go through one shared mechanism, so the delicate "open the file,
apply the change, save either a new copy or in place" envelope is written once and the features
compose (write an outline *and* flatten annotations in a single save).

Authoring stays overlay-first: the outline the user edits is kept as synced overlay data (like
bookmarks — offline and multi-device for free) and only baked into the file bytes on an explicit
"save into PDF" action. Swapping where the outline is stored, or adding another PDF-writing feature,
touches only that feature's own adapter — never the shared writer or the other features.

Reading the PDF's *embedded* outline is unrelated and needs none of this: it comes free with the
cached file via octavo's `outline()` (`PdfKitNavigator` → `PDFDocument.outlineRoot`), works offline,
and is identical on every device because it is part of the document.

### Technical details

Server side (`catalogue.webui`): `pdf_mutation.write_pdf(src, [mutations…], mode="copy"|"inplace")`
is the shared executor — it owns opening the file, applying mutations in order, and saving
(copy = new file with `garbage/deflate`; inplace = `incremental=True` so the original bytes/signature
survive). Each feature is a `PdfMutation` (a `.apply(doc)` on an open PyMuPDF document):
`annotate_export.AnnotationFlatten` (annotations → standard PDF constructs; `export_annotated` now
delegates to the shared writer) and `outline_export.OutlineWrite` (entries → `doc.set_toc`). Where
authored entries live is a separate seam — `outline_store.OutlineStore`: the real adapter is
`ReaderStateOutlineStore` (over `reader_state`, so the outline is a `/sync/reader`-synced overlay like
bookmarks — one wholesale `Outline` row per copy, LWW by a stable per-copy id), with an in-memory
reference adapter for tests; the read side of the file's own embedded outline is
`services.toc.extract_pdf_outline`. The bake routes are `GET /holding/<id>/outlined.pdf` (copy) and
`POST …/outlined` (in-place, localhost-only), mirroring the annotated routes.

**Done (server):** the shared write mechanism, the outline mutation, the synced `Outline` sync-of-record
(wire contract **v2** — `outline` record + op), the DB-backed `OutlineStore`, and the bake routes.

**Done (iOS client):** a shared `editOutline` control in `readerChromeVM` (JS source of truth + Swift
port + goldens, so every surface gets the entry point); the client outline sync stack mirroring
bookmarks — `OutlineSync` (transport over `/sync/reader`), `LocalOutlineStore` (durable offline outbox +
`OutboxProbe`, folded into the "N unsynced" chip), the `OutlineEntry` wire codec, and `OutlineModel`
(pure flatten of the embedded TOC to seed the editor). The reader presents a **unified "Contents" panel**
(PDF-Expert style): one popup with a segmented **Bookmarks | Outline** picker, reached from a single ⋯
"Contents" entry. Both tabs default to **tap-to-jump** (view mode); an **Edit/Done** toggle reveals the
editing affordances — Bookmarks: Add / rename (tap a row) / delete / Clear All; Outline: add (focuses the
new title) / rename / reorder / delete, with **Done** (or Close, or a tab switch) syncing the outline and
**Save into PDF** baking it via `/holding/<id>/outlined.pdf`. Names are user-editable (a dialog for
bookmarks, inline for outline); rows show the page number in small font. This consolidation is an
**iOS rendering composition**: the shared `readerChromeVM` spec still lists `bookmarkAdd`/`bookmarkList`/
`editOutline` as capabilities (iOS folds them into the one panel), so the web reader is unaffected;
lifting the combined panel into the shared spec is a follow-up. The pure/
sync/store logic is unit- + system-tested headlessly (`OutlineWireTests`, `OutlineModelTests`,
`LocalOutlineStoreTests`, `OutlineSyncTests`, `ViewModelParityTests`); the SwiftUI editor sheet's
rendering/interaction is only observable in the simulator (not asserted headlessly).

**Not yet built:** the web/PWA renderer of the `editOutline` control (the shared spec now carries it, so
it lights up when each web surface renders the sheet); Android.

## Composition-root decisions

- **Native reader everywhere on iOS.** Tapping a book and the **Read** tab both open the *same*
  native `ReaderShell`. The web-hosted EPUB prototype (`WebEpubReaderView`) is **un-wired** — native
  is the only iOS path. (The prototype file remains, dormant, for reference.)
- **Holding URLs are absolute from the host root.** `HoldingBytes` / `WebEpubReaderView` build
  `/holding/<id>/file` (and `/read`) via `URLComponents` with an **absolute path**, never
  `appendingPathComponent`, so a configured base URL carrying an `/app` prefix does not become
  `/app/holding/<id>/file` (→ 404). This matches how `/sync/reader` and `/api/v1/*` already set their
  paths (which is why those worked when file fetches didn't).

---

## Critical files

| Concern | Native (iOS) | Web / PWA |
|---|---|---|
| Chrome spec (Tier 2) | `CatalogueCore/ReaderChrome.swift` | `static/js/library-core.js` (`readerChromeVM`) |
| Chrome renderer (Tier 3b) | `CatalogueReader/ReaderView.swift` | `templates/reader.html` · `static/pwa/reader.js` (`els`) |
| Engine (Tier 3a) | `octavo-swift/Sources/Octavo` + `OctavoPDFKit` + `OctavoEPUB` (+ `epub-bridge.js`) | `static/reader/reader-core.js` |
| Themes (Tier 2.5) | `CatalogueDesign/Generated/ReadingPalette.swift` | `static/reader/reader-themes.css` (from `theme/palette.json` + `gen.py`) |
| Multi-book | `CatalogueReader/OpenSessionsStore.swift` · `ReaderShell.swift` | `static/reader/reader-sessions.js` |
| Sync (Tier 1) | `CatalogueReader/*Sync*`, `LocalBookmarkStore.swift` | `ReadingStore` / bookmarks adapters → `/sync/reader` |
