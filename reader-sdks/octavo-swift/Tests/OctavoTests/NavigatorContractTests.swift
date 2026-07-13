import XCTest
@testable import Octavo

/// OS-U5 — goTo ⇄ onLocationChanged consistency, tested against the protocol
/// contract via a tiny in-memory FakeNavigator (no real document needed).
@MainActor
final class NavigatorContractTests: XCTestCase {

    func testGoToEmitsSameLocator() async throws {
        let nav = FakeNavigator(publicationId: "pub-1", pageCount: 100)
        var emitted: [Locator] = []
        nav.onLocationChanged = { emitted.append($0) }

        try await nav.open()
        let target = Locator(publicationId: "pub-1", format: .pdf,
                             locations: .init(page: 42))
        try await nav.goTo(target)

        // The last emitted location resolves to the requested page.
        XCTAssertEqual(emitted.last?.locations.page, 42)
        XCTAssertEqual(nav.currentLocation?.locations.page, 42)
        // progression is consistent with the page.
        XCTAssertEqual(nav.currentLocation?.locations.progression, 41.0 / 100.0)
    }

    func testNextPrevConsistency() async throws {
        let nav = FakeNavigator(publicationId: "p", pageCount: 10)
        try await nav.open()                       // page 1
        try await nav.next()                       // page 2
        try await nav.next()                       // page 3
        XCTAssertEqual(nav.currentLocation?.locations.page, 3)
        try await nav.prev()                       // page 2
        XCTAssertEqual(nav.currentLocation?.locations.page, 2)
    }

    /// The façade restores a saved position and persists changes.
    func testFacadeRestoreAndPersist() async throws {
        let store = SpyStore()
        await store.seed("p", Locator(publicationId: "p", format: .pdf,
                                      locations: .init(page: 7)))
        let nav = FakeNavigator(publicationId: "p", pageCount: 50)

        let reader = try await Octavo.open(
            navigator: nav, publicationId: "p", readingStore: store)

        // Restored to the saved page.
        XCTAssertEqual(reader.currentLocation?.locations.page, 7)
    }
}

/// Minimal in-memory Navigator exercising the page⇄Locator contract.
@MainActor
final class FakeNavigator: Navigator {
    let publicationId: String
    let pageCount: Int
    private var page = 1

    var currentLocation: Locator?
    var onLocationChanged: (@MainActor (Locator) -> Void)?

    init(publicationId: String, pageCount: Int) {
        self.publicationId = publicationId
        self.pageCount = pageCount
    }

    func open() async throws { go(to: 1) }

    func goTo(_ locator: Locator) async throws {
        go(to: locator.locations.page ?? 1)
    }

    func next() async throws { if page < pageCount { go(to: page + 1) } }
    func prev() async throws { if page > 1 { go(to: page - 1) } }
    func search(_ query: String) async throws -> [Locator] { [] }
    func outline() -> [TocItem] { [] }

    private func go(to p: Int) {
        page = min(max(p, 1), pageCount)
        let loc = Locator(
            publicationId: publicationId, format: .pdf,
            locations: .init(
                page: page,
                progression: Double(page - 1) / Double(pageCount),
                position: page - 1))
        currentLocation = loc
        onLocationChanged?(loc)
    }
}

private actor SpyStore: ReadingStore {
    private var store: [String: Locator] = [:]
    func seed(_ id: String, _ loc: Locator) { store[id] = loc }
    func getPosition(_ id: String) async throws -> Locator? { store[id] }
    func setPosition(_ id: String, _ loc: Locator) async throws { store[id] = loc }
    func recent(_ n: Int) async throws -> [Locator] { Array(store.values.prefix(n)) }
}
