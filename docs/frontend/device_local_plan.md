# Device-local library — build plan (2026-06-17)

*A device-local version of the catalogue so the **Mac server being up is not required**
for the two things that matter on the go: (1) check whether a text is in the library,
and (2) open it. Tolerates stale data; refreshes and syncs when the Mac is reachable.
This is the current client-side plan (it supersedes an earlier read-only-snapshot /
phone-add sketch); the server-side FK method and integrity net are unchanged.*

---

## Decisions (locked with the user, 2026-06-17)

| Question | Decision |
|---|---|
| Client form | **PWA now, native later** — reuse the existing responsive web UI + a service worker; revisit native when in-app reading / reading-state matters. |
| Offline scope | **Read + append-only capture** — lookup & open offline; add-a-book/scan-ISBN queued and flushed up later. |
| Open while Mac down | **Internet OK** — open the file straight from kDrive (no Mac); true airplane-mode (pinned offline) is a later upgrade. |
| Relationship to live app | **One app, graceful degradation** — same client uses the live Mac server when reachable, falls back to the local replica when not. |

## Goal / non-goals

- **Goal:** offline **lookup** (is *X* in my library, with enough to identify it) and
  offline **open** (stream the file from kDrive), plus an **append-only capture queue**
  that flushes to the Mac when it's next up. One PWA that degrades gracefully.
- **Non-goals (now):** offline *edits to existing catalogue records* (needs real merge —
  deferred; edits happen online, "Tier 1"); annotations / reading-position sync (set
  aside); true airplane-mode open (needs kDrive offline-pinning — later); native app.

## What we already have to build on

- **`signature.py`** — text content-fingerprint, stable across re-encode/annotation; the
  stable identity the replica keys on.
- **`bookfile.py` (`BookFileService`)** — resolves a holding to readable bytes
  (local / kDrive-via-WebDAV). The Mac-up open path and the kDrive-locator source.
- **`reconcile` / `relink.py`** — sweep + move/byte-rewrite healing; capture flush feeds
  the same ingest.
- **`capture_staging`** — append-only capture rows with **ISBN idempotency index** →
  conflict-free outbox flush target. `/capture` endpoint exists (`web.py:2084`).
- **`/find` + `SEARCH_TYPES`**, the shared `_book_browser.html` widget — the UI we reuse
  online; the client-side lookup mirrors its fields.
- **`/holding/<id>/file`** streaming; **`before_request`** FK+(future)auth gate; Tailscale
  reach (§9.3) so "Mac reachable" holds away from home.

---

## Architecture

```
  ONLINE (Mac reachable, via LAN or Tailscale)
     PWA ──HTTP──▶ live Flask app ──▶ catalogue.db        full UI, live edits
       │  └─ on each online load: pull /replica.json ─▶ cache in IndexedDB
       │  └─ open: stream /holding/<id>/file
       │  └─ flush capture outbox ─▶ POST /capture (ISBN-idempotent)
       ▼
  OFFLINE (Mac down)                       internet still OK
     PWA (service worker) ──▶ IndexedDB replica            lookup (stale, "as of …")
       └─ open: kDrive open-URL from replica ─▶ kDrive serves bytes (no Mac)
       └─ capture: write to IndexedDB outbox (Background Sync flushes later)
```

Two one-way streams, **no conflict resolution**: replica **down** (read-only), capture
outbox **up** (append-only, ISBN-deduped).

---

## Abstraction boundaries (required — nothing crosses either seam)

Consistent with the codebase's existing layering (`relink.MoveResolver`, the
provider-agnostic `webdav.WebDAVClient`/`Mount`, the framework-agnostic
`bookfile.BookFileService`). Two seams keep **storage** and **client** independently
swappable:

```
   clients            │        server               │      storage
 ┌──────────────┐     │   ┌──────────────────┐      │   ┌────────────────┐
 │ PWA  (now)   │     │   │  Sync API        │      │   │  StoragePort   │
 │ native (later)│ ─▶ │   │  contract        │  ─▶  │   │   (ABC)        │
 └──────────────┘     │   │  /api/v1/*       │      │   ├────────────────┤
   any client ────────┘   └──────────────────┘      │   │ KDriveProvider │
                          server knows neither      │   │  (or other)    │
        ▲ CLIENT seam:    the client type nor       ▲   └────────────────┘
          the API contract  the storage provider      PROVIDER seam: StoragePort
```

