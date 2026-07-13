# Device-local API contract (`/api/v1/*`)

*The single source both clients — the PWA today, a native iOS app later — implement
against. Keep this in sync with `catalogue/webui/routes/api.py` and
`catalogue/domain/export_replica.py`. The server branches on **no** client type; a native
app is "a second consumer." See `device_local_plan.md` §12.*

Versioning: the URL carries the major version (`/api/v1`). The replica body also carries
`schema_version`; bump it only on a **breaking** change. Additive fields don't bump it —
clients must ignore unknown fields.

---

## Endpoints

### `GET /api/v1/health`
Reachability probe. No DB write. → `200 {"ok": true, "service": "catalogue", "api": 1}`.
A client uses reachability to choose live vs offline mode.

### `GET /api/v1/replica`
The thin, read-only device dataset (below). `ETag` is over **content** (excludes
`exported_at`), so an unchanged library returns `304` to `If-None-Match`. Clients cache
the body and serve lookup from it offline.

### `POST /api/v1/capture`
Append-only capture (the offline outbox flushes here when reachable). Body:
`{ "isbn": "9780…", "source": "pwa", "scanned_at": "ISO-8601", "uuid": "client-id" }`.
Idempotent on ISBN (`capture_staging_raw_isbn_uq`) → re-flush is safe.
→ `201 {status:"ok", staging_id, isbn, duplicate, …}` · `422 {status:"invalid", reason}`.

### Wishlist — `GET/POST/PATCH/DELETE /api/v1/wishlist` (§14.10)
Books wanted but not yet owned — a single shared list, kept OUT of the catalogue graph until
acquired. Resolution reuses the ISBN/CIP/intake services (`services/wishlist_resolve.py`).
- `GET /api/v1/wishlist` → `{ "items": [WishlistItem], "schema": 1 }`. `ETag` over content
  (like the replica) → clients cache it for offline display.
- `POST /api/v1/wishlist` — body carries exactly one input form: `{ "isbn": "…" }` |
  `{ "title": "…", "author": "…" }` | `{ "cip_text": "…" }` (+ optional `source`). Resolves and
  persists; → `201 { "item": WishlistItem, "verdict": {…} }`. An input the resolver can't identify
  is still stored with `status` `unresolved`/`ambiguous` (never dropped). Requires edit capability.
- `PATCH /api/v1/wishlist/<id>` — `{ notes?, priority?, status?, pick?: N, confirm_owned?: <edition_id>,
  decline_suspected?: true, rev? }`. `pick` resolves an *ambiguous* item from its Nth candidate;
  `confirm_owned` marks a *suspected* item as the edition you already own (→ acquired);
  `decline_suspected` keeps it on the wishlist. `409` on a stale `rev`.
- `DELETE /api/v1/wishlist/<id>` — soft-delete (tombstone). Optional `?rev=`.

Add/dedupe semantics: `POST` returns `{ item, verdict, added, owned, duplicate }`. An already-owned
book is NOT added (`owned`); a book already on the list returns the existing item (`duplicate`); a
partial catalogue match (similar title+author, e.g. a different-ISBN printing) is added as
`status:"suspected"` with the candidate editions for the operator to confirm. The shared
`LibraryCore.wishlistRequest` (intent→request) and `wishlistAddMessage` (response→text) are the one
source every client uses — renderers never hardcode these.

`WishlistItem`: `{ id, source, status, raw_isbn?, raw_title?, raw_author?, title?, subtitle?,
authors[], publisher?, year?, isbn?, ol_work_key?, lccn?, cover_url?, candidates[],
matched_edition_id?, priority?, notes?, added_at, updated_at?, acquired_at?, rev }`.
`status` ∈ `unresolved | resolved | ambiguous | suspected | owned | acquired`. Composed into a screen
by the shared `LibraryCore.wishlistVM` (web/PWA/iOS).

### Capture intent (§14.10)
`POST /api/v1/capture` and `POST /capture/cip` accept an optional `"intent": "wishlist"` — routes
the scan into the wishlist instead of `capture_staging`, returning `{ intent:"wishlist",
wishlist_item, … }`. Absent/`"catalogue"` = the unchanged default. The catalogue path also returns
`fulfilled_wishlist_item` (the acquisition loop: a positive verdict flips a matching wishlist item
to `acquired`). Capture contract version is now **4**.

### `GET /holding/<id>/file`
Raw book bytes (range-capable). The reader fetches this once and caches it (FileStore) for
offline reading. Not a `/api/v1` route but part of the contract.

### `GET /edition/<id>/cover.jpg`
Cover image (real cover or SVG fallback — always 200). Referenced opaquely via the
replica's `cover_url`; the client never constructs it.

---

## Replica schema (`schema_version: 2`)

```jsonc
{
  "schema_version": 2,
  "exported_at": "2026-06-17T12:00:00+00:00",   // UTC; excluded from the ETag
  "provider": "kdrive",                          // StoragePort name, or null
  "count": 388,
  "editions": [ EditionRow, … ]                  // one row per edition
}
```

`EditionRow`:
```jsonc
{
  "edition_id": 1,
  "title": "…", "subtitle": "… | null", "volume": "… | null",
  "publisher": "… | null", "year": 2024,
  "authors": ["…"], "translators": ["…"],
  "isbns": ["9780…"], "subjects": ["…"], "work_titles": ["…"],
  "cover_url": "/edition/1/cover.jpg",           // opaque; fetch as-is
  "search_text": "folded blob of all of the above",  // client matches on THIS (NFKD+casefold)
  "holdings": [ Holding, … ]
}
```

`Holding`:
```jsonc
{
  "holding_id": 1,
  "format": "electronic | …",          // form_type code, for display
  "kind": "pdf | epub | null",         // file extension — the READER dispatch key
  "has_file": true,
  "storage": {                                   // null when no provider covers the file
    "provider": "kdrive",
    "relpath": "Common documents/…/Book.pdf",    // NATIVE open via Files provider (no id)
    "open_url": "https://…/preview/pdf/279 | null"  // OPAQUE web/PWA handoff; null until resolver
  }
}
```

Client rules (enforce on both PWA and native):
- **Match on `search_text`** (fold the query the same way: NFKD + casefold + collapse
  spaces). Don't re-derive searchability client-side.
- **Treat `cover_url` / `open_url` as opaque.** Never parse or construct them.
- **Open precedence:** in-app reader (cache `/holding/<id>/file`) → else `open_url` (web)
  → else `relpath` + Files provider (native). The provider is never named in client logic.

---

## Reading-state record (client-local now; kDrive per-device-file sync later, §11)

Keyed by `edition_id`. Identical shape on every client so a future sync interoperates
byte-for-byte. Merge rules: **position = last-write-wins** (by `updated_at`); **bookmarks
= union**.

```jsonc
{
  "edition_id": 1,
  "location": { "kind": "pdf", "page": 42 } /* or */ { "kind": "epub", "cfi": "epubcfi(…)" },  // null until first read
  "bookmarks": [ { "id": "uuid", "label": "…", "loc": "…", "created_at": "ISO" } ],
  "opened_at": "ISO | null",      // drives the "Recently opened" shelf
  "updated_at": "ISO"
}
```

Future sync file (one per device): `_reading/<device>.json` = `{ device, items: [record…] }`.
A reader folds over all device files at load (LWW position, union bookmarks). Not built yet.

---

## Auth (when added)

`Authorization: Bearer <token>` on `/api/v1/*` — works identically for browser `fetch` and
native `URLSession`. Never cookie/session-only (a native app can't share the browser
session).
