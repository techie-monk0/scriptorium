# Scriptorium

**A local-first catalogue for a personal book library** — real authority control,
full-text search that actually finds things, OCR-aware ingest, and offline reading
on the web, a phone PWA, and a native iOS app. Your own SQLite database; no server,
no cloud required.

Handles everyday ebooks and PDFs, and has deep optional support for scanned and
scholarly texts (including Tibetan/Sanskrit/Buddhist material).

<!-- TODO: a screenshot or short GIF of the web UI + PWA goes here — it's the
     single biggest thing that makes a repo feel real. -->

## What makes it different

- **Real authority control** — works, editions, and persons are distinct, linked
  entities (FRBR-style), not a flat list of files like most personal tools.
- **Search that finds things** — full-text search that folds diacritics
  (*tathāgatagarbha* ↔ *tathagatagarbha*), collapses name/spelling variants, and
  strips honorifics.
- **Local-first & private** — your own SQLite file; a local LLM (Ollama) does the
  AI work; the reader works offline. Cloud AI (Claude/Gemini) is optional.
- **Scans, not just clean PDFs** — tracks OCR/scan provenance (born-digital vs
  scanned), quality, and re-OCR history.
- **Scholarly depth when you want it** — optional tradition classification,
  root-text ↔ commentary relationships, and BDRC/84000 authority resolution.
- **Built to last** — reads and writes are physically separated behind an
  authorization gateway; a schema-drift guard; extensible controlled vocabularies.

## Quick start

```bash
uv sync                                             # install the workspace
CATALOGUE_DB=./my-library.db uv run python -m catalogue.db_store.db   # create a DB
uv run python -c "from catalogue.webui.web import create_app; create_app().run(port=8000)"
```

Open **http://localhost:8000**. You supply the database — nothing is uploaded
anywhere. See **[docs/USAGE.md](docs/USAGE.md)** for the launcher, the phone PWA,
and the full environment-variable reference.

## How it's organized

A monorepo of independent packages with one-way dependencies
(`contracts ← db-store ← access-api ← services ← apps`), enforced by import-linter.
The `catalogue.*` library is reusable on its own; the `catalogue-webui` / `-cli` /
`-pwa` apps and the native iOS app (`catalogue-app`) are built on top. For the full
picture — packages, entities and their relationships, reads/writes, ingest, and
search — see **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

| Area | What it is |
|------|-----------|
| `catalogue/` (packages) | The reusable library: data layer, the authorized read/write API, business logic, ingest pipelines |
| `catalogue-webui/` | Flask web UI + the HTTP API the PWA and native app consume |
| `catalogue-cli/` | Batch/admin operations (dedup, backup, content-index build) |
| `catalogue-pwa/` | Installable, offline-first phone app |
| `catalogue-app/` | Native iOS reader/library client — **Scriptorium Reader** (browse, search, read, sync annotations, offline replica) |
| `../octavo-postilla/` *(sibling repo)* | Reusable, host-agnostic reading SDKs the app embeds — **octavo** (PDF/EPUB engine), **postilla** (annotations / handwriting), **reader-contract** (the shared seam). Extracted out of this repo; consumed by `catalogue-app` via relative path |
| `docs/` | Design, data-model, and access-contract docs |

### Packages in the library

Dependency direction is one-way; nothing imports "upward".

| Package | Import | What it does |
|---------|--------|--------------|
| `contracts` | `catalogue.contracts` | Shared data types, open vocabularies, and the authorization contracts (`Principal`, `Policy`, `Action`, `Denied`) — no behavior |
| `db-store` | `catalogue.db_store` | Lowest data layer: connections (read-only vs read-write), schema, migrations, vocab seeding, integrity guard. Also publishes the versioned, language-neutral **contracts** consumers verify against without importing catalogue code — the external read-contract (edition identity) and the **`catalogue.reader_sync`** wire contract (`/sync/reader`, consumed by the web/PWA readers and the native `postilla` adapter) |
| `access-api` | `catalogue.access_api` | The one and only API for touching the database — per-concern reader/writer surfaces behind the policy gateway |
| `services` | `catalogue.services` | Business logic: cataloguing, classification, resolution, sweep, editions/works, covers, export |
| `populate` | `catalogue.populate` | The pipelines that populate the DB: scan sweeps, staging→load, batch imports |

