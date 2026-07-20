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
| **B — rev-cursor delta (mergeable)** | reader annotations + bookmarks + authored PDF outlines | `GET/POST /sync/reader` | monotonic **`rev`** (`?since=<rev>`) | bidirectional | LWW + `rev` tiebreaker, keyed by uuid (an outline is one wholesale row per copy, keyed by a stable per-copy id) |
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

### 2.3 App-version handshake (stale-server guard)

A long-running server keeps serving the code and cached templates it loaded at startup, even after the
files on disk change. Browsers, meanwhile, refetch changed static JS/CSS. So a server that hasn't been
restarted after an edit can hand a client a mismatched pair — an old cached template wired to fresh JS —
and the page silently breaks. (This is how a scanned-PDF reader once hung at "Downloading… X/X": the
process served the old range-mode template against the new whole-file engine.)

The fix is a version handshake, built the same way as the reader_sync contract. The server advertises the
build it's running and whether it's stale; every client compares and reacts:

- **`app_build`** — identifies the build the server process is running. It's stamped into every page as
  `window.APP_BUILD` and returned by `GET /version` and `GET /api/v1/health`. A client compares the build
  it loaded with against the live one; a difference means the server was restarted/redeployed, so the open
  page should reload.
- **`server_stale`** — true when the running process is behind its own code on disk (a restart is pending).
  While this is true the web app **refuses to serve HTML pages** (503 interstitial) so a stale server can't
  hand a client a broken page; the API/health/version/static routes stay open so clients can still detect
  the condition and recover. `CATALOGUE_ALLOW_STALE=1` opts out for live-editing dev.

Clients act on the same two fields: web pages + the reader show a reload/restart banner
(`static/js/app-version.js`), the PWA folds the check into its `/api/v1/health` probe, and iOS surfaces it
in Settings (`AppBuildContract` / `AppBuildWatcher`, the native mirror of `app-version.js`).

"Requires a restart" is scoped to Python code: templates are served fresh (`TEMPLATES_AUTO_RELOAD`) and
static assets are cache-busted (`static_v`), so changing those needs no restart and doesn't flip
`server_stale`; only a code change does — which is exactly "a feature that requires a restart", and it
bumps the build automatically because the build id is derived from the files.

#### Technical details

- **Build id** = a short SHA-256 over each tracked file's path + `st_mtime_ns`, sorted. Captured at process
  start (`catalogue.webui.app_version.BuildStamp`, the `DEFAULT` instance). It is a fingerprint, not a
  monotonic counter — the handshake only needs "same or different", and a content-derived stamp updates
  automatically with no manual bump or persistence. Per-process/per-machine (mtime-based), so it's only ever
  compared against values from the *same* running server.
- **Scope = every catalogue package this process imported**, not just webui. `loaded_catalogue_roots()` reads
  `sys.modules` for `catalogue.*` and fingerprints each package's directory (`web.py` calls `finalize()` once
  all eager imports are done). So a restart-requiring change in `db_store`, `services`, `contracts`, … — or a
  contract descriptor JSON read into a module global at import — flips `server_stale`, not only a change under
  `webui/`. Packages the server never imports (populate, test_kit) contribute nothing, so they don't cause
  false blocks.
- **Staleness** = the restart-tracked fingerprint (`.py` + `.json`) recomputed now ≠ the one captured at
  startup. Recomputed at most every ~1.5s (a `before_request` on every hit must stay cheap; a full-namespace
  scan is ~3 ms). `__pycache__` + vendored assets are skipped; templates (`.html`) and static JS/CSS are
  excluded because they update live (auto-reload / `static_v`).
- **Wire**: `{ "app_build": str, "server_stale": bool }`, additive on `/api/v1/health` (older clients ignore
  it) and standalone on `/version`.
- **Client rule** (identical in JS `AppVersion.classify` and Swift `AppBuildContract.classify`): `server_stale`
  wins → restart; else a live build ≠ the baseline → reload; else ok. Missing fields (an older server) → ok,
  so it never false-alarms.
- **Where it lives**: server `app_version.py` + the staleness gate in `web.py` + `/version` in `routes/api.py`;
  clients `static/js/app-version.js` (web/PWA/reader) and `CatalogueCore/AppBuildContract.swift` (iOS);
  interstitial `templates/_stale.html`. Guards: `tests/test_app_version.py`, `tests/system/test_version_handshake*.py`,
  `Tests/CatalogueCoreTests/AppBuildContractTests.swift`.

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
  flushes on `online`; **iOS** now does the same — `LocalAnnotationStore` persists every op to a device
  file *before* the network and keeps it in an outbox, so a mark made offline survives relaunch and
  flushes on the next reachable pull. The outbox depth is surfaced through the `OutboxProbe` port so the
  freshness chip shows "N unsynced" (offline) / "N syncing" (online).
- **Pull (read)** is **incremental**: track the `rev` cursor and `GET /sync/reader?holding=<id>&since=<rev>`
  to fetch only deltas + tombstones, merged **in place** (never repositions the page — marks are an
  **overlay**, not baked into the file, so a delta is a few hundred bytes and never re-downloads the PDF).
