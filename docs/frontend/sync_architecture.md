# Frontend ↔ backend sync architecture

How every client (web, PWA, native iOS, and — later — Android) stays fresh against the server, as one
model. Companion to `frontend_contract.md` (the shared Tier-2 contract) and `api_contract.md`.

## 0. The one-paragraph version

The server is **pull-only** — it never pushes; a change is discovered by asking. Client state falls into
**three sync shapes** (below). A single shared **update model** drives them: a golden-locked Tier-2
`syncVM` decides *what the freshness UI says*, and a per-surface **`SyncEngine`** (transport-agnostic —
pull now, push later) *revalidates registered resources* on a fixed set of triggers and bumps a
**data revision** so open screens repaint. "Refresh" is therefore one concept everywhere; a new surface
is "implement the ports + render `syncVM`".

## 1. The three sync shapes

| Shape | What | Endpoint(s) | Cursor | Direction | Conflict |
|---|---|---|---|---|---|
| **A — ETag snapshot read cache** | catalogue metadata (replica), starred, wishlist, content-index | `GET /api/v1/replica`, `/starred`, `/wishlist`, `/content-index` | **ETag** (`sha256` of content, excl. `exported_at`) | server→client | none (read-only) / optimistic write returns fresh state |
| **B — rev-cursor delta (mergeable)** | reader annotations + bookmarks | `GET/POST /sync/reader` | monotonic **`rev`** (`?since=<rev>`) | bidirectional | LWW + `rev` tiebreaker, keyed by uuid |
| **C — fire-and-forget LWW** | reading position | `GET/POST /holding/<id>/position` | none | bidirectional | last-write-wins (advisory resume, no merge) |

Everything else is either a **read cache** (covers/spine/preview/file bytes — ETag/immutable, cached on
the client) or **device-local, not synced** (open reading-sessions, prefs). Auth is a signed cookie with
silent 401 re-auth.

**A change flips its cursor.** Adding an edition or a series member changes the replica body → new ETag →
the next `If-None-Match` GET returns `200 + full snapshot` instead of `304`. A highlight bumps the reader
`rev`. That is the entire "how does the client know" story — there is no push (yet; see §6).

## 2. The shared update model

Two reused repo patterns: the **golden-locked Tier-2 VM** (like `readerChromeVM`) for the decidable part,
and the **strategy-ABC + protocol-agnostic executor** (like `ServerEndpoint`) for the transport.

### 2.1 Tier-2 `syncVM` (golden-locked, pure)
`SyncState → SyncStatusVM`. One pure function, reimplemented per language, locked byte-equal by
`goldens.json` (`testSyncVMParity`).
- **Input `SyncState`**: `{ online, syncing, lastError?, exportedAt?, lastCheckedAt?, pendingWrites }`.
- **Output `SyncStatusVM`**: `{ state: live|syncing|offline|error, label, tone: ok|warn|error|muted,
  detail?, canPull }`.
- Wording: `Live` / `Syncing…` / `Offline · <YYYY-MM-DD>` / `Sync failed`; `canPull = online && !syncing`.
- Source of truth: `catalogue-webui/.../static/js/library-core.js` (`syncVM`) ↔
  `catalogue-app/ios/.../CatalogueCore/Sync.swift`.

### 2.2 `SyncEngine` (executor) + `SyncTransport` (strategy) + `SyncResource` (registry)
Shared *shape* per language, native *implementation* (touches disk/IndexedDB/URLSession/timers).
- **`SyncResource`** — one syncable thing (`replica`, `starred`, `wishlist`, `reader:<hid>`): owns its
  cursor + `revalidate()` (Shape A = conditional GET; Shape B = `?since=rev` + merge).
- **`SyncTransport`** — *how* changes are learned: pull now (`conditionalGet`/`delta`); a `subscribe()`
  **push seam** defaulting to no-op.
- **`SyncEngine`** — `refresh(reason, ids?)` revalidates targets, single-flights concurrent triggers,
  publishes `SyncState` (→ `syncVM`) and a monotonic **`dataRevision`** that screens key on to repaint.
  `reason ∈ {launch, appear, foreground, online, manual, pushed}`.

iOS: `catalogue-app/.../CatalogueData/SyncEngine.swift`. Web/PWA: the same states/methods realized in
`app.js` (`syncNow`, `refresh`, `setStatus`).

## 3. Triggers — when each surface revalidates