Provider neutrality survives the client seam because the server resolves the open link
**before** it crosses: a replica row carries an **opaque `open_url`**, so the client opens
a URL without ever knowing it's kDrive.

### Provider seam — `StoragePort` (swap kDrive for anything)

An ABC the server depends on; `KDriveProvider` is the first implementation. Generalizes
today's provider-agnostic WebDAV/cloudsync code into one named port:

- `fetch_bytes(local_path) -> bytes | None`   — real bytes (today: `webdav`)
- `is_placeholder(local_path) -> bool`        — online-only stub? (today: `cloudsync`)
- `open_link(local_path) -> str | None`       — **NEW**: a client-openable, *Mac-independent* URL

Config-driven, **never hardcoded**: base URL + drive id from `.kdrive_settings`; per-file
ids from a swappable `FileIdResolver` (REST-API / PROPFIND / xattr implementations — TBD,
see §D). A provider that can't mint a Mac-independent link returns `None` → the client
falls back to streaming. Swapping to another cloud = one new `StoragePort` impl, zero
client changes.

### Client seam — the Sync API contract (swap PWA for native)

A stable, **versioned** HTTP contract every client consumes; the server branches on **no**
client type:

- `GET  /api/v1/replica`  — the thin export (rows carry the opaque `open_url`)
- `GET  /api/v1/health`   — reachability probe
- `POST /api/v1/capture`  — append-only capture (metadata + bytes); reuses the existing
  `capture_staging` handler under the versioned path

Each client is an independent implementation of this contract; **all** client-specific code
(service worker, IndexedDB, JS today; Swift/Files-provider later) lives client-side only.
Adding the native app = a second consumer of `/api/v1/*`, zero server changes.

---

## Components

### A. Thin replica (Mac export)

- **`catalogue/export_replica.py`** → builds a denormalized, read-only export. **One row
  per openable holding**, only lookup+open fields — drops the FRBR graph, authority,
  review/OCR/undo state. Stays small (low MB for thousands of books).
- **Row fields:** `holding_id, edition_id, title, subtitle, authors[], translators[],
  isbn[], subject, volume, format, signature(text wire), open_url, exported_at`.
  `open_url` is **opaque** — produced by `StoragePort.open_link`, the client never parses it.
  Include folded/alias title forms for fuzzy match.
- **Transport (graceful-degradation native to a PWA):** served at **`GET /api/v1/replica`**
  (also `ETag`/`If-None-Match` for cheap refresh). The PWA pulls it on every online load
  and stores it in **IndexedDB**; offline it reads the cached copy. *Also* write a copy to
  kDrive `_replica/` (backup + the transport a future native client would read).
- **Refresh cadence:** regenerated by the existing nightly/sweep timer **and** on demand;
  `exported_at` drives the staleness banner.

### B. PWA shell + reachability state machine

- **`manifest.webmanifest`** + icons + `display: standalone` → "Add to Home Screen".
- **Service worker:** precache the app shell (HTML/CSS/JS); runtime-cache static assets.
- **Reachability probe:** lightweight `GET /api/v1/health` with a short timeout decides mode:
  - `ONLINE` → proxy/navigate to the live app (full features), refresh replica, flush outbox.
  - `OFFLINE` (Mac unreachable) → render the **local lookup** view from IndexedDB; opens use
    kDrive URLs; captures go to the outbox.
- Single UI; an offline banner + disabled "edit/curate" affordances mark the degraded mode.

### C. Local lookup (offline)

- Client-side search over the IndexedDB replica: NFC-folded substring match on
  title/subtitle/authors/translators/ISBN/subject (mirror `SEARCH_TYPES` semantics; an
  ISBN-prefixed query → exact ISBN). Small enough for in-memory filter; add a prebuilt
  token index only if it gets slow.
- Result shows enough to **identify** the text (title + vol + authors + subject + ISBN) and
  an **Open** action.

### D. File open — the linchpin

- **Mac up:** stream `/holding/<id>/file` (existing `BookFileService`; pulls kDrive
  placeholders via WebDAV). Unchanged.
