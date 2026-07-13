# Shared frontend contract (web · PWA · native)

The five reader-facing features — **Search**, **Browse**, **Content search**, **Book detail**,
**Settings** — plus the **app navigation** chrome are built ONCE as a platform-neutral abstraction
and rendered by per-toolkit UIs. The web and
PWA share the JavaScript renderer; a native app (iOS/SwiftUI, Android) reimplements only the
rendering, reusing everything below it. This document is the spec a new UI implements.

The abstraction has three tiers. **Nothing in Tier 1 or Tier 2 references the DOM, `window`,
`localStorage`, or any toolkit** — that is what makes it portable.

```
 Tier 1  Data contract (language-agnostic JSON over HTTP)      ← server, /api/v1/*
 Tier 2  Presenter / view-models + adapter protocol (pure)     ← library-core.js  (reimplement in Swift)
 Tier 3  UI renderer (per toolkit)                             ← library-ui-dom.js (web+PWA) | SwiftUI | …
```

A UI provides a small **platform adapter** (data fetching + navigation + prefs + online state);
the shared presenter turns adapter data into neutral **view-models**; the renderer draws them.

---

## Tier 1 — Data contract (`/api/v1/*`)

Stable JSON every client consumes. The web fetches these live; the PWA serves Search/Browse from
its cached replica and only hits `/content` live (the in-book text isn't in the replica). Source:
`catalogue/webui/routes/api.py`, reusing `catalogue/domain/{search,library}.py`.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/replica` | Whole offline dataset (one row per edition + folded `search_text`). ETag/304. `catalogue/domain/export_replica.py`. |
| `GET /api/v1/find?q=&only=` | **Browse**: `{q, groups:[{key,label,label_plural,count,hits:[{id,label,sublabel,url}]}]}`. |
| `GET /api/v1/library?q=&work_title=&person=` | **Search**: `{q, rows:[{id,title,subtitle,done,holding_id,has_file,file_ext}]}`. No query → newest-first browse. |
| `GET /api/v1/content?q=` | **Content search**: `{q, books:[{eid,title,authors,snippets}], available}`. Snippets carry `[match]` highlight markers + `…` elision. |
| `GET /api/v1/edition/<id>` | **Book detail** (read-only): one edition, the SAME per-edition shape as a replica row (`display_title`, authors, translators, subjects, isbns, publisher, year, holdings, cover_url/spine_url). 404 if gone. The PWA reads this shape from its cached replica instead of refetching. |
| `GET /api/v1/subjects?kind=&q=` | **Subject hierarchy** as a pre-order forest: `{kind, tree:[{id,name,leaf_label,depth,parent_id,has_children,is_protected,n_books_direct,n_books_total}]}`. `kind=topic` (default) or `series`; `q` filters to matching names + their ancestors. Drives the fold/unfold tree. `catalogue/domain/subject_tree.py`. |
| `GET /api/v1/subject/<id>` | **Subject browse page** (canonical subject target): `{subject:{id,name,kind,leaf_label}, crumbs, children, books, n_books}`. `books` is DESCENDANT-INCLUSIVE (a topic rolls up its sub-topics; a series lists its volumes by `edition.volume`); `children` are the immediate sub-subjects to drill into. 404 if gone. |
| `GET /api/v1/content-index` | Offline **Content search** bundle: a standalone SQLite file (external-content FTS5, same tokenizer as the live DB), gzipped, ETag/304. `catalogue/domain/export_content_index.py`. |
| `GET /find/suggest?q=` | Typeahead completions: `{matches:[{type,label,sublabel,url}]}`. |
| `GET /api/v1/health` · `POST /api/v1/capture` | Reachability probe · append-only ISBN capture. |

`url` values in `find`/`suggest` hits are WEB URLs; a non-web UI maps them to its own navigation
via the neutral `ref` (below) — `LibraryCore.refFromUrl` does this parse.

---

## Tier 2 — Presenter + adapter protocol (`library-core.js` → `window.LibraryCore`)

Pure functions; no DOM. **Reimplement this tier in the native language** (it's small: query
handling, replica-group derivation, offline/live selection, shape normalization).

### Adapter protocol (each platform supplies one)

```
adapter = {
  data: {
    search(q)        -> Promise<[Card]>            // Card = {eid, title, by, cover_url, spine_url}
    browse(q, only)  -> Promise<{groups:[Group]}>  // Tier-1 /find shape; hits carry url OR ref
    content(q)       -> Promise<{books:[{eid,title,authors,snippets}], available}>
    detail(eid)      -> Promise<EditionRow|null>   // read-only Book detail (replica-row shape)
    suggest(q)       -> Promise<[{type,label,sublabel, url|ref}]>
  },
  nav:   { hrefFor(ref) -> navigation target,       // ref → URL (web) | hash (PWA) | route (native)
           readHref(eid, holding)? -> URL },         // optional: where a detail "Read" link points
  openBook(eid, holding)?,                          // optional: opens the in-app reader (PWA/native);
                                                    //   when ABSENT the detail renderer links to nav.readHref
  prefs: { get(key)->string|null, set(key,val), remove(key) },
  isOffline() -> bool,
}
```

### Neutral nav reference (`ref`)

Toolkit-agnostic pointer the renderer never interprets — it only asks `nav.hrefFor(ref)`:

```
{kind:'edition', id}  | {kind:'work', id} | {kind:'person', id}
{kind:'subject', id}  | {kind:'url', url}
```

Web maps edition→`/library?eid=`, work→`/work/<id>`, subject→`/subject/<id>` (the canonical
descendant-inclusive browse page), etc. PWA maps edition→`#/book/<id>` and subject→`#/subject/<id>`;
work/person still return `null` (no standalone page yet). Native maps to its own routes.

### View-models (the renderer's only input)

`searchVM(adapter,q)`, `browseVM(adapter,q,only)`, `contentVM(adapter,q)`, `detailVM(adapter,eid)`,
`settingsVM(adapter)`, `navVM(items, activeKey)` return plain structs: `{kind, q, …, empty, error?, offline?}` —
Search→`cards`, Browse→`groups` (each hit `{type,label,sublabel,ref}`), Content→`books` +
`available`, Detail→`{title,by,authors,translators,subjects,isbns,publisher,year,workTitles,
coverUrl,holdings}` (or `missing:true`), Settings→`theme/shelfArt` + option lists. Offline/error
states are encoded as fields, not exceptions, so every UI handles them uniformly.

### Command path — write actions are Tier 2 too (NOT just reads)

A feature's *write* side belongs in this tier exactly like its read VM. For any interactive feature
expose THREE shared functions (`library-core.js` + native port, golden-locked):

```
xVM(data)             -> the view-model (what to show)            // read path
xRequest(action,args) -> {method, path, body}                    // user intent → backend request
xMessage(resp)        -> user-facing string                      // backend response → one wording
```

Tier-3 renderers are **transport + paint only**: they EXECUTE the descriptor (`fetch` / `Net` /
`URLSession`) and PAINT the VM/message. A renderer must contain **zero `/api/v1/...` endpoint
literals and zero response-message strings** — those live once in Tier 2, so web/PWA/native issue
identical requests and show identical wording. Enforced by `tests/test_frontend_command_parity.py`
(greps renderers for endpoint/message literals) + goldens on `xRequest`/`xMessage`.

**Reference implementation — the wishlist** (`/wishlist`, the §wishlist feature): `wishlistVM`,
`wishlistRequest(action, {body|id|index|editionId})`, `wishlistAddMessage(resp)` in `LibraryCore`
(JS) + `LibraryCore` (Swift). `wishlistRequest` actions: `list · add · remove · pick · confirm ·
decline`. The iOS adapter executes them via `CatalogueAPI.wishlistExec`; web via `fetch`; PWA via
`Net` (+ an offline outbox for `add`). See `docs/design/wishlist_model.md`.

---

## Tier 3 — UI renderer (per toolkit)

Dumb: no fetching, no business logic — it draws a view-model and emits the actions the
view-model declares. Web + PWA share `library-ui-dom.js` (`window.LibraryUI`: `search/browse/
content/detail/settings/nav`, navigation via real `<a href=hrefFor(ref)>`). A native app implements
the same screens against the same view-models in its own toolkit. The **detail** renderer turns
`holdings` into Read controls: a `<button>` calling `adapter.openBook(eid, holding)` when present
(in-app reader), else an `<a href=nav.readHref(eid, holding)>` (web).

---

## Device preferences (neutral keys — mirror these for cross-device consistency)

Persisted by `adapter.prefs`; **applying** the choice is the renderer's job.

| Key | Values | Semantics |
|---|---|---|
| `theme` | `auto` \| `light` \| `dark` | `auto` ⇒ REMOVE the key (follow OS). Web/PWA set `<html data-theme>`. |
| `shelfArt` | `spine` (default) \| `cover` | Web/PWA set `<html data-shelf>`. |

Colour values are the single-source palette in `static/css/tokens.css` (web + PWA link it). A
native client encodes those same values as its colour spec. The web/PWA apply the saved theme
**before first paint** (the pre-paint script in `_base.html` / `app.html`).

---

## Offline content search (PWA; native parity)

Live `/api/v1/content` by default. Offline is behind a **generic engine seam** so the
implementation can change without touching the client: the client (`Platform.data.content` +
Settings) depends ONLY on the `window.ContentIndex` facade — `{available, status, load, enable,
disable, search}` — and the storage/query mechanism lives in a swappable **engine**
(`static/pwa/content-index.js`).

The engine is chosen at runtime by capability (`content-index.js`):
- **OpfsEngine (preferred):** streams the download straight into an OPFS file via SQLite's
  **OPFS SAH-Pool VFS** (`importDbChunked`), then queries it **page-on-demand**. Constant memory
  for both download AND query → handles a multi-hundred-MB index on a phone. Needs **no COOP/COEP
  headers and no SharedArrayBuffer** (the point of the SAH-Pool VFS); only a secure context with
  OPFS (HTTPS/localhost — where service workers already run).
- **MemEngine (fallback):** streams the download into IndexedDB, then `sqlite3_deserialize`s the
  whole DB into memory to query. Fine on desktop; can exceed a phone's memory on a big library.
  Used when OPFS is unavailable (e.g. plain-LAN http).

Both use the vendored FTS5 SQLite-WASM (`static/vendor/sqlite3.*`); they're swapped purely by
capability — **the `ContentIndex` facade and all client code stay identical** either way.

Every engine runs the **same** `match_fts` query (`catalogue/domain/search.py`) → offline results
equal online results. Query semantics: normalize (NFKD, strip marks, lowercase, collapse
whitespace), match the WHOLE query as one FTS5 phrase, ORDER BY `bm25`. A native client mirrors
this with its own SQLite (FTS5) engine behind the same facade.

---

## Reader (per-platform — a contract, NOT a shared renderer)

Opening and reading a book file is the ONE feature with no shared renderer: pdf.js/epub.js are
browser-only (web + PWA reuse `static/pwa/reader.js`); a native app uses **PDFKit + a native EPUB
renderer**. So instead of sharing UI, each platform implements its own reader against this
contract. Everything here is data + state + seams — match it and the readers stay interoperable
(notably the reading-position schema, so a future per-device sync just works).

### Opening the bytes

A book is opened from one of an edition's `holdings` (see the `/api/v1/edition` / replica shape):

```
holding = { holding_id, format, kind, has_file, storage }
```

- `has_file` — gates whether a Read control appears.
- `kind` ∈ {`pdf`, `epub`} — the **renderer dispatch key**.
- Get the bytes from the **opaque** handle `storage.open_url` (a `StorageRef`), or fall back to
  `GET /holding/<holding_id>/file`. The server resolves disk vs. cloud/WebDAV behind that URL —
  **the client never learns the storage backend** (the kDrive-doesn't-leak seam). Supports HTTP
  range requests, so a native reader can stream a large PDF.

The detail feature already routes through this: `LibraryUI.detail` renders a Read control per
holding that calls `adapter.openBook(eid, holding)` (in-app reader) or links to
`adapter.nav.readHref(eid, holding)` (web). The PWA's `Opener.open(edition, holding)` is:
record-open → try the in-app reader → on failure open `storage.open_url` externally.

### Reading-position schema (`ReadingStore`, §11 — mirror this exactly)

Per-device reading state, one record per edition. The PWA stores it in IndexedDB (`reading`
store); a native app uses its own store with the **same shape** so a later kDrive per-device sync
drops in and "Recently opened" behaves identically:

```
{ edition_id, location, bookmarks: [], opened_at, updated_at }    // ISO-8601 timestamps
```

`location` is format-tagged — the renderer reads/writes it on every page turn:

```
PDF :  { kind: 'pdf',  page }          // 1-based page number
EPUB:  { kind: 'epub', cfi }           // epub.js CFI string
```

Operations: `recordOpen(eid)` (stamps `opened_at`), `setLocation(eid, location)`, `recent(n)`
(editions by `opened_at` desc → the home **Recently opened** shelf). A reader restores `location`
on open and saves it as the reader moves.

### Offline bytes

The PWA caches opened files on demand via `FileStore` (Cache API, keyed `/__file/<holding_id>`);
the **service worker deliberately does NOT cache book bytes** — the app/user controls that (a book
can be large). A native app uses its own filesystem cache. (Cache API needs a secure context;
over plain-LAN http the PWA reads in-memory, online only.)

### Annotations + bookmarks (the sync-of-record — `GET/POST /sync/reader`)

Bookmarks and annotations (highlights, underline, strikeout, notes, **handwritten ink**) are a
**normalized, offline-first sync-of-record** — NOT embedded in the PDF/EPUB bytes. Every client
(web reader, PWA, a future native PDFKit/PencilKit reader) reads/writes the SAME records over the
same endpoint, so marks made on one device appear on the others. The store is the reader-state
PORT `catalogue.db_store.reader_state.ReaderStateStore` (SQLite adapter today; the route depends on
the abstraction, so a remote/native backend drops in).

- `GET /sync/reader?since=<rev>` → `{rev, bookmarks:[…], annotations:[…]}` — rows with `rev > since`,
  **including tombstones** (`deleted_at` set), so a deletion on one device propagates.
- `POST /sync/reader` → `{ops:[{type:'bookmark'|'annotation', id, holding_id, …fields}]}` →
  `{rev, applied:[…]}`. Each op is an idempotent **last-write-wins** upsert keyed by the client-
  generated `id` (a UUID — never a recycled int, so two offline devices can't collide). Editor-only.

**Annotation record** (only the columns a `kind` needs are set; coords are page-relative so they
survive zoom and render identically in a native renderer):

```
{ id, holding_id, kind, cfi_range, page, rect, color, note_text, ink,
  created_at, updated_at, deleted_at, rev }
```

| kind | anchoring |
|---|---|
| `highlight` / `underline` / `strikeout` | EPUB: `cfi_range` · PDF: `page` + `rect` = JSON `[[x,y,w,h],…]` (0..1 of the page) |
| `note` | PDF: `page` + `rect` = JSON `[x,y]` anchor · EPUB: `cfi_range` — plus `note_text` |
| `ink` (handwriting) | `page` (PDF page, or EPUB spine index) + `ink` = JSON `{strokes:[{points:[[x,y,pressure],…], width, color, mode}]}`, points 0..1 of the page |

Ink is stored as **raw `[x,y,pressure]` points**, not a rendered outline or a PDF.js blob — so the
web renders it with perfect-freehand and a native app renders the same record with PencilKit/PDFKit
(and ink the native app draws round-trips back). EPUB freehand ink is **best-effort**: anchored to
the spine section + page-relative coords, it may shift if the font size later changes (reflow).

**Third-party-tool portability (PDF — implemented).** `GET /holding/<id>/annotated.pdf` streams a
flattened COPY with the marks baked in (original untouched); `POST /holding/<id>/annotated` writes
them into the original in place (localhost-only). Server-side via PyMuPDF
(`catalogue.webui.annotate_export`): highlight/underline/strikeout → standard `/Highlight`
`/Underline` `/StrikeOut` annotations, note → `/Text`, **ink → a faithful filled vector path** drawn
from the same perfect-freehand outline (pressure preserved; not a separately-editable `/Ink`
object). **EPUB has no embedded-annotation standard**, so EPUB marks stay our-ecosystem-only (see
the deferred EPUB portability plan).

### Not shared

The rendering itself (canvas paging, EPUB pagination, TOC, gestures) is per-toolkit. This contract
fixes the **byte handle, format dispatch, position schema, recently-opened, annotation sync-of-
record, and the open seam** — nothing about how pages are drawn.

---

## App navigation (the floating menu — shared component)

Section navigation is ONE shared component, not per-surface chrome: a thumb-reachable button
anchored at the **bottom-right** of the viewport that expands **upward** into a **vertical** menu
(right-thumb sweep). Every surface mounts the same component so it looks and behaves identically.

- **Tier 2** `LibraryCore.navVM(items, activeKey)` → `{kind:'nav', items:[{key,label,icon,href,active}]}`.
  Each item is a **platform-supplied** `{key, label, icon, href}` — the available sections genuinely
  differ (web has Scan/Capture, and **Review only on desktop**, that the PWA lacks; **Browse is
  dropped everywhere — the Search page covers it**). `icon` is an **iOS SF Symbol name**
  (`house`, `magnifyingglass`, `doc.text`, `viewfinder`, `camera`, `checklist`, `slider.horizontal.3`);
  `href` is the surface's own target (web URL · PWA hash · native route), because nav chrome routes
  to app *sections*, not data `ref`s. The VM just marks the active item so every UI highlights the
  current section identically. `label` is icon-only chrome's accessible name (aria-label / tooltip).
- **Tier 3** `LibraryUI.nav(host, platform, opts)` where `opts = {items, activeKey, variant, label?, icon?}`.
  A Menu renderer that offers presentation **forms** but does **not** decide which to use — the
  calling implementation passes `variant`:
  - `variant:'bar'` — a **normal horizontal top menu bar** (icon + label links).
  - `variant:'fab'` (default) — a **floating bottom-right button** whose items fan out **icon-only**
    on a quarter-circle **arc** (right-thumb reach) over a dimmed scrim (tap an item / outside /
    Escape to close).

  Returns `{el, setActive(key), open, close}` so an SPA re-highlights the active section on route
  changes **without re-rendering**. The renderer holds the *look* of each form (injected styles +
  the arc `layout()`), tokenised via `tokens.css`, but **no policy** about when to use which —
  that's the implementation's call. Icons: SF Symbols can't be embedded on the web, so the renderer
  maps each **SF Symbol name → an inline stroke SVG** (`SF_ICONS`); a native client draws the same
  names with `Image(systemName:)`.

Each implementation chooses the form: the **web** (`_base.html`) picks `bar` at `min-width:768px`
and `fab` below it (and drops its own `desktopOnly` sections — Review, Scan — from the compact fab),
re-mounting when the breakpoint is crossed; the **PWA** (`app.js`) always uses `fab`; a **native**
app reimplements Tier 3 against the same `navVM`, choosing the form natural to its platform (e.g. a
tab bar) with real SF Symbols. The shared layer stays free of any hamburger-vs-bar decision.

## Section visibility — protocols

Whether a section/menu-item is shown is decided by ONE small **protocol layer**, not ad-hoc checks.
A *protocol* is a named capability gate evaluated against a runtime **context**; every section
declares one, and the built-in **`default` is always visible** (so a section that declares nothing
stays visible).

| Protocol | Visible when | Example sections |
|---|---|---|
| `default` | always | Home, Search, Text, Capture, Settings, device-preferences |
| `local` | request is from the host machine (loopback) | Library **mount roots** settings section |
| `desktop` | desktop-class client (large screen) | **Review**, **Scan** menu items |

Context keys: `local` (server: loopback `remote_addr`; client: `localhost`/`127.0.0.1` hostname),
`desktop` (client-only: viewport ≥ 768px — the server can't know screen size, so server-rendered
sections gate on `local`/`default`).

Canonical definition: `catalogue/domain/protocols.py` (`PROTOCOLS`, `is_visible`), **mirrored** in
Tier 2 `library-core.js` (`PROTOCOLS`, `protocolVisible`) so every surface gates identically:
- **Server sections** (Jinja): `{% if protocol_visible('local') %}…{% endif %}` (web.py template global).
- **Menu items** (client): each item carries `protocol`; `navVM(items, activeKey, ctx)` drops the
  ones whose protocol isn't satisfied by `ctx`. The web/PWA build `ctx` from their environment.
- A **native** client reimplements the same tiny predicate set against its own context.

(`local` is also defence-in-depth, not the only guard: the mutating mount-root routes remain
localhost-only server-side regardless of what the client renders.)

## Surfaces today

| Feature | Web | PWA | Native (spec ready) |
|---|---|---|---|
| Search   | `/find`-adjacent; the `/library` editor is kept as the power tool | Search tab | reimplement Tier 3 |
| Browse   | `/find` (client-rendered) | Browse tab | reimplement Tier 3 |
| Content  | `/search` (client-rendered, live) | Text tab (live + optional offline index) | reimplement Tier 3 |
| Book detail | editable editorial pages (`/edition/<id>`, `/library?eid=`) kept as the power tool; shared read-only component available | `#/book/<id>` (shared `LibraryUI.detail`) | reimplement Tier 3 |
| Settings | `/settings` (device prefs shared; mount-root stays server-only) | Settings tab (+ offline-content download) | reimplement Tier 3 |
| Navigation | floating menu (`LibraryUI.nav` in `_base.html`) | floating menu (`LibraryUI.nav` in `app.js`) | reimplement Tier 3 against `navVM` |
| Reader | `reader-core.js` viewer (`/edition/<id>/read`, `/holding/<id>/file`) | in-app `reader.js` on the same `reader-core` | native `octavo` reader (PDFKit + epub.js/WKWebView) |

The reader has **two rendering engines** (native `octavo` + web `reader-core`) under one shared
chrome spec (`readerChromeVM`), so its architecture is documented separately:
**[`reader_architecture.md`](./reader_architecture.md)**.