| Trigger | web | PWA | iOS |
|---|---|---|---|
| launch / page load | server-render + shelves fetch | boot (if online) | `AppModel` init + first screen `.task` |
| screen appear | per-navigation (live) | `route()` | `.task` on each catalogue screen |
| foreground / tab-return | `visibilitychange` refetch of Home shelves | `visibilitychange` + `focus` → `syncNow` | `scenePhase == .active` → `refresh(.foreground)` |
| network regained | (browser) | `online` event | `NWPathMonitor` → `refresh(.online)` |
| manual | (reload) | pull-to-refresh gesture | `.refreshable` pull-to-refresh on every screen |
| unchanged cost | `304` (cheap) | `304` | `304` |

Reading position rides along: iOS mirrors it to the server on background/close/poll; web/PWA already
`POST` it on page turns (`sendBeacon`).

## 4. Reader sync (Shape B) in detail

- **Push (write)** is immediate on each edit (`POST /sync/reader` with `{ops:[…]}`), optimistically
  rendered so a mark shows instantly (even offline). PWA queues failed pushes to an IndexedDB outbox and
  flushes on `online`; **iOS** renders optimistically but does not yet persist an offline outbox across
  restarts (tracked).
- **Pull (read)** is **incremental**: track the `rev` cursor and `GET /sync/reader?holding=<id>&since=<rev>`
  to fetch only deltas + tombstones, merged **in place** (never repositions the page — marks are an
  **overlay**, not baked into the file, so a delta is a few hundred bytes and never re-downloads the PDF).
- **Cross-device freshness**: iOS re-pulls on foreground + a 45 s poll while open. Web/PWA re-pull via a
  **⟳ Refresh button** in the reader chrome (pull-to-refresh would fight the reading scroll) and on
  tab-return. (`reader-core.js reloadMarks()` → `overlay.load()` + `repaint()`.)
- **Flatten/export** (`GET /holding/<id>/annotated.pdf` copy, `POST …/annotated` in-place) is a separate
  on-demand path, decoupled from sync.

## 5. Cross-language parity

The decidable Tier-2 pieces (`syncVM`, `readerChromeVM`, the VMs) are generated from the real
`library-core.js` into `Tests/CatalogueCoreTests/Goldens/goldens.json` (`Tools/gen_goldens.mjs`) and the
Swift port is asserted byte-equal (`ViewModelParityTests`). Android adds a Kotlin port that passes the
same goldens. The **impure** engine/transport/resource *shape* is mirrored by hand (like `DataPort`).

## 6. The push seam (future, not built)

`SyncTransport.subscribe(onChange)` is a no-op today. A future server `GET /api/v1/events` (SSE) emits a
`resource-changed` ping (resource id + new cursor); an `EventStreamTransport` implements `subscribe` to
call `engine.refresh(id, .pushed)`. Dropping it in needs **zero** changes to `SyncEngine`, `syncVM`, or any
surface's UI — pull silently upgrades to push.

## 7. Deferred / known gaps

- iOS durable offline reader-push outbox (in-memory only this session).
- PWA/web reader while-open *automatic* overlay refresh beyond the button + tab-return.
- SSE push transport (§6).
- iOS reader Share/Export affordance (server `annotated.pdf` → share sheet — approach recorded in
  `catalogue-app/docs/app-TODO.md`).
- The web/PWA reader's ⟳ button is hand-built chrome, not yet part of the shared `readerChromeVM` spec
  (iOS auto-refreshes instead); fold it into the VM when convenient.

## 8. Where the pieces live

- Shared VM: `catalogue-webui/.../static/js/library-core.js` (`syncVM`), `CatalogueCore/Sync.swift`.
- iOS engine: `CatalogueData/SyncEngine.swift`; wired in `CatalogueUI/AppModel.swift`, `RootShell.swift`,
  `Components.swift` (`SyncStatusPill`, `catalogueRefreshable`), `Screens.swift`.
- iOS reader: `CatalogueReader/ReaderView.swift` (delta pull + resume), `PositionSync.swift`, `ReaderSync`,
  `BookmarkSync`.
- PWA: `static/pwa/app.js` (`syncNow`, status chip, pull-to-refresh), `static/pwa/reader.js` (⟳ button),
  `static/reader/reader-core.js` (`reloadMarks`). Web: `templates/home.html`, `templates/reader.html`.
- Server: `routes/api.py` (replica/starred/wishlist ETag), `routes/reader_sync.py` (`/sync/reader`),
  `routes/bookfiles.py` (position, files, covers, annotated export).
