# Library cataloguing — usage

A short guide to running the server, reading the web UI, and the two ways to add books
(physical via the phone app, digitized via folder scan).

> Setting up the toolchain (uv, `uv sync`, running tests, managing dependencies) lives in
> the README's [Development (uv)](../README.md#development-uv) section.

## Environment variables

**You don't need to set anything to start the app and browse an existing catalogue** —
sensible defaults apply, and when something genuinely required is missing the app
**stops with a clear message** rather than misbehaving quietly. Environment variables
are how you point the app at your data and turn on optional features. Most data-location
settings can also be set in the web **Settings** page instead (which saves them for you).

**The few things that are required — and error clearly if missing:**

- **A database.** Defaults to `private/catalogue-db/catalogue.db`; point elsewhere with
  `$CATALOGUE_DB`. If the file doesn't exist, the app tells you how to create one
  (it does not silently start empty). See the README "database" section.
- **A books folder — only if you add/scan books from disk.** Set `$CATALOGUE_MOUNT_ROOT`
  (or configure the library root in Settings). Trying to scan without it stops with
  *"No library books directory is configured…"* instead of scanning the wrong folder.
- **Login credentials — only if you serve the app on the public internet.**
  `$CATALOGUE_AUTH_USER` / `$CATALOGUE_AUTH_PASS`; the launcher refuses to expose an
  unauthenticated server without them.

