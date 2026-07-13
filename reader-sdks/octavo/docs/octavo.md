# Reader SDK — plan (a generic, embeddable reading engine)

Package **`octavo`** (the engine) — paired with **`postilla`**, the annotations/handwriting/
recognition extension (`postilla.md`). A host-agnostic reading engine
others can embed: open a book from any byte source, render/paginate/search/navigate PDF + EPUB (extensible),
expose a stable **Locator** model, and leave storage, sync, and UI chrome to the integrator. This
is the **base** SDK; annotations/handwriting/recognition are a separate extension
(`postilla.md`) built on its seams.

**Why this exists:** there is no embeddable, cross-platform, *headless* reader core. Finished apps
(PDF Expert, GoodReader) are closed destinations; engines (pdf.js, epub.js, PDFKit, Readium) each
solve one format on one platform with no shared contract, no neutral position model, and no
annotation-as-data story. We already built that contract internally
(`private/frontend/frontend_contract.md` → "Reader"); this plan **extracts it into a standalone,
catalogue-free package** with reference renderers per platform.

---

## 1. Goals / non-goals

**Goals**
- One **contract** (byte source, Locator/position, navigation, search, decoration seam, capabilities)
  that is identical across web, iOS, Android.
- **Headless core** + **per-platform engines** (pdf.js/epub.js on web; PDFKit + epub.js-webview on
  iOS; PdfRenderer/pdfium + WebView/Readium on Android).
- Integrator supplies **adapters** (their storage, their sync backend, their nav) — the SDK ships
  the engine, not opinions about where bytes or state live. The storage backend NEVER leaks to the
  client (the "kDrive doesn't leak" seam is a first-class SDK property).
- Reusable by anyone: a generic reading app, a research tool, a docs viewer — not catalogue-coupled.

**Non-goals (base SDK)**
- Annotations, handwriting, recognition, export → the **extension** plan.
- Account/auth, DRM/LCP, a catalogue/metadata model, a UI design system → integrator's job.
- Audiobooks/comics initially (Locator model leaves room; not in v1).

---

## 2. What we already have to extract (don't rebuild)

| Existing piece (in `catalogue-webui/.../static/reader/` + backend) | Becomes |
|---|---|
| `reader-core.js` (L1 engine: tools, toggle, palm rejection, panels, download overlay) | SDK **core** (strip catalogue/Flask assumptions) |
| `overlay.js` (L2: `createOverlay(format, ctx)` → `PdfOverlay`/`EpubOverlay`) | SDK **decoration seam** (moves to the extension) |
| host-supplied `annotations`/`bookmarks`/`position` adapters (`reader.html`) | SDK **port interfaces** |
| `/sync/reader` + `ReaderStateStore` ABC | reference **AnnotationStore/ReadingStore** port + a sample server (extension) |
| neutral ink `[x,y,pressure]` + vendored `perfect-freehand` | canonical **ink model + renderer** (extension) |
| range-aware serving, 1MB chunking, on-demand local copy | guidance for the **Source** port (HTTP-range reference adapter) |

The internal architecture is already layered (L1–L4) and import-disciplined (`lint-imports`). The SDK
work is mostly **decoupling** (remove every catalogue/Flask reference) + **documenting the contract**
+ **reference adapters**, not green-field.

---

## 3. Architecture — packages & ports

```
octavo-core        (platform-neutral spec + core logic; ZERO platform/host deps)
  ├─ Publication model         {format, metadata?, resources}
  ├─ Locator                   {format-tagged location + progression + textContext}   ← from reader-contract
  ├─ Navigator (interface)     open · goTo(locator) · next/prev · search(q)->[Locator] · outline
  ├─ Source (PORT)             read(range)->bytes · length · contentType   (integrator supplies)
  ├─ ReadingStore (PORT)       getPosition · setPosition · recent(n)        (integrator supplies)
  ├─ Capabilities              canAnnotate · canExport · …                  (integrator supplies)
  └─ DecorationHost (interface, EXTENSION plugs here)                       ← from reader-contract

Note: `Locator` + `Decoration` + `DecorationHost` now live in the shared **`reader-contract`**
package (`reader-contract/README.md`), which both octavo and postilla depend on — so the annotation
extension no longer depends on octavo. octavo re-exports them (`@_exported import ReaderContract`), so
`import Octavo` still surfaces these types unchanged.

octavo-web         (Navigator impls: PdfJsNavigator, EpubJsNavigator)   — reference renderer
octavo-swift       (Navigator impls: PdfKitNavigator, EpubWebNavigator) — iOS/macOS
octavo-kotlin      (Navigator impls: PdfiumNavigator, EpubWebNavigator) — Android/Boox

octavo-adapters    (optional reference adapters: HttpRangeSource, FileSource, MemoryReadingStore)
octavo-examples    (minimal embed per platform; the integration tutorial as runnable code)
```

