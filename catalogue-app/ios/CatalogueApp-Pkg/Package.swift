// swift-tools-version: 6.0
import PackageDescription

// catalogue-app — the native iOS reader/library client (sibling of catalogue-webui / catalogue-pwa).
// SwiftPM library targets: pure tiers test headlessly via `swift test`; iOS UI/reader targets build for
// the simulator via `xcodebuild`. The reader HOSTS the octavo + postilla SDKs (local path deps).
//   CatalogueDesign  palette.json port + tokens                         [step 1]
//   CatalogueCore    Tier 2 — pure presenter/view-models               [step 3]
//   CatalogueData    adapters (API client, replica, file cache)         [steps 2,5]
//   CatalogueUI      Tier 3 — SwiftUI screens (iOS)                      [step 4]
//   CatalogueReader  in-app reader — hosts Octavo + Postilla (iOS)       [steps 6–7]
let package = Package(
    name: "CatalogueApp",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "CatalogueDesign", targets: ["CatalogueDesign"]),
        .library(name: "CatalogueCore", targets: ["CatalogueCore"]),
        .library(name: "CatalogueData", targets: ["CatalogueData"]),
        .library(name: "CatalogueReader", targets: ["CatalogueReader"]),
        .library(name: "CatalogueUI", targets: ["CatalogueUI"]),
    ],
    dependencies: [
        // The reader SDKs live in the sibling `octavo-postilla` repo (extracted out of this one).
        // This package is nested at catalogue-app/ios/CatalogueApp-Pkg, so reach up four levels to
        // /Users/…/Dev, then into octavo-postilla. Each package pulls `reader-contract` transitively.
        .package(path: "../../../../octavo-postilla/octavo/octavo-swift"),
        .package(path: "../../../../octavo-postilla/postilla/postilla-swift"),
    ],
    targets: [
        .target(name: "CatalogueDesign"),
        .target(name: "CatalogueCore"),
        .target(name: "CatalogueData", dependencies: ["CatalogueCore"]),
        .target(name: "CatalogueReader", dependencies: [
            "CatalogueCore", "CatalogueData", "CatalogueDesign",
            .product(name: "Octavo", package: "octavo-swift"),
            .product(name: "OctavoPDFKit", package: "octavo-swift"),
            .product(name: "OctavoEPUB", package: "octavo-swift"),
            .product(name: "OctavoAdapters", package: "octavo-swift"),
            .product(name: "Postilla", package: "postilla-swift"),
            .product(name: "PostillaRender", package: "postilla-swift"),
        ]),
        .target(name: "CatalogueUI", dependencies: ["CatalogueCore", "CatalogueData", "CatalogueDesign", "CatalogueReader"]),

        .testTarget(name: "CatalogueDesignTests", dependencies: ["CatalogueDesign"]),
        .testTarget(name: "CatalogueCoreTests", dependencies: ["CatalogueCore"], resources: [.copy("Goldens")]),
        .testTarget(name: "CatalogueDataTests", dependencies: ["CatalogueData", "CatalogueCore"]),
        .testTarget(name: "CatalogueReaderTests", dependencies: [
            "CatalogueReader", .product(name: "Octavo", package: "octavo-swift"),
            .product(name: "Postilla", package: "postilla-swift"),
        ]),
    ]
)
