# Vendored EPUB assets (manual copy)

`EpubWebNavigator` inlines three scripts into its WKWebView host page. Two are **not committed here**
and must be copied from the web reader's vendor dir (same versions → identical CFIs across bindings):

- `epub.min.js`  — epub.js (from `catalogue-webui/.../static/vendor/`)
- `jszip.min.js` — JSZip (epub.js's archive dependency)

`epub-bridge.js` (the octavo command/event bridge) **is** committed here.

Until `epub.min.js` / `jszip.min.js` are dropped in, `EpubWebNavigator.hostHTML` inlines empty scripts
and the EPUB session is a no-op (the package still builds — `Bundle.module` just doesn't find them).
This is the one remaining step before EPUB renders on a simulator/device.