**Locator** (the heart of the contract — Readium-inspired, but ours):
```
{ publicationId, format:'pdf'|'epub',
  locations: { page? , cfi? , progression(0..1) , position? },
  text?: { before, highlight, after } }   // textContext makes a locator survive re-pagination
```
`goTo(locator)`, `search(q) -> [Locator]`, and the decoration seam all speak Locator. PDF uses
`page`(+rect via decorations); EPUB uses `cfi`; `progression` is the universal fallback. This is the
single unit of "where am I / take me here / anchor a mark here," and it is what makes a search hit,
a bookmark, an LLM citation, and a highlight all interoperable.

**Source port** — `read(range)`/`length`/`contentType`. Reference `HttpRangeSource` (range requests,
the 1MB-chunk lesson baked in) and `FileSource` (native disk — the zero-transfer fast path). The
engine never learns whether bytes came from disk, HTTP, or cloud.

**Navigator** is the only thing that's per-platform; everything above it is shared/ported. Format
dispatch (`pdf` vs `epub`) is the SDK's, not the integrator's.

---

## 4. Public API (what an integrator writes)

```ts
const reader = await Octavo.open({
  source: new HttpRangeSource(url),        // or FileSource(path) on native
  format: 'pdf',                            // or sniffed
  readingStore: myReadingStore,             // PORT — persist position / recent
  capabilities: { canAnnotate: true },
  host: pdfContainerElement,                // web; a UIView on iOS; etc.
})
reader.onLocationChanged(loc => myReadingStore.setPosition(id, loc))
await reader.goTo({ locations:{ page: 42 } })
const hits = await reader.search('dependent origination')   // -> [Locator]
reader.outline()                                            // TOC
// decorations/annotations come from the extension:
reader.decorations.apply([...])            // DecorationHost seam
```

Same shape in Swift/Kotlin (idiomatic). The contract doc is the source of truth; each language
binding is a thin, tested mirror.

---

## 5. Extraction / decoupling strategy (the boundary)

The whole credibility of "generic" is **zero catalogue imports**. Enforce it mechanically from day
one, the way the repo already enforces layering:

1. Create `octavo-core` as a package with an **import-linter contract**: `octavo-core` and `octavo-web`
   may import nothing from `catalogue.*` / Flask. CI fails on violation (`lint-imports` already in use).
2. Move `reader-core.js` behind that boundary; replace every catalogue/Flask touchpoint with a port.
3. The **catalogue becomes the first consumer**: it implements the ports (`HttpRangeSource` over
   `/holding/<id>/file`, a `ReadingStore` over `/sync/reader`) — proving the SDK is genuinely
   host-free by *using it as an outsider would*.
4. Only after the API is stable (v0.x → v1) **extract to a standalone public repo**; the catalogue
   then depends on the published package. (See §9 repo decision.)

This "decouple in place, extract at v1" path means you get the clean package immediately and defer
the multi-repo + OSS-maintainer cost until the API has earned it.

### 5.1 The boundary contract (drop-in for the first `octavo` commit)

`octavo/` lands as a **top-level workspace member** (sibling of `catalogue-webui`), package + import
root **`octavo`** — deliberately **outside** the `catalogue.` namespace (it's the one package that
must never be catalogue-coupled). Add to the uv workspace `members`, then add this to the **root
`pyproject.toml`** so the rule is machine-enforced from commit #1:

```toml
[tool.importlinter]
root_packages = ["catalogue", "octavo"]   # was: root_package = "catalogue"
include_external_packages = true          # so the external forbidden_modules below are detected

[[tool.importlinter.contracts]]
name = "octavo (reader SDK) depends on no host"
type = "forbidden"
source_modules = ["octavo"]
forbidden_modules = [
    "catalogue",   # the ENTIRE catalogue namespace — the SDK must be host-free
    "flask",       # no web-framework coupling
    "wsgidav",
]
# allow_indirect_imports defaults to false → even a TRANSITIVE reach into catalogue fails CI.
```

The reverse direction is already legal under the existing "Layered architecture" contract: the app
sits on top, so `catalogue-webui` importing `octavo` is fine; `octavo` importing `catalogue` is what
this forbids. This governs the SDK's **Python** surface (the host-neutral sync store + export logic).

**The JS engine needs its own guard** — `lint-imports` is Python-only and can't see `reader-core`/
`overlay`. Two cheap complementary checks in the `octavo` JS package:

```jsonc
// .eslintrc — the engine stays host-free
"rules": {
  "no-restricted-imports": ["error", { "patterns": ["**/catalogue/**", "**/webui/**"] }],
  "no-restricted-globals": ["error", "LibraryCore", "LibraryUI"]
}
```
plus a one-line CI grep test asserting the engine files contain **no hardcoded catalogue routes**
(`/api/v1`, `/holding/`, `/sync/reader`) — those must arrive through injected adapters, never as
literals in the engine. Together (import-linter + eslint + grep), the no-host rule is enforced on
both language surfaces from the first commit.

