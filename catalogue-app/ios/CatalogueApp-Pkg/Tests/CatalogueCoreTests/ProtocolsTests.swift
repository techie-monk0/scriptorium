import XCTest
@testable import CatalogueCore

/// U4 — Protocol visibility. `protocolVisible('default'|'local'|'desktop', ctx)` matches the
/// catalogue's truth table; a section declaring nothing stays visible; an unknown name is treated as
/// `default`. (Scan/Review gate on `desktop`, mount-roots on `local` — the same gate the server uses.)
final class ProtocolsTests: XCTestCase {
    private let none = ProtocolContext(local: false, desktop: false)
    private let local = ProtocolContext(local: true, desktop: false)
    private let desktop = ProtocolContext(local: false, desktop: true)
    private let both = ProtocolContext(local: true, desktop: true)

    func testDefaultAlwaysVisible() {
        for ctx in [none, local, desktop, both] {
            XCTAssertTrue(protocolVisible("default", ctx))
            XCTAssertTrue(protocolVisible(nil, ctx))      // no declared protocol → visible
            XCTAssertTrue(protocolVisible("bogus", ctx))  // unknown → treated as default
        }
    }

    func testLocalGate() {
        XCTAssertFalse(protocolVisible("local", none))
        XCTAssertTrue(protocolVisible("local", local))
        XCTAssertFalse(protocolVisible("local", desktop))
        XCTAssertTrue(protocolVisible("local", both))
    }

    func testDesktopGate() {
        XCTAssertFalse(protocolVisible("desktop", none))
        XCTAssertFalse(protocolVisible("desktop", local))
        XCTAssertTrue(protocolVisible("desktop", desktop))
        XCTAssertTrue(protocolVisible("desktop", both))
    }

    func testNavGatingDropsUnsatisfiedItems() {
        let items = [
            NavMenuItem(key: "home", label: "Home"),
            NavMenuItem(key: "scan", label: "Scan", protocol: "desktop"),
            NavMenuItem(key: "mounts", label: "Mounts", protocol: "local"),
        ]
        let phone = LibraryCore.navVM(items, activeKey: "home", ctx: none)
        XCTAssertEqual(phone.items.map(\.key), ["home"])          // scan + mounts gated out
        let desk = LibraryCore.navVM(items, activeKey: nil, ctx: both)
        XCTAssertEqual(desk.items.map(\.key), ["home", "scan", "mounts"])
    }
}
