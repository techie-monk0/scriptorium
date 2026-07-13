# Catalogue browser frontend — plan (native iOS + PWA)

An Apple Music–style browser for the book catalogue. Two possible clients (native iOS,
and a cross-platform PWA) share **one backend**. This document is split so the shared
engine is described once, then each frontend in its own section.

The mental model maps Apple Music onto the FRBR data model:

| Apple Music | Catalogue |
|---|---|
| composition | `work` |
| composer | author (`person` via `work_author`) |
| album | `edition` |
| track on an album | a work contained in an edition (`edition_work`) |
| recording (a composition performed many ways) | a work's translations across editions (`edition_translator` / `edition_work.translator_person_id`) |
| the audio file you press play on | the `holding` file (PDF/EPUB) |

**Does an off-the-shelf solution exist? No — not for this data model.** Self-hosted
readers (Kavita, Komga, Calibre-Web, BookLore, Audiobookshelf) are edition/file-centric
(one book = one file) and have no concept of a **work distinct from its
editions/translations** — exactly the distinction this app is built around. So we build
a thin client on the existing server (Flask, LAN-bound `0.0.0.0:8000`, already JSON-capable,
already streams files via `GET /holding/<hid>/file`).

**Decisions locked with the user:** favorites included in v1; annotation via **WebDAV
round-trip** to the device's reader (PDF Expert / GoodReader on iPad, NeoReader on Boox).
Native vs. PWA is still open — see §2 (shared) and the comparison in §4.

---

# Section 1 — Common: backend & engine (shared by both frontends)

The server work is **identical** for the native app and the PWA. Both reuse the same
`/api` browse JSON and the same `/dav` WebDAV mount; only the client differs.

## 1.1 Factor the holding-path helper (enables reuse)
`_holding_file_path(hid)` is currently a closure inside `create_app` (`web.py:484`). Lift
its body to a module-level `holding_file_path(db, hid)` (in `web.py` or a new
`catalogue/webui/files.py`); keep the closure as a one-line wrapper so the existing
`/holding/<hid>/file` route is unchanged. Both `/api` and `/dav` reuse it.

## 1.2 JSON browse API — new Blueprint `catalogue/webui/api.py`
Flask Blueprint at prefix `/api`, registered from `create_app`
(`app.register_blueprint(api_bp)`). Blueprint routes see `g.db` (the `Store` set in
`@app.before_request`, `web.py:158`) identically. All reads via `g.db.execute(...)`,
**reusing the SQL already in `web.py`**:

- `GET /api/editions` (list, `?letter=` A–Z + `?limit/offset`) and `GET /api/editions/<eid>`
  (album → **tracks**): reuse `_edition_card_context` SQL (`web.py:742-785`) for contained
  works + per-track translator + locator; attach holdings with `file_url: /holding/<id>/file`.
- `GET /api/works` (list) + `GET /api/works/<wid>` (titles/aliases, authors, every edition
  it appears in): reuse `work_detail` SQL (`web.py:1050-1100`), incl. `cs.work_translator`.
- `GET /api/works/<wid>/translations` — flat list of every edition/translation of a work,
  each with `holding_id` + `file_url` (shared `_work_editions(db, wid)` helper).
- `GET /api/authors` (list) + `GET /api/authors/<pid>` (split `authored_works` vs
  `translated_works`): reuse `person_detail` SQL (`web.py:1205-1230`) + `cs.person_work_ids`.
- `GET /api/search?q=` — reuse `find_books` (`search.py:205`, diacritic-folded) across
  edition/work/author + FTS snippets via the existing `SearchService`.

List envelope (consistent with `/catalogue/works?letter=`, `web.py:1925`):
`{ "letters": [...], "letter": "B", "total": N, "items": [...] }`.

## 1.3 Favorites (global / single-user — app has no auth)
- **Schema** — add to `catalogue/db/schema.sql` (tail, before `schema_meta`):
  `favorite(id PK, entity_type TEXT, entity_id INT, created_at TEXT DEFAULT datetime('now'),
  UNIQUE(entity_type, entity_id))` + `favorite_type_idx`. `entity_type` in
  `{person, work, edition}`, extensible.
