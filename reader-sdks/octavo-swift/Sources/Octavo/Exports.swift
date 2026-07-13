// The reader/annotation seam lives in its own package (`ReaderContract`) so that
// postilla can be hosted by any reader, not just octavo. Octavo re-exports it so
// every existing consumer — `import Octavo` and the whole module below — keeps
// seeing `Locator`, `Decoration`, and `DecorationHost` with no import changes.
@_exported import ReaderContract
