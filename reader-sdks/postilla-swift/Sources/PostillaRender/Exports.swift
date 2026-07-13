// `MarkOverlay` / `Decorations` expose contract types (`Locator`, `Decoration`,
// `DecorationHost`) in their public API. Re-export the seam so consumers that
// `import PostillaRender` see them without importing ReaderContract.
@_exported import ReaderContract
