// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Postilla",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "Postilla", targets: ["Postilla"]),
        .library(name: "PostillaRender", targets: ["PostillaRender"]),
    ],
    dependencies: [
        // Only the neutral seam — NOT octavo. Postilla anchors marks/ink at a
        // `Locator` and renders through `DecorationHost`; it does not depend on
        // any reading engine, so any reader that speaks ReaderContract can host it.
        .package(path: "../reader-contract"),
    ],
    targets: [
        // Pure core — models, ports, LWW sync engine. No UIKit/PencilKit.
        // Depends on ReaderContract for the Locator / Decoration contract types.
        .target(
            name: "Postilla",
            dependencies: [.product(name: "ReaderContract", package: "reader-contract")]
        ),

        // Capture + render. PencilKit/UIKit code is `#if canImport`-guarded so
        // the CoreGraphics renderer (FreehandRenderer) + DecorationHost mapping
        // still compile/run on macOS via `swift test`.
        .target(
            name: "PostillaRender",
            dependencies: [
                "Postilla",
                .product(name: "ReaderContract", package: "reader-contract"),
            ]
        ),

        // Pure unit tests — run fully on macOS via `swift test`.
        .testTarget(
            name: "PostillaTests",
            dependencies: ["Postilla", "PostillaRender"]
        ),
    ]
)
