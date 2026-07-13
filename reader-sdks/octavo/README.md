# octavo + postilla

A host-agnostic, embeddable **reading SDK** (`octavo`) and its **annotations / handwriting /
recognition extension** (`postilla`). Extracted in spirit from the catalogue's reader
(`reader-core` / `overlay` / `/sync/reader`), generalized so any app can embed it: open a book
from any byte source, render/search/navigate PDF + EPUB, and (with `postilla`) highlight, ink,
sync annotations as structured data, and drive an LLM/research layer via the Locator seams.

- **octavo** — the engine. Ports: Source · Locator · Navigator · ReadingStore. Apache-2.0.
- **postilla** — the extension. Annotation model + offline-first sync-of-record + handwriting
  (canonical perfect-freehand ink) + recognition seam + portable export.

## Status

**Pre-build / planning.** No code yet — this directory currently holds the design docs. It lives
**inside `library_cataloging`** for now, behind a hard no-`catalogue`-import boundary
(import-linter `forbidden` contract + eslint/grep on the JS engine), with the catalogue as the
first consumer. It **extracts to its own public repo at the v1 API freeze** (`git filter-repo` of
this subtree). This is the deliberate exception to the catalogue's central-docs rule — the SDK's
docs are colocated here so they travel out with the package.

## Docs

- [`docs/octavo.md`](docs/octavo.md) — base engine: architecture, ports, public API, the boundary
  contract, extraction strategy, tests.
- [`docs/postilla.md`](docs/postilla.md) — annotations / handwriting / recognition extension:
  model, sync-of-record, the canonical-ink rule, recognition seam, integration hooks, export.
- [`docs/octavo-swift_plan.md`](docs/octavo-swift_plan.md) — iOS/macOS binding (impl & test plan):
  `PdfKitNavigator`/`EpubWebNavigator`, Swift ports, parity gate. The package `catalogue-app` hosts.
- [`docs/postilla-swift_plan.md`](docs/postilla-swift_plan.md) — iOS annotation/handwriting binding
  (impl & test plan): PencilKit input + perfect-freehand-Swift render, sync engine, export.

See also the consuming app's native binding: `../../catalogue-app/docs/ios_native_plan.md` (**`catalogue-app`**,
the native sibling of `catalogue-webui`/`catalogue-pwa`) — its reader, impl step 6–7, hosts these.