Everything else is optional. Add API keys (see the README's "API keys" table) only for
cloud AI help and online lookups.

### Technical details

| Variable | Purpose | If unset |
|----------|---------|----------|
| `CATALOGUE_DB` | Path to the catalogue `.db` file | Falls back to `private/catalogue-db/catalogue.db`; **errors clearly** if that file is absent |
| `CATALOGUE_DATA_DIR` | Folder holding the DB + its caches (alternative to `CATALOGUE_DB`) | Uses the `private/catalogue-db/` folder |
| `CATALOGUE_MOUNT_ROOT` / `CATALOGUE_LIBRARY_ROOT` | Your on-disk books folder(s) (`LIBRARY_ROOT` is `os.pathsep`-separated for several) | Uses the library root set in Settings; **scanning errors clearly** if none is set |
| `CATALOGUE_INBOX_DIR` | Drop folder scanned first for freshly-added books | No inbox (feature simply off) |
| `CATALOGUE_TRASH_DIR` | Where deleted book files are moved | Delete-to-Trash **errors clearly** until set (in Settings or here) |
| `CATALOGUE_AUTH_USER` / `CATALOGUE_AUTH_PASS` | Login for public/tunnel serving | The launcher **refuses** to expose an unauthenticated server |
| `CATALOGUE_ALLOW_OPEN` | Run without a login on loopback only | Auth fail-closes (no anonymous access) |
| `CATALOGUE_SECRET` | Signs login cookies | A dev default is used — **set this in any shared deployment** |
| `CATALOGUE_VOCAB` / `CATALOGUE_VOCAB_LOCAL` | Authority-control vocab file / your overlay | Uses the shipped `vocab.json` (see README "Vocabulary lists") |
| `CATALOGUE_LLM_MODELS` / `CATALOGUE_LLM_BASE_URL` / `CATALOGUE_LLM_TIMEOUT` | Local/cloud LLM overrides | Uses `vocab.json` `_local_llm` / `_external_llm` |
| `CATALOGUE_FEATURES` | Feature-flag overrides | Uses `vocab.json` `_features` |
| `CATALOGUE_DRY_RUN`, `CATALOGUE_ACCESS_LOG`, `CATALOGUE_PERFLOG`, `CATALOGUE_RESOLVER`, `CATALOGUE_HTTP_MIN_INTERVAL` | Advanced toggles (dry-run writes, request logging, perf tracing, resolver choice, rate-limit) | Off / sensible defaults |
| `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` / `GEMINI_API_KEY`, `GOOGLE_BOOKS_API_KEY`, `KDRIVE_WEBDAV_*` | External services (cloud AI, covers, kDrive sync) | Those features are simply unavailable — see the README "API keys" table |

Provide keys/secrets as environment variables or as `KEY=VALUE` lines in a git-ignored
`api_key.txt` at the project root (an environment variable wins if both are set).

## Start the server

**Always start it with the launcher** — it sources your credentials so the app comes up
authenticated, and brings the Cloudflare tunnel up alongside it:

```bash
cd /path/to/library_cataloging
caffeinate -s bash scripts/library-serve.sh        # Flask (:8000) + tunnel; Ctrl-C stops both
```

Open **http://localhost:8000** (or `/app`). It binds `0.0.0.0:8000` so the iPhone scanner can
reach it over Wi-Fi (use the Mac's LAN IP, e.g. `http://192.168.1.50:8000`, from the phone), and
it's reachable remotely over the tunnel at **https://your-domain.example** — see **Remote access** below.

- Live DB is `private/catalogue-db/catalogue.db` (the root `catalogue.db` is empty — ignore it).
- `caffeinate -s` keeps long OCR/LLM passes from freezing when the Mac sleeps (it does **not**
  stop lid-close sleep).
- To restart and pick up code/credential changes: **Ctrl-C and re-run the launcher**.

**Localhost only (no LAN, no tunnel, no login):**
```bash
bash scripts/library-serve.sh --local      # binds 127.0.0.1:8000 only; runs open (safe on loopback)
```
Use this for a quick private run on the Mac itself. It **won't start while the public tunnel is
running** (that would bridge the open server to the internet) — it tells you how to stop the tunnel
first. Add `--perflog` to either mode for `[PERF]` request tracing.

> **Why the launcher, not `python -m catalogue.webui.web` directly:** the app **fail-closes** —
> it refuses to start unless authentication is configured (`CATALOGUE_AUTH_USER`/`PASS`, which
> `library-serve.sh` sources from `~/.catalogue-auth`). This is deliberate: the public tunnel must
> never be able to front an unauthenticated backend. A bare `python -m catalogue.webui.web` with no
> credentials will exit with that message — use `--local` above for a no-auth localhost run, or set
> `CATALOGUE_ALLOW_OPEN=1` if you must invoke the module directly.

## The phone PWA (`/app`)

`/app` is the installable phone app (home shelves, Search/Browse, Content search, in-app reader,
ISBN capture). On the Mac open **http://localhost:8000/app**; on a phone, "Add to Home Screen".

Over a plain LAN URL (`http://<LAN-IP>:8000/app`) it works **online only** — iOS/Safari disables
service workers, offline caching, and install on non-HTTPS origins. For **offline** use on the
phone you need an HTTPS origin → see **Remote access** below. Offline content search is opt-in
under the app's **Settings**.

## Remote access — stable HTTPS + auth

The PWA is **offline-first**, so the server only needs to be reachable when you **sync**: refresh
the catalogue, (re)download the offline content index, cache a book to read offline, or flush
queued ISBN captures. Otherwise leave it down — the phone uses its offline copy. It's reached over
a permanent Cloudflare **named tunnel**, gated by a **signed-cookie login** (the default when
`CATALOGUE_AUTH_USER`/`PASS` are set; `CATALOGUE_AUTH=basic` opts into HTTP Basic instead). The app
**fail-closes** if neither is configured, so the tunnel can never front an unauthenticated backend.

**Stable URL:** `https://your-domain.example/app` (never changes → the installed PWA + its downloaded
offline index persist across sessions).

### Bring it up for a sync session
```bash
bash scripts/library-serve.sh        # starts Flask (:8000) + the tunnel; Ctrl-C stops both
```
It sources your credentials from `~/.catalogue-auth` and warns if they're missing (don't expose
the public URL unauthenticated). Wrap in `caffeinate -s` to stop idle sleep — note that does NOT
prevent lid-close sleep, which stops the server/tunnel until you re-run the launcher.

### Restarting (and the port-8000 guard)
To pick up code/credential changes, just **Ctrl-C and re-run the launcher**. If a previous server
is still holding port 8000 (it didn't shut down, or another copy is running), the launcher
**detects it and asks before killing it**:
```text
⚠️  Port 8000 is already in use (probably a previous server):
      Python  34281 youruser ... TCP *:8000 (LISTEN)
    Kill it and restart? [y/N]
```
- **`y`** → it stops the old server (force-kills if stubborn) and starts fresh on the new code.
- **anything else** → it leaves the old one running and exits (the new server can't bind while
  :8000 is taken).

This guard exists because a leftover server silently keeps serving the **old** code while the
tunnel forwards to it — e.g. an old login still in effect — which looks like "my changes did
nothing." Answering `y` guarantees you're running the current code. (It also clears any stray
`cloudflared tunnel run` from a previous run.)

To stop everything by hand instead:
```bash
pkill -f catalogue.webui.web; pkill -f "cloudflared tunnel run"
```

### One-time setup (already done — kept here for rebuild/reference)
- **Domain:** `your-domain.example`, registered at **Cloudflare Registrar**, so it's already on
  Cloudflare DNS — no nameserver changes.
- **Tunnel** (created once):
  ```bash
  cloudflared tunnel login                                   # browser auth; pick the zone
  cloudflared tunnel create library
  cloudflared tunnel route dns library your-domain.example        # permanent hostname → tunnel
  ```
  Config lives in `~/.cloudflared/config.yml` (tunnel id `6a802f23-…`, ingress →
  `http://localhost:8000`). Sanity-check it with `cloudflared tunnel ingress validate`.

### Access control (auth) — a pluggable seam
Gating goes through `catalogue/webui/auth.py` (`AuthProvider`); `create_app` just calls
`auth.install(app)`. Selected by env (`CATALOGUE_AUTH`):
- `none` (default with no credentials) — open. Fine on localhost; **never** behind the public tunnel.
- `cookie` (**default when the two vars below are set**) — a same-origin **signed-cookie session**
  set by a `/login` form. The cookie auto-attaches to *every* request (incl. sync + the
  content-index download) AND carries a max-age (90 days), so the PWA **stays logged in across
  launches** and re-auths through one form — not a native dialog on every cold start. This is the
  PWA default. (We use a same-origin cookie and **not** Cloudflare Access — Access 302-redirects
  every request to a *cross-origin* login a PWA's `fetch()` can't complete.)
- `basic` — **HTTP Basic Auth**. Simple, but it has *no* session lifetime: an iOS home-screen PWA
  re-prompts on every cold launch, and the native dialog can double-fire. Kept for curl / non-PWA
  use; set `CATALOGUE_AUTH=basic` to opt in.

**What the login asks for:** open `https://your-domain.example` and you get a **username / password**
form (the app's own page, not a browser dialog). That is **your own library credential — NOT your
Cloudflare login**; your Flask server checks it, Cloudflare isn't involved (Access is deleted). The
values come from `~/.catalogue-auth` on the Mac: `CATALOGUE_AUTH_USER` (e.g. `youruser`) and
`CATALOGUE_AUTH_PASS`. Sign in once per device and the session cookie keeps you in for ~90 days.

Credentials live in `~/.catalogue-auth` (chmod 600, on the Mac, outside the repo), sourced by the launcher:
```bash
printf 'export CATALOGUE_AUTH_USER=youruser\nexport CATALOGUE_AUTH_PASS=%s\n' "$(openssl rand -base64 18)" \
  > ~/.catalogue-auth && chmod 600 ~/.catalogue-auth && cat ~/.catalogue-auth
```
When the server starts it **prints these credentials to the console** (and prints a one-time
reminder the first time the PWA hits it unauthenticated), so you always see what to type.

- **Rotate the password:** edit `~/.catalogue-auth` (or re-run the line above), restart the
  launcher. The cookie signing key is derived from the password, so changing it **instantly
  invalidates every device's session** — each re-logs in once on next use.
- **Change the protocol later** (bearer token, Cloudflare-Access JWT, mTLS, …): add an
  `AuthProvider` subclass + a branch in `provider_from_env`, or inject one with
  `auth.install(app, MyProvider())`. Routes and `create_app` don't change.

### Maintaining / checking it
- **Up?**  `curl -u <user>:<pass> https://your-domain.example/api/v1/health` → `{"ok":true,…}`;
  without `-u` it should return **401** (gate working).
- **Stop everything:** `pkill -f catalogue.webui.web; pkill -f "cloudflared tunnel run"`.
- **After reboot / lid sleep:** nothing auto-starts — re-run the launcher when you want to sync.
- **First use on a device:** the browser/PWA prompts for the user/pass once, then remembers it.
- **Throwaway alternative (no named setup):** `cloudflared tunnel --url http://localhost:8000`
  prints a random `*.trycloudflare.com` URL (new every run, ungated — obscurity only); fine for a
  one-off test, bad for daily use (the changing origin resets the installed PWA each time).

## The home page (6 cards)

| Card | URL | What it's for |
|---|---|---|
| 📖 **Browse** | `/library` | Page through every book; edit titles, people, works in place. Badge = total books. |
| 🔎 **Search** | `/search` | Metadata search/browse by title, person, work, or subject; edit in place. |
| 📖 **Content search** | `/text` | Full-text search *inside* the books — page-level matches with snippets. |
| ✓ **Review** | `/review-hub` | The work queue: confirm detected works, complete works, resolve people & subjects. Badge = pending count. |
| 📂 **Scan directory** | `/reconcile` | Bring **newly added / moved / re-OCR'd digitized files** into the catalogue. |
| 📷 **Capture** | `/capture` | Add a **physical book** by ISBN (from the phone), then resolve it to an edition. |

Badges turn orange when there's pending work. Most cards open a master–detail page: a list on
the left, the selected record's editable card on the right.

## Check whether a book is already in the library (phone app)

The **ISBN Scanner** iPhone app (`../isbn-scanner/`) is a thin client of the server. Set its
**Server base URL** (gear, top-left) to the Mac's `http://<LAN-IP>:8000` — the status line
under the field turns **green** when the contract version matches.

Two ways to check a book, both giving the same "already in catalogue?" verdict:

1. **Barcode** — point the camera at the ISBN-13 barcode. The app validates the checksum
   on-device and POSTs to `/capture`. The row shows **duplicate** (already held) or **sent**
   (new), tap to review.
2. **Pages mode** — for books with no barcode (older / foreign / reprints): switch the mode
   selector (right edge) to **Pages**, tap **Scan pages**, photograph the **copyright page**
   (B&W filter) — plus title/back page if there's no ISBN — and **Save**. The app OCRs on-device
   and POSTs the *text* to `/capture/cip`; the server parses the CIP block and answers the same
   found / not-found verdict. (No images leave the phone.)

Failed sends (Mac unreachable) are queued with a **Retry** button — scans are never lost.
For tips on autocorrect, signing, and LAN setup, see the app's own `README.md`.

## Add new digitized books (folder scan)

For PDFs/EPUBs you've digitized or downloaded into the library folder:

1. Open **📂 Scan directory** (`/reconcile`).
2. Confirm the root(s) to scan (defaults to `$CATALOGUE_LIBRARY_ROOT` or the sweep mount),
   then **Run**. Moves and re-OCRs are auto-applied; genuinely new/ambiguous files are queued.
3. For each queued item pick a disposition:
   - **new / distinct** — brand-new edition + holding (most new books).
   - **add_copy** — another copy of an existing edition.
   - **replace** — this file supersedes a chosen edition's copy.
   - **repoint / accept** — same book, just moved or re-annotated in place.
   - **remove / ignore** — drop a missing holding, or skip.

New editions then flow into **Review** (`/review-hub`) for work detection, then
people/subject resolution. Books under an `ANNOTATED`-named folder are excluded by design.

## Once a book is in: Review

`/review-hub` is a tabbed queue (Books / Works / People / Subjects):
- **Books / Works** — confirm the detected work(s) per edition; mark multi-work editions;
  link to authority works; set root↔commentary relationships.
- **People / Subjects** — disambiguate, merge duplicates, bind to external authorities.

Merges, deletes, and applies are reversible (↩ Undo). When you're confident, **Browse**
(`/library`) is the place to do free-form edits to any record.

## Quick health checks

- `/health` — server up.
- `/integrity` — dangling-reference / schema-drift report.