- **Migration** — add the same `CREATE TABLE/INDEX IF NOT EXISTS` to `_migrate`
  (`catalogue/db/db.py`, before the `schema_version` write), matching the project's
  precedent (index creations at `db.py:494`). Confirm `assert_schema_current` (`db.py:323`)
  passes on a pre-existing DB.
- **Endpoints** in `api.py`, writes through `g.db.write(..., rows=(0,1))` + `commit()`:
  `GET /api/favorites[?type=]` (enriched with labels), `POST /api/favorites
  {entity_type, entity_id}` (validates type + existence; idempotent `INSERT OR IGNORE`;
  returns `created`), `DELETE /api/favorites/<entity_type>/<id>` (returns `removed`).

## 1.4 WebDAV annotation layer — new `catalogue/webui/dav.py`
Use **`wsgidav`** (implements PROPFIND/LOCK/PUT correctly — PDF Expert & GoodReader need
class-2 locking) mounted **in-process** alongside Flask via `DispatcherMiddleware` at
`/dav`, one server on `0.0.0.0:8000`.

- Custom `HoldingProvider(DAVProvider)` exposes a **flat virtual namespace**
  `/dav/<holding_id>.<ext>`. `get_resource_inst` parses the id, opens its own short-lived
  `connect(DB_PATH)` (DAV runs outside Flask's request context — no `g.db`), resolves the
  absolute path via `holding_file_path(db, hid)`, and serves a `FileResource` at that path.
  `PROPFIND /dav/` (Depth 1) lists holdings that have a file.
- **Why id-keyed flat namespace** (not the raw library root): `holding.file_path` is
  scattered across roots; id→path indirection keeps URLs stable across moves, makes
  traversal structurally impossible (only `/dav/<int>.<ext>` is addressable), and hides
  the layout. **A PUT replaces the file in place at `holding.file_path`** → that *is* the
  canonical copy, so annotations round-trip with no separate store.
- Read+write, lock manager enabled. Auth: anonymous on LAN (matches the app's `0.0.0.0`
  trust model); optional shared static credential via env var.
- Mount in `create_app`: `app.wsgi_app = DispatcherMiddleware(app.wsgi_app,
  {"/dav": make_dav_app(app.config["DB_PATH"])})`, **guarded** so the app still boots
  (warning) if `wsgidav` isn't installed.
- **Dependency**: add `wsgidav>=4` under `[project.optional-dependencies] ios` in
  `pyproject.toml` (lazy-imported inside `make_dav_app`), keeping the core app light.

## 1.5 Reader-app integration — verified facts (apply to both frontends)
The "play → annotate" handoff relies on these, each checked against its primary source.

### Confirmed
| # | Statement | Source |
|---|---|---|
| 1 | iPad: a link to the file → Safari renders the PDF → Share → "Open in PDF Expert / GoodReader / Books" (standard share sheet) | Apple iOS behavior |
| 2 | **GoodReader** URL scheme fetches a file from a server: prefix `g` → `ghttp://host/file` / `ghttps://`; tapping closes Safari, opens GoodReader, download starts automatically, any file type | [goodreader.com](https://www.goodreader.com/goodreader-networking-built-in-web-browser) |
| 3 | **PDF Expert** opens a *remote* file via scheme: `pdfehttp://…` (or prefix `PDFE` to the URL) → "the PDF will be sent to PDF Expert automatically." Local files: `pdfefile:///…` | [Readdle KB](https://support.readdle.com/pdfexpert/en_US/for-developers/url-schemes) |
| 4 | PDF Expert & GoodReader **sync annotated PDFs back via WebDAV** (PDF Expert: closing a WebDAV-synced doc auto-syncs back; GoodReader uploads annotated PDFs, markup saved into the PDF) | [Readdle](https://support.readdle.com/pdfexpert/en_US/synchronization/sync-your-pdf-files-between-devices), [GoodReader](https://www.goodreader.com/goodreader-networking-auto-sync) |
| 5 | Boox: downloading a PDF in the browser opens it in **NeoReader**, annotations **embed into the PDF** | [Boox help](https://help.boox.com/hc/en-us/articles/25401838386196-Reading-Data-Syncing-and-Backup) |
| 6 | Boox supports **WebDAV** third-party cloud (NextCloud, NutStore, etc.) | [Boox blog](https://shop.boox.com/blogs/news/new-feature-integrated-third-party-cloud-storage) |
| 7 | Android (Boox): a downloaded PDF can be routed to a reader via the OS "Open with" chooser | Android behavior + #5 |

### Confirmed, with a nuance
| # | Statement | Nuance | Source |
|---|---|---|---|
| 8 | Tapping a custom-scheme link in iOS Safari launches the app | iOS shows a **confirmation prompt** and requires a real user tap — one-tap-plus-confirm, not silent | [useyourloaf](https://useyourloaf.com/blog/launching-ios-apps-with-a-custom-url-scheme/) |
| 9 | "iOS PWAs can't deep-link" | Applies to deep-linking *into* an installed PWA. Launching *out* to another app's scheme is a separate mechanism that **does** work | [Progressier](https://intercom.help/progressier/en/articles/6902113-complete-guide-to-pwa-deep-links) |

### Corrections (previously overstated)
| # | Corrected statement | Source |
|---|---|---|
| 10 | The URL scheme (`ghttp://`, `pdfehttp://`) is a **one-way fetch** (open only). The **round-trip is WebDAV-only** — the reader must open the file *from the WebDAV mount*, not via the scheme, for markup to sync back | #2,#3 vs #4 |
| 11 | **Boox round-trip is not confirmed as auto-sync.** Boox embeds annotations in the PDF and can *upload* them to WebDAV, but docs describe uploading, with no documented "close = auto-sync-back." Achievable but possibly **manual / one-directional** → **verify on a real Boox device** | [Boox help](https://help.boox.com/hc/en-us/articles/25401838386196-Reading-Data-Syncing-and-Backup) |

### Unverified
- Whether external scheme-launch fires from a **home-screen-installed standalone PWA**
  (vs. plain Safari). iOS standalone mode is historically quirky here. **Guaranteed
  fallback on both platforms:** plain download + "Open in… / Open with." Treat the
  one-tap scheme as an enhancement, not load-bearing.

## 1.6 Backend implementation order & critical files
1. Factor `holding_file_path` (§1.1).
2. `api.py` browse Blueprint (§1.2) + register in `create_app`.
3. Favorites schema + `_migrate` + endpoints (§1.3); confirm `assert_schema_current`.
4. `dav.py` provider + `DispatcherMiddleware` mount (§1.4) + `pyproject.toml` extra.

- `catalogue/webui/web.py` — register Blueprint, factor helper, mount DAV (`create_app` `:120`, helper `:484`, reused SQL `:742`, `:1050`, `:1205`)
- `catalogue/webui/api.py` *(new)* — JSON browse + favorites
- `catalogue/webui/dav.py` *(new)* — wsgidav `HoldingProvider` + `make_dav_app`
- `catalogue/db/schema.sql` — `favorite` table
- `catalogue/db/db.py` — `_migrate` favorite create (`:323` governs conformance)
- `pyproject.toml` — `wsgidav` optional extra

## 1.7 Backend verification (system tests, `tests/system/`)
Use the `app_env` + `seed` fixtures from `tests/system/conftest.py`; black-box HTTP only.
- `test_api_browse.py` — seed person→work→work_alias→edition→edition_work (translator +
  locator)→holding(real file under tmp). Assert each browse endpoint's shape, A–Z
  bucketing, diacritic-insensitive search (`bodhicaryavatara` → `Bodhicaryāvatāra`), and
  that each `file_url` streams 200.
- `test_api_favorites.py` — add (201 `created:true`), idempotent re-add (200 `created:false`),
  list + `?type=` filter, delete (`removed:true`/`false`), invalid type → 400, missing
  entity → 404; assert `assert_schema_current` doesn't raise on a fresh DB.
- `test_api_favorites_migration.py` — drop `favorite` from an initialized DB, re-run
  `create_app`, assert it boots and `assert_schema_current` passes (guards the
  silent-missing-column bug class).
- `test_webdav.py` — `pytest.importorskip("wsgidav")`. `OPTIONS /dav/` advertises `DAV: 1,2`;
  `PROPFIND` lists `<id>.pdf`; `GET` returns bytes; **`PUT` new bytes and assert the on-disk
  `holding.file_path` now contains them** (the round-trip); bogus id → 404; traversal name
  cannot escape.
- Run `pytest tests/system/ -q` (whole suite; ~1000 tests) + a `curl` smoke of each `/api/...`
  route against the live DB. Wrap long runs in `caffeinate -i -s`.

---

# Section 2 — iOS native frontend (SwiftUI)

A new Xcode project (e.g. `ios/CatalogueMusic/`) modeled on Apple Music's tab +
master-detail structure. **iPad/iPhone only** (Boox is Android — see §3).

- **Networking**: a `CatalogueAPI` client (`URLSession` + `Codable`), one configurable
  base URL (the Mac's LAN address), one struct per `/api/...` shape from §1.2.
- **Tabs**: Library (editions = albums, cover-grid + A–Z scrubber), Works (compositions),
  Authors (composers), Favorites, Search.
- **Navigation**: Author → authored + translated works; Work → titles, author, translations;
  Edition → track list (contained works) with translators.
- **Favorites**: star toggle on author/work detail → `POST`/`DELETE /api/favorites`;
  Favorites tab reads `GET /api/favorites`.
- **Play / annotate**: primary "Annotate in PDF Expert / GoodReader" opens the
  **WebDAV-mounted** file so markup round-trips (verified facts #2–#4, #10); secondary
  "Quick view" reads in-app via `PDFKit`/`QuickLook` from `/holding/<id>/file`. Optional:
  in-app PencilKit annotation with upload back.
- **Build/distribution caveat**: the Swift source can be scaffolded here, but it must be
  **compiled, run, and installed in Xcode** (Mac + Apple Developer account) — it cannot be
  built/run in the agent environment.
- **One-time setup**: add the Mac's `/dav` mount once in PDF Expert/GoodReader.

---

# Section 3 — PWA frontend (iPad + Boox)

A lightweight alternative: a **PWA that is a browse/launcher shell, not a reader.** You
browse in the PWA; "open" hands the file to the device's reader, where reading and stylus
annotation happen. This is the **only single client that covers both iPad and Boox**
(Boox can't run a SwiftUI app).

- **Client**: responsive browse UI (Library / Works / Authors / Favorites / Search) served
  by the existing Flask app + a web manifest for "Add to Home Screen." Same `/api` data.
- **Open action, per device** (verified facts in §1.5):
  - *Quick read* — `ghttp://`/`pdfehttp://` one-tap on iPad (#2,#3, with the #8 confirm
    prompt); download → "Open with" on Boox (#5,#7).
  - *Annotate* — open the **WebDAV-mounted copy** in the reader so markup round-trips (#4,#10).
- **Per-device expectations (honest):**
  - **iPad + PDF Expert/GoodReader** — true auto round-trip (open from WebDAV → annotate →
    close → syncs back). Solid.
  - **Boox + NeoReader** — annotate works well; pushing markup back to the server is
    annotate-then-upload and **possibly manual** (#11) → confirm on device before relying on it.
- **Fallback**: the guaranteed handoff on both platforms is plain download + "Open in… /
  Open with"; the one-tap scheme is an enhancement (standalone-PWA scheme launch is the
  one unverified item in §1.5).
- **One-time setup**: add the Mac's `/dav` mount once in each reader.

---

# Section 4 — Choosing between the frontends

| | PWA launcher (§3) | Native SwiftUI (§2) |
|---|---|---|
| Build/distribute | Flask server + web manifest; "Add to Home Screen" | Xcode + Apple Developer acct + App Store/sideload |
| Devices | iPad **and Boox** (+ any browser) | iPad/iPhone only — **Boox can't run it** |
| Reading/annotation | Hand off to PDF Expert / GoodReader / NeoReader | Same handoff, or in-app PencilKit |
| In-app polish | Web UI (can still look like Apple Music) | Nicer native feel |
| Buildable in this repo | **Yes, fully** | Source only; you build in Xcode |

The backend in §1 is identical either way, so the choice can be deferred or both built.
Because a **Boox is in the mix**, the PWA is the only single solution that serves it.
