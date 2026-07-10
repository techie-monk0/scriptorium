# catalogue-pwa

Phase-0 shell. The installable PWA front-end — service worker, app shell, and the
offline content-index consumer. It depends on **catalogue-webui only over HTTP**
(the API contract), so it shares no Python code with the rest of the repo.

It is a **JS/front-end** package (own `package.json` + lockfile, own build) and is
**not** part of the uv workspace. It lives in the monorepo (not a separate repo) so
HTTP-contract changes land atomically with the server.
