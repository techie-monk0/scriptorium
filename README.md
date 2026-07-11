# Scriptorium

**A local-first catalogue for a personal book library** — real authority control,
full-text search that actually finds things, OCR-aware ingest, and an offline
reader. Your own SQLite database; no server, no cloud required.

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
`-pwa` apps are built on top.

| Area | What it is |
|------|-----------|
| `catalogue/` (packages) | The reusable library: data layer, the authorized read/write API, business logic, ingest pipelines |
| `catalogue-webui/` | Flask web UI + the HTTP API the PWA consumes |
| `catalogue-cli/` | Batch/admin operations (dedup, backup, content-index build) |
| `catalogue-pwa/` | Installable, offline-first phone app |
| `docs/` | Design, data-model, and access-contract docs |

### Packages in the library

Dependency direction is one-way; nothing imports "upward".

| Package | Import | What it does |
|---------|--------|--------------|
| `contracts` | `catalogue.contracts` | Shared data types, open vocabularies, and the authorization contracts (`Principal`, `Policy`, `Action`, `Denied`) — no behavior |
| `db-store` | `catalogue.db_store` | Lowest data layer: connections (read-only vs read-write), schema, migrations, vocab seeding, integrity guard |
| `access-api` | `catalogue.access_api` | The one and only API for touching the database — per-concern reader/writer surfaces behind the policy gateway |
| `services` | `catalogue.services` | Business logic: cataloguing, classification, resolution, sweep, editions/works, covers, export |
| `populate` | `catalogue.populate` | The pipelines that populate the DB: scan sweeps, staging→load, batch imports |

## The database (you provide it)

The catalogue lives in a SQLite file you supply — it isn't shipped (it's your
library, and often large). Point at it with `$CATALOGUE_DB`, or drop it in
`catalogue-db/`. Its covers and caches sit alongside it. **Back it up yourself.**

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

## Development

`uv sync` installs the whole workspace editable from one lockfile. Each package has
its own tests; `uv run pytest` runs the suite. See **[HOWTO.md](HOWTO.md)** for
day-to-day commands.

## License

MIT — see [LICENSE](LICENSE).