- **Cross-device freshness**: iOS re-pulls on foreground + a 45 s poll while open. Web/PWA re-pull via a
  **⟳ Refresh button** in the reader chrome (pull-to-refresh would fight the reading scroll) and on
  tab-return. (`reader-core.js reloadMarks()` → `overlay.load()` + `repaint()`.)
- **Writes *into* the PDF** — flattening annotations (`GET /holding/<id>/annotated.pdf` copy,
  `POST …/annotated` in-place) and **authoring the PDF outline** (`GET /holding/<id>/outlined.pdf` copy,
  `POST …/outlined` in-place) — are a separate on-demand path, decoupled from sync. They share one server
  mechanism (`pdf_mutation.write_pdf` + `PdfMutation` implementations) so the copy/in-place envelope is
  written once. The authored outline itself is an **overlay synced through this Shape-B path** (a
  `reader_state.Outline` row, `outline` op/record in the wire contract v2), so authoring is offline +
  multi-device like bookmarks; the file bytes are only rewritten on the explicit bake. See
  `reader_architecture.md` → "Persistent PDF writes".

### 4.1 The wire contract (versioned)

The `/sync/reader` shape is a **published, versioned contract** — `catalogue.reader_sync` — not just a
docstring, so the three clients (web reader, PWA, and the native app's `postilla` `AnnotationStore`
adapter) target one artifact instead of each re-deriving the shape. It mirrors the external read-contract
pattern:

- `db_store/reader_sync_contract.json` — the machine-readable, language-neutral spec (endpoints, the
  `bookmark`/`annotation`/`outline` pull-row records, the push ops, and the cursor/auth/conflict
  semantics), each record `source`-linked to the `reader_state` dataclass it comes from. **v2** adds the
  `outline` record + op (older clients ignore the new `outlines` array; the client check is `version >=
  built-for`, so a v1 client keeps working against a v2 server).
- Every `/sync/reader` response carries `contract_version`, and `GET /sync/reader/contract` serves the full
  descriptor — so a client asserts the live version ≥ what it was built for (a few lines it owns, no
  catalogue import).
- `db_store/reader_sync_contract.py`'s `verify()` is the provider-side truthfulness check: every field the
  descriptor declares must be a real `reader_state` dataclass field / `apply_*` parameter, so a future edit
  that drops a field fails loudly (`tests/test_reader_sync_contract.py`) instead of silently breaking
  clients.

Layering note: `postilla`'s generic `AnnotationStore` port (in the sibling `octavo-postilla` repo) knows
nothing about this contract — the catalogue-specific wire binding lives only in the app's `ReaderSync`
adapter. The contract is what the adapter targets; octavo-postilla stays reusable.

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

- ~~iOS durable offline reader-push outbox~~ — **done**: `LocalAnnotationStore` is a persist-before-network
  outbox that survives relaunch and self-heals on reconnect; its depth is reported through `OutboxProbe`.
- iOS offline **search-inside-books** (full-text): `ContentIndex` is still a `NoContentIndex` stub. Browse/
  search of catalogue metadata and opening a previously-read book already work offline; searching *words
  inside* books offline needs a local FTS index (whole-library pack, or index-only-what's-cached — the
  cheaper option). Deferred.
- ~~iOS offline bookmark outbox~~ — **done**: `LocalBookmarkStore` now has the same persist-before-network
  outbox as annotations (survives relaunch, flushes on reconnect), drains on the reader's open/foreground/
  poll cadence, and reports its depth through `OutboxProbe` (folded into the same "N unsynced" count).
- PWA/web reader while-open *automatic* overlay refresh beyond the button + tab-return.
- SSE push transport (§6).
- iOS reader Share/Export affordance (server `annotated.pdf` → share sheet — approach recorded in
  `catalogue-app/docs/app-TODO.md`).
- The web/PWA reader's ⟳ button is hand-built chrome, not yet part of the shared `readerChromeVM` spec
  (iOS auto-refreshes instead); fold it into the VM when convenient.

## 8. Where the pieces live

- Wire contract (Shape B): `db-store/.../db_store/reader_sync_contract.json` + `reader_sync_contract.py`
  (`verify()`), served/advertised by `catalogue-webui/.../routes/reader_sync.py`; guarded by
  `tests/test_reader_sync_contract.py`.
- Shared VM: `catalogue-webui/.../static/js/library-core.js` (`syncVM`), `CatalogueCore/Sync.swift`.
- iOS engine: `CatalogueData/SyncEngine.swift`; wired in `CatalogueUI/AppModel.swift`, `RootShell.swift`,
  `Components.swift` (`SyncStatusPill`, `catalogueRefreshable`), `Screens.swift`.
- iOS reader: `CatalogueReader/ReaderView.swift` (delta pull + resume), `PositionSync.swift`, `ReaderSync`,
  `BookmarkSync`; authored outline via `OutlineSync` + `LocalOutlineStore` (durable outbox) + the shared
  `editOutline` control in `readerChromeVM`.
- PWA: `static/pwa/app.js` (`syncNow`, status chip, pull-to-refresh), `static/pwa/reader.js` (⟳ button),
  `static/reader/reader-core.js` (`reloadMarks`). Web: `templates/home.html`, `templates/reader.html`.
- Server: `routes/api.py` (replica/starred/wishlist ETag), `routes/reader_sync.py` (`/sync/reader`),
  `routes/bookfiles.py` (position, files, covers, annotated export).
