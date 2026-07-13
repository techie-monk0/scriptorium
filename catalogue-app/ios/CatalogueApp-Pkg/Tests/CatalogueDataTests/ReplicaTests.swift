import XCTest
@testable import CatalogueData
import CatalogueCore

/// U6 — Replica grouping / offline selection. Search & Browse derived from the cached replica match
/// expectations (diacritic-insensitive, like the live `/find`), and the offline-vs-live selector picks
/// the cached path. Also covers `ReplicaStore` persistence round-trip.
final class ReplicaTests: XCTestCase {
    private let replicaJSON = #"""
    {"schema_version":3,"count":3,"editions":[
      {"edition_id":42,"title":"Bodhicaryāvatāra","display_title":"Bodhicaryāvatāra",
       "authors":["Śāntideva"],"translators":["Crosby"],"isbns":["9780192835802"],
       "subjects":["Buddhism/Emptiness"],"work_titles":["Bodhicaryāvatāra"],
       "holdings":[{"holding_id":7,"format":"pdf","kind":"pdf","has_file":true,"storage":null}]},
      {"edition_id":43,"title":"The Way of the Bodhisattva","display_title":"The Way of the Bodhisattva",
       "authors":["Śāntideva","Padmakara"],"translators":["Wallace"],"isbns":[],
       "subjects":["Buddhism/Ethics"],"work_titles":[],"holdings":[]},
      {"edition_id":50,"title":"Zhuangzi","authors":["Zhuang Zhou"],"translators":[],"isbns":[],
       "subjects":["Daoism"],"work_titles":[],"holdings":[{"holding_id":9,"format":"epub","kind":"epub","has_file":true,"storage":null}]}
    ]}
    """#

    private func replica() throws -> Replica { try CatalogueJSON.decode(Replica.self, from: Data(replicaJSON.utf8)) }
    private func data() throws -> ReplicaData { ReplicaData(try replica()) }

    func testSearchMatchesTitleNewestFirst() async throws {
        let cards = try await data().search("bodhi")     // matches both Bodhi* titles
        XCTAssertEqual(cards.map(\.eid), [43, 42])        // newest-first (id desc)
        XCTAssertEqual(cards.first(where: { $0.eid == 42 })?.by, "Śāntideva")
        XCTAssertEqual(cards.first(where: { $0.eid == 42 })?.coverUrl, "/edition/42/cover.jpg")
        XCTAssertEqual(cards.first(where: { $0.eid == 42 })?.holdingId, 7)
        XCTAssertEqual(cards.first(where: { $0.eid == 42 })?.hasFile, true)
    }

    func testSearchIsDiacriticInsensitive() async throws {
        let plain = try await data().search("santideva")
        let accented = try await data().search("Śāntideva")
        XCTAssertEqual(Set(plain.map(\.eid)), [42, 43])
        XCTAssertEqual(plain.map(\.eid), accented.map(\.eid))    // accent-folded the same
    }

    func testEmptyQueryReturnsAllNewestFirst() async throws {
        let cards = try await data().search("")
        XCTAssertEqual(cards.map(\.eid), [50, 43, 42])
    }

    func testDetailFromReplica() async throws {
        let row = try await data().detail(43)
        XCTAssertEqual(row?.editionId, 43)
        XCTAssertEqual(row?.translators, ["Wallace"])
        let missing = try await data().detail(999)
        XCTAssertNil(missing)
    }

    func testContentUnavailableOffline() async throws {
        let doc = try await data().content("emptiness")
        XCTAssertFalse(doc.available)
        XCTAssertTrue(doc.books.isEmpty)
    }

    func testBrowseGroupsBooksPeopleSubjects() async throws {
        // "buddhism" hits book metadata (subjects in the blob) AND the two subject names.
        let doc = try await data().browse("buddhism", only: nil)
        let byKey = Dictionary(uniqueKeysWithValues: doc.groups.map { ($0.key, $0) })
        XCTAssertEqual(byKey["editions"]?.count, 2)
        XCTAssertEqual(Set(byKey["subjects"]?.hits.map(\.label) ?? []), ["Buddhism/Emptiness", "Buddhism/Ethics"])
        // book hits carry an edition url → a ref resolves via refFromUrl
        let firstBookURL = byKey["editions"]?.hits.first?.url
        XCTAssertEqual(refFromUrl(firstBookURL), .edition(43))    // newest-first
    }

    func testBrowsePeopleMatch() async throws {
        let doc = try await data().browse("padmakara", only: nil)
        XCTAssertEqual(doc.groups.first(where: { $0.key == "people" })?.hits.map(\.label), ["Padmakara"])
    }

    func testOfflineFirstSelectsReplicaAndDisablesContent() async throws {
        let rep = try replica()
        let live = CatalogueAPI(baseURL: URL(string: "http://unused.invalid")!)   // never hit
        let sel = OfflineFirstData(live: live, replica: { rep }, isOffline: { true })
        let cards = try await sel.search("zhuang")
        XCTAssertEqual(cards.map(\.eid), [50])
        let content = try await sel.content("x")
        XCTAssertFalse(content.available)                       // offline → content unavailable, no network
        let row = try await sel.detail(42)
        XCTAssertEqual(row?.editionId, 42)                      // from replica, not the (invalid) live URL
    }

    func testReplicaStorePersistsAndReloads() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let rep = try replica()
        let store = ReplicaStore(directory: dir)
        await store.store(rep, etag: "\"v1\"")
        // a fresh store over the same directory reads it back from disk
        let reopened = ReplicaStore(directory: dir)
        let cached = await reopened.cached()
        XCTAssertEqual(cached?.count, 3)
        XCTAssertEqual(cached?.editions.map(\.editionId), [42, 43, 50])
    }
}
