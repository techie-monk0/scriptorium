# Scriptorium Reader (catalogue-app)

The native iOS reader/library client for the Scriptorium catalogue — the native sibling of
the web UI (`catalogue-webui`) and the PWA (`catalogue-pwa`). It browses and searches the
catalogue, reads PDF/EPUB holdings, and syncs highlights and annotations, working offline
against a local replica and reconnecting when the server is reachable.

It talks to the catalogue only over the public HTTP API (`/api/v1`, `/sync/reader`); the
server address is entered at runtime (LAN, tunnel, NAS, or direct), so nothing about a
particular deployment is baked into the app.

## Layout

- `ios/CatalogueApp-Pkg/` — the SwiftPM package: all app code and tests
  (`CatalogueCore`, `CatalogueData`, `CatalogueUI`, `CatalogueReader`, `CatalogueDesign`).
- `ios/CatalogueApp-XC/` — the runnable Xcode app bundle (generated with XcodeGen).
- `docs/` — build status and the implementation plan.

It hosts two reusable reading SDKs from the sibling `octavo-postilla` repo: **octavo**
(the PDF/EPUB engine) and **postilla** (annotations / handwriting).

## Build

```sh
# run the package tests
cd ios/CatalogueApp-Pkg && swift test

# generate + open the app project
cd ios/CatalogueApp-XC && xcodegen generate && open CatalogueApp.xcodeproj
```

## Bundle identifier / signing

The bundle id defaults to the public, unregistered `app.scriptorium.reader`. To build for a
device or the App Store, set your own identifier (registered to your Apple Developer team) in
a git-ignored `ios/CatalogueApp-XC/Config/App.local.xcconfig`:

```
PRODUCT_BUNDLE_IDENTIFIER = com.yourteam.yourapp
```

Then re-run `xcodegen generate`. The keychain service name is derived from the bundle id at
runtime, so overriding the id is all that's needed.

## Handwriting recognition

Online handwriting recognition (e.g. via a commercial engine such as MyScript) is an optional,
not-yet-wired seam — the app builds and runs without it, and ships no such credential.

## Technical details

See `docs/ios_native_plan.md` for the architecture (Tier-2 view-model parity with the web
client, the reader/annotation stack, offline replica) and `docs/STATUS.md` for the current
build status and known gaps.
