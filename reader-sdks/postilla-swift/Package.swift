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
        .library(name: "PostillaUI", targets: ["PostillaUI"]),
    ],
    dependencies: [
        // Local path dependency on the base Reader SDK.
        .package(path: "../octavo-swift"),
    ],
    targets: [
        // Pure core — models, ports, LWW sync engine. No UIKit/PencilKit.
        // Depends on Octavo for the Locator / Decoration contract types.
        .target(
            name: "Postilla",
            dependencies: [.product(name: "Octavo", package: "octavo-swift")]
        ),

        // Capture + render. PencilKit/UIKit code is `#if canImport`-guarded so
        // the CoreGraphics renderer (FreehandRenderer) + DecorationHost mapping
        // still compile/run on macOS via `swift test`.
        .target(
            name: "PostillaUI",
            dependencies: [
                "Postilla",
                .product(name: "Octavo", package: "octavo-swift"),
            ]
        ),

        // Pure unit tests — run fully on macOS via `swift test`.
        .testTarget(
            name: "PostillaTests",
            dependencies: ["Postilla", "PostillaUI"]
        ),
    ]
)
