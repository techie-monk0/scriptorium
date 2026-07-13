// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ReaderContract",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "ReaderContract", targets: ["ReaderContract"]),
    ],
    targets: [
        // The neutral seam shared by a reading engine and an annotation layer:
        // `Locator` (where in a book), `Decoration` (a mark), `DecorationHost`
        // (draw marks). Foundation-only — no engine, no UIKit. Both `octavo`
        // (the reader) and `postilla` (annotations) depend on THIS, not on each
        // other, so postilla can be hosted by any reader that speaks it.
        .target(name: "ReaderContract"),
    ]
)