- **Mac down, internet OK:** open the opaque **`open_url`** baked into the replica — for the
  kDrive provider, a deep link that opens the file under the user's account **with no Mac
  involved**. This is the load-bearing requirement of the whole plan. The client treats it
  as an opaque URL (provider seam); only `KDriveProvider.open_link` knows its shape.
  - `KDriveProvider.open_link` builds it from config (base + drive id, never hardcoded) +
    per-file ids from a swappable **`FileIdResolver`**. **Verified portable template**
    (same PDF on Mac *and* phone, 2026-06-17): `{base}/all/kdrive/app/drive/{drive_id}/
    files/{dir_id}/preview/{kind}/{file_id}` — the `all` context is device-portable. The
    resolver yields the file's kDrive `file_id` **and its parent `dir_id`** (both from one
    lookup). kDrive doesn't expose these via a local xattr (verified 2026-06-17), so the
    resolver impl is the **kDrive REST API** or a **WebDAV PROPFIND** — reusing existing creds.
  - **✅ Verified by hand (2026-06-17), Mac server off:** (1) Files app → kDrive → PDF opens
    in QuickLook (native path); (2) an account-private kDrive deep link opens the file in
    mobile Safari (PWA-handoff path). Both work with no Mac. R1 closed; no spike needed.
    Native (Files provider) remains the smoother inline-open path for the later upgrade.

### E. Append-only capture outbox (offline write)

- Offline capture (ISBN scan, photo, note) → **IndexedDB outbox** record.
- **Flush** via the Background Sync API (and an on-load flush) **when the Mac is reachable**
  → `POST /capture` with `source='pwa'`. Bytes (photos) ride the same POST; the existing
  **ISBN idempotency index** dedupes, so re-flush is safe.
- Because it's append-only (new `capture_staging` rows, never edits), **merge = insert** —
  no conflict logic. Desk-reconcile against swept files as today.
