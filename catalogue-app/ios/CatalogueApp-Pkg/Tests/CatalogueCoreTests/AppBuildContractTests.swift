import XCTest
@testable import CatalogueCore

/// U — the app-version handshake (AppBuildContract / AppBuildWatcher), the native mirror of
/// `static/js/app-version.js`. The `classify` truth table here is byte-for-byte the JS rule, so the
/// surfaces agree on when to prompt a reload / flag a stale server.
final class AppBuildContractTests: XCTestCase {
    private func decode<T: Decodable>(_ t: T.Type, _ json: String) throws -> T {
        try CatalogueJSON.decode(t, from: Data(json.utf8))
    }

    func testClassifyTruthTable() {
        // in sync
        XCTAssertEqual(AppBuildContract.classify(live: "abc", serverStale: false, baseline: "abc"), .ok)
        // server was rebuilt/redeployed under us
        XCTAssertEqual(AppBuildContract.classify(live: "def", serverStale: false, baseline: "abc"), .outdated)
        // server running stale code wins over everything
        XCTAssertEqual(AppBuildContract.classify(live: "abc", serverStale: true, baseline: "abc"), .serverStale)
        XCTAssertEqual(AppBuildContract.classify(live: nil, serverStale: true, baseline: nil), .serverStale)
        // forgiving: an older server that omits the fields, or before we've seen a baseline → ok
        XCTAssertEqual(AppBuildContract.classify(live: nil, serverStale: nil, baseline: "abc"), .ok)
        XCTAssertEqual(AppBuildContract.classify(live: "abc", serverStale: false, baseline: nil), .ok)
    }

    func testWatcherLatchesBaselineThenDetectsDrift() {
        var w = AppBuildWatcher()
        XCTAssertEqual(w.update(live: "b1", serverStale: false), .ok)          // first build seen = baseline
        XCTAssertEqual(w.baseline, "b1")
        XCTAssertEqual(w.update(live: "b1", serverStale: false), .ok)          // same build → still ok
        XCTAssertEqual(w.update(live: "b2", serverStale: false), .outdated)    // server rebuilt → outdated
        XCTAssertEqual(w.update(live: "b2", serverStale: true), .serverStale)  // stale flag wins
    }

    func testResetRebaselinesToTheNextBuildSeen() {
        var w = AppBuildWatcher()
        _ = w.update(live: "b1", serverStale: false)
        XCTAssertEqual(w.update(live: "b2", serverStale: false), .outdated)   // drift detected
        w.reset()                                                            // user acted on it
        XCTAssertNil(w.baseline)
        XCTAssertEqual(w.status, .ok)
        XCTAssertEqual(w.update(live: "b2", serverStale: false), .ok)        // b2 is the new baseline
        XCTAssertEqual(w.baseline, "b2")
    }

    func testWatcherUpdatesFromHealth() throws {
        var w = AppBuildWatcher()
        _ = w.update(try decode(Health.self, #"{"ok":true,"app_build":"x","server_stale":false}"#))
        XCTAssertEqual(w.baseline, "x")
        XCTAssertEqual(w.status, .ok)
    }

    func testHealthDecodesBuildFields() throws {
        let h = try decode(Health.self, #"{"ok":true,"api":1,"app_build":"deadbeef0001","server_stale":true}"#)
        XCTAssertEqual(h.appBuild, "deadbeef0001")
        XCTAssertEqual(h.serverStale, true)
    }

    func testHealthToleratesMissingBuildFields() throws {
        let h = try decode(Health.self, #"{"ok":true,"api":1}"#)   // an older server that predates the handshake
        XCTAssertNil(h.appBuild)
        XCTAssertNil(h.serverStale)
    }
}
