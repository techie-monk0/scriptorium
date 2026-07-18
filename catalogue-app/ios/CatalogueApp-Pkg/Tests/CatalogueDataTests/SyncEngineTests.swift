import XCTest
@testable import CatalogueData
import CatalogueCore

/// A mutable outcome the test flips between refreshes (a reference so the `@Sendable` resource closure
/// can capture it).
private final class Mode: @unchecked Sendable {
    var value: SyncOutcome
    init(_ v: SyncOutcome) { self.value = v }
}

@MainActor
final class SyncEngineTests: XCTestCase {
    /// The reachability adapter (`OnlineState`) the data layer's `isOffline` predicate reads must mirror
    /// the engine's server-reachability: a resource reporting `.offline` flips it false; a later reachable
    /// answer (`.updated`/`.unchanged`) restores it — even with no connectivity event in between (covers
    /// "the server was just down, then came back on the same network").
    func testOnlineStateMirrorsServerReachability() async {
        let engine = SyncEngine()
        XCTAssertTrue(engine.onlineState.isOnline, "starts online")

        let mode = Mode(.offline)
        engine.register(ClosureResource(id: "r") { mode.value })

        _ = await engine.refresh(.manual)
        XCTAssertFalse(engine.onlineState.isOnline, "an .offline outcome marks us offline")
        XCTAssertFalse(engine.state.online)

        mode.value = .updated
        _ = await engine.refresh(.manual)
        XCTAssertTrue(engine.onlineState.isOnline, "a reachable answer restores online without a net event")
        XCTAssertTrue(engine.state.online)
    }
}