## The database (you provide it)

The catalogue lives in a SQLite file you supply — it isn't shipped (it's your
library, and often large). Point at it with `$CATALOGUE_DB`, or drop it in
`private/catalogue-db/`. Its covers and caches sit alongside it. **Back it up yourself.**

Create a fresh, empty database:

```bash
CATALOGUE_DB=/path/to/catalogue.db uv run python -m catalogue.db_store.db
```

## Reads vs. writes & authorization

Every database access goes through `access-api`, where **reads are physically
separated from writes**: readers get an OS-enforced read-only connection (a reader
literally cannot write, even with a bug); each operation declares an `Action` that a
policy gateway checks before dispatch. Access modules contain no auth logic, so an
auth layer can allow/deny each operation cleanly.

## Configuration

Nothing is required just to browse an existing catalogue. Adding books from disk
needs a books folder (`$CATALOGUE_MOUNT_ROOT` or the Settings page); serving on the
public internet needs a login. Anything genuinely required **fails with a clear
message** rather than misbehaving. Full table in **[docs/USAGE.md](docs/USAGE.md)**.

## API keys (all optional)

With no keys you can catalogue by hand, run the app, search, and use a **local**
model. Keys unlock cloud AI and online lookups (`ANTHROPIC_API_KEY`, Gemini, Google
Books, WebDAV sync) — see the table in `docs/USAGE.md`.

## Extending the vocabulary

The controlled vocabularies it matches against (honorifics, name-spelling variants,
organization markers, traditions, dropdown options) ship as defaults and are
**user-extensible** without editing the shipped file — drop a `vocab.local.json`
next to your database. See `catalogue.db_store.authority_vocab`.

## Development (uv)

This repo uses **uv** to manage the Python version, the virtualenv, and every package
in the monorepo — you rarely call `python`/`pip` directly. (Full day-to-day reference,
including the phone PWA and the server launcher, is in
**[docs/USAGE.md](docs/USAGE.md)**.)

**Install uv** (once, macOS / Linux):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

**Set up / refresh the workspace** — after cloning, and whenever dependencies change:

```bash
uv sync        # creates .venv/ and installs every package editable, from uv.lock
```

"Editable" means code edits are live with no reinstall; if imports break after a pull,
run `uv sync` again.

**Run things** inside the project env (no manual activation needed):

```bash
uv run pytest                                      # the whole test suite
uv run pytest catalogue-packages/db-store/tests    # just one package's tests
```

**Manage dependencies** — always name the owning package with `--package`:

```bash
uv add flask --package catalogue-webui         # add a third-party dependency
uv add access-api --package catalogue-webui    # depend on one of OUR packages
uv remove flask --package catalogue-webui      # remove one
uv lock --upgrade && uv sync                   # bump locked versions, then install
```

Cheat sheet:

| I want to… | Command |
|------------|---------|
| Set up / refresh the workspace | `uv sync` |
| Run the tests | `uv run pytest` |
| Test one package | `uv run pytest catalogue-packages/<pkg>/tests` |
| Run any command in the env | `uv run <command>` |
| Add a dependency to a package | `uv add <dep> --package <pkg>` |
| Remove a dependency | `uv remove <dep> --package <pkg>` |
| Re-lock after upstream changes | `uv lock` then `uv sync` |

Troubleshooting: **`uv: command not found`** → reinstall, then open a new terminal;
**import errors after pulling** → `uv sync`.

## License

MIT — see [LICENSE](LICENSE). The reusable reading SDKs in the sibling
`octavo-postilla` repo (**octavo**, **postilla**) are Apache-2.0, per their own READMEs.
