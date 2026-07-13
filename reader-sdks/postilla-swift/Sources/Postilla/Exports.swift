// Postilla's public API surfaces contract types (e.g. `Annotation.locator: Locator`).
// Re-export the seam so consumers that `import Postilla` see `Locator` /
// `Decoration` / `DecorationHost` without importing ReaderContract, and so the
// rest of this module sees them without a per-file import.
@_exported import ReaderContract
