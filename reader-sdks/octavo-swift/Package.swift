// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Octavo",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "Octavo", targets: ["Octavo"]),
        .library(name: "OctavoPDFKit", targets: ["OctavoPDFKit"]),
        .library(name: "OctavoEPUB", targets: ["OctavoEPUB"]),
        .library(name: "OctavoAdapters", targets: ["OctavoAdapters"]),
    ],
    dependencies: [
        // The neutral seam (Locator / Decoration / DecorationHost) — shared with
        // postilla, owned by neither. Octavo re-exports it (see Exports.swift) so
        // `import Octavo` still surfaces these types unchanged.
        .package(path: "../reader-contract"),
    ],
    targets: [
        // Pure contract mirror — no UIKit/PDFKit/WebKit.
        .target(
            name: "Octavo",
            dependencies: [.product(name: "ReaderContract", package: "reader-contract")]
        ),

        // Navigator engine — PDF (PDFKit; available on iOS AND macOS).
        .target(name: "OctavoPDFKit", dependencies: ["Octavo"]),

        // Navigator engine — EPUB (WKWebView + epub.js host). Bundles the JS bridge + (to be
        // vendored) epub.js/jszip, inlined into the host page so only the book itself is fetched
        // through the Source-backed custom scheme.
        // `.process` (not `.copy`): flatten the assets to the bundle root — the iOS-correct layout
        // (a copied `Resources/` subdir fails iOS resource-bundle codesign).
        .target(name: "OctavoEPUB", dependencies: ["Octavo"], resources: [.process("Resources")]),

        // Reference ports / adapters.
        .target(name: "OctavoAdapters", dependencies: ["Octavo"]),

        // Pure unit tests — run fully on macOS via `swift test`.
        // Depends on OctavoPDFKit because PDFKit IS on macOS (the gated PDF test runs here).
        .testTarget(
            name: "OctavoTests",
            dependencies: ["Octavo", "OctavoAdapters", "OctavoPDFKit", "OctavoEPUB"]
        ),
    ]
)