- Outbox holds until the Mac is up (capture isn't urgent); show pending-count in the UI.

### F. Staleness UX

- Banner: **"Local copy — as of {exported_at}"** whenever serving from the replica.
- Per-row subtlety: a holding present in the replica but not yet re-swept (or vice-versa) is
  possible within one refresh interval — acceptable; the banner makes it honest.

---

## Mac-side work items

0. ✅ **`StoragePort` ABC + `KDriveProvider`** (`storage.py`) — wraps `webdav`/`cloudsync`
   behind the port, adds `locator()` (relpath for native + opaque `open_url` for web) and a
   swappable **`FileIdResolver`** (`NullFileIdResolver` default). Provider seam done.
1. ✅ `export_replica.py` (one row/edition, `search_text`, per-holding `StorageRef`) +
   **`GET /api/v1/replica`** (content-ETag → 304). Read-only. 8 tests; 1586 suite green.
   ◻ Remaining: nightly/sweep hook + kDrive `_replica/` copy.
2. ◻ Productionize `FileIdResolver` (REST-API / PROPFIND → `(file_id, dir_id, kind)`;
   backfill `open_url`). The only piece still returning null. `kind` derives from format.
3. ✅ **`GET /api/v1/health`** (cheap, no DB write).
4. ✅ **`POST /api/v1/capture`** (`source='pwa'`, reuses idempotent `_capture_one_json`).
   ◻ No-ISBN free-note / photo path deferred (ISBN-scan path shipped).
5. (Carried from §9.3) Tailscale + minimal auth on write routes so "reachable" works away from home and the outbox flush is authenticated.

## Device / PWA work items   (✅ shipped 2026-06-17 — `templates/app.html`, `static/pwa/*`)

6. ✅ `manifest.webmanifest` + SVG icon + `sw.js` (served root-scope, shell precache,
   replica network-first w/ cache fallback). Route `/app`, `/sw.js`, `/manifest.webmanifest`.
7. ✅ Reachability via `/api/v1/health` probe → live/offline status pill + "as of" stamp.
8. ✅ IndexedDB replica store (`kv`) + pull-on-online (ETag) + folded local lookup.
9. ✅ Open dispatch: opaque `open_url` when present, else stream `/holding/<id>/file`
   (labelled "via Mac"). open_url stays null until the resolver lands (item 2).
10. ✅ Capture outbox (IndexedDB `outbox`) + flush to `/api/v1/capture` on online + pending count.
    ◻ Background Sync (iOS Safari limited → flush is on-open/online-event; acceptable).

### Remaining before it's fully usable on the phone
- **HTTPS for the phone** — the service worker needs a secure context; over LAN-HTTP it
  won't register. Enable Tailscale HTTPS certs (admin console) + `tailscale serve 8000` →
  `https://your-mac.tailnet.ts.net/app`. (LAN-HTTP works on the Mac via
  localhost for dev.)
- **Offline OPEN (item 2)** — needs the kDrive file id. WebDAV PROPFIND does NOT expose it
  (verified); requires the kDrive REST API + a `KDRIVE_API_TOKEN`. Until then open falls
  back to streaming. (A native app avoids this via `relpath` + Files provider.)
- **Nightly export hook + kDrive `_replica/` copy** (item 1 tail) — ops/launchd.

---

## Phased build order (regression test at each step — per project rule)

- **P0 — Spike the linchpin (§D). ✅ DONE 2026-06-17.** Verified by hand with the Mac off:
  both the Files-app open and the Safari kDrive-link open work. PWA-first is safe to proceed.
- **P1 — Replica export.** `export_replica.py` + `/replica.json`. **Tests:** row-count ==
  openable holdings; required fields non-null; size bound; `open_url` well-formed; ETag 304
  on no-change.
- **P2 — PWA shell + reachability + local lookup.** Install, offline app-shell, IndexedDB
  pull, search. **Tests:** black-box — given a fixture replica, queries return the right
  holdings offline; ONLINE↔OFFLINE switch on a stubbed `/health`.
- **P3 — Open.** Up = stream; down = kDrive URL. **Tests:** open dispatch picks the right
  path per mode; URL points at the right file.
- **P4 — Capture outbox.** Offline queue + flush + idempotency. **Tests:** queued capture
  survives reload; flush POSTs once; **double-flush is idempotent** (ISBN + uuid); desk
  reconcile sees one row.
- **P5 — Polish.** Staleness banner, pending-count, auth on flush, Tailscale verified away.

System/black-box tests for the major pieces (replica contract, reachability machine, outbox
flush) per the project's "end-to-end system tests for major changes" rule.

## Open questions / risks

- **R1 — kDrive Mac-independent open link. ✅ RESOLVED 2026-06-17** by manual test (Mac
  off): both Files-app and Safari-link open succeed. No longer a risk.
- **R2 — PWA on iOS limits.** Background Sync / service-worker behavior on iOS Safari is
  weaker than Android; flush may be "on next open" rather than truly background. Acceptable
  (capture isn't urgent); note it.
- **R3 — Replica freshness vs sweep lag.** A just-added book may not be in the replica for
  up to one refresh interval. Acceptable; banner is honest.
- **R4 — Cold device offline.** Cleared cache while Mac is down = no data until next online
  load. Acceptable edge.
- **R5 — No-ISBN capture idempotency.** Needs a client-generated uuid so re-flush dedupes
  without an ISBN.

## Verification

- **Offline lookup:** Mac off, Wi-Fi/cell on → open PWA, search a known title → it appears
  with id/vol/authors/ISBN, "as of …" banner shown.
- **Offline open:** from that result, **Open** → kDrive serves the file, Mac never contacted.
- **Online full:** Mac up → same PWA shows the full live UI, edits work.
- **Capture round-trip:** Mac off → add a book (ISBN+photo) → queued; bring Mac up → flushes
  once → one `capture_staging` row; re-trigger flush → still one row (idempotent).
- **Staleness:** banner date matches the last `exported_at`.
- **Tests green:** `pytest` + new black-box tests for P1–P4.

## Future (explicitly deferred)

- **Native iOS app** — best offline open (Files provider/QuickLook), embedded reader; the
  R1 fallback and the home for reading-state.
- **True airplane-mode open** — pin the kDrive Books folder offline on the device.
- **Reading state** (position/bookmarks) + **PDF outline authoring** — separate per-device
  sync over kDrive; see the design notes. Out of scope here.

---

## §12 — PWA enhancement: web-parity look + full offline + in-app reader (2026-06-17)

*Turns the minimal `/app` prototype into an offline-first reading app that looks like the
web version. Endpoints now live in `catalogue/webui/routes/api.py`; assets in
`webui/static/pwa/` + `templates/app.html`. `pdf.js`, `epub.js`, `jszip` are already
vendored under `webui/static/vendor/`.*

### Decisions (locked with the user, 2026-06-17)

| Question | Decision |
|---|---|
| Scope | **Reading-focused now** (home shelves · search/browse · in-app reader · capture), **architected to expand** to full parity (review/curation/edit, online-only) later. |
| Reader | **Full** — paging, TOC/outline nav, in-book text search, remembers your place. |
| Covers | **Eager** — prefetch every cover so home/browse looks complete offline (tens of MB, within iOS limits). |
| Reading state | **Remember position + bookmarks**, per-device, stored locally now; structured for later per-device kDrive sync (§11). |
| (from earlier) | **Enhance the PWA**, not native; **on-demand** book-file caching. |

### Core architectural choice — client-rendered look-alike, not server HTML

The web app is **server-rendered** (Jinja); those pages need the Mac. To both *look like
it* **and** *work offline*, the PWA stays a **client-rendered** app that renders the same
look from the **IndexedDB replica** + cached covers. It **reuses the web app's theme**
(the CSS custom-properties from `_base.html` — `--bg/--fg/--surface/--muted/…`) so it's
visually the web app, not a separate skin. Expansion to "full parity" later = add
online-only screens that fall back to live server pages when reachable (graceful
degradation), never offline.

### Caching model (three tiers, matched to iOS limits)

| Tier | What | Strategy | Why |
|---|---|---|---|
| Shell + reader libs | app.js, sw.js, `vendor/{pdf,pdf.worker,epub,jszip}.min.js`, theme CSS | **precache** on SW install | instant offline launch + reader |
| Metadata + covers | replica JSON, every `/edition/<id>/cover.jpg` | **eager** (replica network-first; covers prefetched after load, SW-cached) | home/browse look complete offline |
| Book files (PDF/EPUB) | the actual bytes | **on-demand** — cached in Cache API the first time a book is opened | iOS can't hold ~5 GB; cache what you read |

Call `navigator.storage.persist()` to reduce eviction; show per-book "cached ✓ / not
cached" and a "remove download" action so the user controls the on-device footprint.

### Backend additions (`routes/api.py`, `export_replica.py`)

1. **Replica gains reader handles per holding**: `format` (pdf/epub, already), and a
   `cover` ref per edition (URL `/edition/<id>/cover.jpg` + a `has_cover` flag so the
   client only prefetches real covers, else uses the SVG text-fallback). No new heavy data.
2. **File bytes for the reader**: reuse `GET /holding/<id>/file` (range-capable). First
   open fetches the whole file → Cache API (needed for offline anyway); pdf.js/epub.js
   read from the cached blob.
3. *(unchanged)* `open_url` stays the opaque external-open option; the **in-app reader is
   now the default** open action, with "open in kDrive" demoted to a secondary control.

### Client components / screens (all offline-capable unless noted)

1. **Home** — Netflix-style shelves with cover art (mirrors `library.home_shelves`: subject
   shelves + "Recently opened/added"), rendered from the replica.
2. **Search / Browse** — folded local search (already built) → cover-grid / list cards in
   the web look.
3. **Book detail** — cover, full metadata (authors/translators/subjects/ISBNs/works), and
   **Read** (in-app) + secondary "open in kDrive".
4. **Reader** — `pdf.js` (PDF) / `epub.js` (EPUB): paging, **TOC/outline** (pdf.js outline
   API / epub.js nav), **in-book search**, restores **position**, **bookmarks**.
5. **Capture** — existing ISBN outbox.
   *Online-only (queue/refuse offline):* capture-send flush, any future edit screens.

### Reading state (local now, sync-ready)

IndexedDB store `reading`, keyed by `edition_id` (+ holding): `{ location, bookmarks[],
updated_at }`. Position = last-write-wins; bookmarks = union set — exactly the merge rules
in §11, so a later kDrive per-device-file sync drops in without reshaping the data.

### Convertibility to a native iOS app (REQUIRED — guarantee, not afterthought)

> Goal: ship the PWA now such that it can become an iOS app later with the **backend
> reused unchanged** and most/all client work reused too.

**The backend is already 100% shared.** Everything server-side lives behind the versioned
`/api/v1/*` contract + the `StoragePort` provider seam; the server never branches on client
type. A native app is "a second consumer" — zero server changes. Concretely reused as-is:
`/api/v1/{health,replica,capture}`, `GET /holding/<id>/file`, `GET /edition/<id>/cover.jpg`,
the `StorageRef` (its `relpath` is already the native Files-provider open path), and the
Tailscale-HTTPS infra (native iOS ATS needs HTTPS too — same setup).

**Two convert paths, both kept cheap by the rules below:**

- **Path A — wrap (max reuse, recommended).** Wrap the *same* PWA (HTML/JS) in a native
  shell (Capacitor / WKWebView). The UI and most logic are reused verbatim; native
  **plugins** swap in the capabilities iOS Safari lacks — chiefly **unlimited offline file
  storage** (native filesystem), which *removes the "can't cache the whole library" limit*
  we hit, plus native file access. This is the literal "convert the PWA into an app."
- **Path B — native rewrite.** SwiftUI + PDFKit/QuickLook reimplement the *client* only,
  reusing the backend + the contracts below. More work, most-native feel.

**Rules to build by NOW so either path is cheap (enforced through P6–P10):**

1. **No business logic in the client.** All "smarts" are server-side and shipped in the
   replica — folded `search_text`, cover selection (`has_cover`), reader handles, subjects.
   The client only renders + matches. (A native client gets identical behavior for free.)
2. **Platform behind thin adapters** (the only files a wrap/rewrite touches):
   - `FileStore` — cache/read book-file bytes. Web impl = Cache API; native impl = native
     filesystem. **This adapter is what lets Path A escape the iOS storage cap.**
   - `ReadingStore` — position/bookmarks. Web = IndexedDB; native = Core Data/SQLite.
   - `Opener` — in-app reader vs external open (`open_url` web link / `relpath` Files provider).
   - `Net` — `fetch` wrapper (works in a WKWebView wrap unchanged).
   Keep these as small, documented interfaces in the JS; the rest of the app depends only on them.
3. **Contracts are client-neutral and written down**, so a native app implements against a
   spec, never by reading the PWA's JS:
   - the `/api/v1/*` + replica **JSON schema** (already versioned);
   - the **reading-state record + future kDrive sync-file format** (§11) — fixed now even
     though local-only, so PWA and native interoperate byte-for-byte.
4. **Auth stays Bearer-token** on `/api/v1/*` (works identically for `fetch` and native
   `URLSession`) — never cookie/session-only.

*Deliverable:* a short `private/frontend/api_contract.md` (the endpoints + replica schema +
reading-state/sync format) maintained alongside the code — the single source both clients
implement against.

### Phased build (regression test each step)

- **P6 — Web-parity shell.** Reuse theme tokens; render Home + Search/Browse + Book detail
  from a fixture replica. *Tests:* given a fixture replica, the home shelves + a search
  render the expected cards (DOM assertions via a headless check or template-free JS unit).
- **P7 — Eager covers.** Replica carries `cover`/`has_cover`; client prefetches; SW caches
  `/edition/*/cover.jpg` cache-first. *Tests:* replica cover fields; SW cache rule.
- **P8 — In-app reader.** Open → fetch+cache file → render with pdf.js/epub.js; TOC +
  in-book search; second open works offline (from Cache API). *Tests:* reader module picks
  pdf vs epub by format; cache-hit path needs no network (mocked).
- **P9 — Reading state.** Persist + restore position; add/remove bookmarks. *Tests:* store
  round-trip; LWW on position; bookmark union.
- **P10 — Polish.** `storage.persist()`, per-book cached indicator + "remove download",
  offline banners, "remove all downloads". *Tests:* eviction-safe behavior, indicator state.

### Risks / constraints

- **iOS evicts web storage** — cached *files* may be dropped under pressure; mitigate with
  `persist()`, on-demand (not eager) files, and a visible cached-state UI. (The full-library
  guarantee remains native-only — the documented "native later" trigger.)
- **HTTPS still required** for the service worker on the phone → Tailscale `serve` + certs
  (carried from §9.3 / above). LAN-HTTP works for online use but not install/offline.
- **pdf.js memory on iOS** — render page-by-page; never hold the whole doc rasterized.
- **EPUB** needs `epub.js`+`jszip` reading from the cached blob (both vendored).

### Verification

- Mac off, phone (HTTPS): home shelves + covers render; search works; open a **previously
  read** PDF and EPUB → reader opens **offline** with TOC + last position; bookmark persists;
  a never-opened book shows "needs Mac/internet". Capture queues; flushes on reconnect.