---

## 6. Distribution, licensing, docs

- **Packages:** npm (`@octavo/core`, `@octavo/web`), SwiftPM (`Octavo`), Maven (`io.octavo:octavo`).
- **License:** **Apache-2.0** (permissive + explicit patent grant → integration-friendly; avoids the
  AGPL/GPL adoption-blocker that sinks KOReader/Sioyek as embeddables). Vendored deps must be
  license-compatible (pdf.js Apache-2 ✓, epub.js permissive ✓, perfect-freehand MIT ✓; **Readium is
  BSD-3 ✓** if used for Android EPUB).
- **Docs:** the contract spec (port it from `frontend_contract.md`), a per-platform quickstart, and a
  runnable `octavo-examples` embed. Docs are load-bearing for an SDK — budget them as a feature.
- **Versioning:** semver; the **contract** (Locator, ports) is the stability surface — engines can
  change underneath without a major bump.

---

## 7. Test strategy (mirror the repo's unit / system / perf split)

- **Unit (per binding):** Locator serialization round-trips; format sniffing; `search` returns valid
  Locators; `goTo(locator)` ⇄ `onLocationChanged` consistency; Source range math; ReadingStore
  contract conformance (parametrize over in-memory + a real adapter, like `test_reader_state.py`).
- **Cross-binding parity:** a **golden corpus** (sample PDF + EPUB) with expected
  search-result Locators and TOC; every binding (web/Swift/Kotlin) must produce the same Locators
  for the same query → guarantees a bookmark/citation made on one platform resolves on another.
- **System:** reference example app opens each corpus book from `HttpRangeSource` and `FileSource`,
  navigates, searches, restores position across reopen (Playwright on web; XCUITest on iOS).
- **Performance:** first-page time (range-stream vs whole-file), Nth-page seek, search latency,
  memory under a large PDF — reuse the ceilings already in `test_reader_perf.py`; add a
  `FileSource` zero-transfer baseline to quantify the native disk-access win.

---

## 8. Milestones

1. **M1 — contract + core extraction.** `octavo-core` (Locator, ports, Navigator interface) with the
   import boundary; spec doc. No new features — just decoupled `reader-core`.
2. **M2 — web reference.** `octavo-web` (PdfJs + EpubJs Navigators) + `octavo-examples` web; catalogue
   repoints its web reader onto the package (dogfood; also retires duplicate code).
3. **M3 — iOS binding.** `octavo-swift` (PdfKitNavigator = the fast disk-access path; EpubWebNavigator =
   epub.js in WKWebView). Cross-binding parity tests green.
4. **M4 — Android binding** (optional, Boox). `octavo-kotlin`.
5. **M5 — extract + publish** at v1 (see annotations extension shipping in parallel as `@octavo/postilla`).

---

## 9. Repo placement — recommendation

**Develop in this monorepo now behind a hard no-catalogue-import boundary; extract to a standalone
public repo at the v1 API freeze.** Rationale:

- A public SDK MUST NOT depend on the catalogue; the only way to *prove* that is a mechanical import
  boundary, which the repo already supports (`lint-imports`). Enforcing it in-place gives you the
  decoupled package's benefits (clean seams, testability, the catalogue as a real consumer)
  immediately — with no multi-repo tax.
- Co-developing with a real first consumer (the catalogue) is the fastest way to find leaky
  abstractions. Premature extraction strands you maintaining two repos for an API that's still moving.
- **Extract at v1** because that's when outside contributors, independent issue tracking, separate
  CI/release, and a clean git history (no catalogue secrets/history bleed) start to pay off. Use
  `git filter-repo` to carry just the package history out.

**Honest caveat (no sugarcoating):** "publicly available" roughly *doubles* the cost over "clean
internal package" — API stability promises, docs, examples, semver discipline, issue/PR triage,
governance — and the payoff depends on adoption you can't control. The decoupled package is worth it
**regardless** (it's what makes your own native iOS reader cheap). Publishing is worth it **only if**
you'll fund the maintainer burden or genuinely want external users. So: get the package now, decide
on publishing at the v1 gate — the plan is identical up to that point either way.

---

## 10. Open decisions

- **Platform scope for v1:** web + iOS (recommended) vs. also Android/Boox now. Android doubles
  binding work but is the only path to a native Boox reader.
- **EPUB engine on native:** epub.js-in-webview (keeps parity with the existing CFI pipeline) vs.
  Readium (more native, BSD-3) — see the annotations plan; leaning epub.js-reuse.
- **Brand/name** — LOCKED: `octavo` (engine) + `postilla` (extension); npm scope `@octavo/*`
  (`@octavo/core`, `@octavo/web`, `@octavo/postilla`), PyPI `octavo` / `octavo-postilla`, Swift
  `Octavo` / `Postilla`. `octavo` is free on PyPI; `@octavo` npm scope to be claimed.
- **Publish-or-stay-internal** — decide at the v1 gate, not now.
