# reader-contract

The neutral seam shared by a reading engine and an annotation layer. It is a tiny,
Foundation-only package with three public types and no logic:

- **`Locator`** — where something is in a book: a format-tagged position
  (`page` / `cfi` / `progression` / `position`) plus optional surrounding text.
  The same shape across every binding, so a bookmark or highlight made on one
  platform resolves on another.
- **`Decoration`** — a mark anchored at a `Locator` (highlight, underline,
  strikethrough, note), with an optional precise anchor (EPUB CFI range or a
  normalized PDF rect).
- **`DecorationHost`** — a two-method protocol (`apply([Decoration])` / `clear()`)
  that a reader's view implements so an annotation layer can draw marks over it.

## Why it exists

`octavo` (the reading engine) and `postilla` (the annotation / handwriting layer)
used to be coupled: postilla depended on octavo just to reach these three types.
Pulling them into their own package inverts that — **both octavo and postilla
depend on `reader-contract`, and neither depends on the other**:

```
              ReaderContract
             (Locator, Decoration, DecorationHost)
                 ▲             ▲
         depends │             │ depends
                 │             │
             octavo         postilla        ← independent siblings
          (the reader)   (annotations/ink)
```

So postilla can be hosted by *any* reader that speaks this contract, and octavo can
ship without pulling in annotations. octavo and postilla each re-export
`ReaderContract` (`@_exported import`), so existing code that does `import Octavo`
or `import Postilla` keeps seeing `Locator` / `Decoration` / `DecorationHost` with
no changes.

## Technical details

`Locator` is `Codable` with deterministic (sorted-key) JSON, so its encoding is
byte-parity across the Swift / web / Kotlin bindings — the property the position
model relies on for cross-platform resolution. `Decoration` and `DecorationHost`
carry no rendering logic: the reader decides how a `Decoration` becomes pixels
(`PdfDecorationHost` maps it to a `PDFAnnotation`; an EPUB host maps it to an
epub.js annotation). The package deliberately has no platform dependency (no UIKit,
PDFKit, or WebKit) so it compiles and its types are usable everywhere.
