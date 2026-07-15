import XCTest
@testable import CatalogueCore

// ── stub platform built from the SAME fixtures the Node generator used ─────────
private struct NavFixture: Codable { var items: [NavMenuItem]; var activeKey: String?; var ctx: ProtocolContext }
private struct HomeFixture: Codable { var recentIds: [Int]; var replica: Replica; var starredIds: [Int] }
private struct StarActionFixture: Codable { var action: String; var eid: Int? }
private struct ChromeCapsFixture: Codable {
    var ready, search, star, resizeText, zoom, reflow, markText, strike, note, draw, erase, annList, export: Bool?
}
private struct ChromeStateFixture: Codable { var reflow, draw: Bool? }
private struct ChromeInputFixture: Codable {
    var format: String; var caps: ChromeCapsFixture; var state: ChromeStateFixture?; var compact: Bool?
}
private struct SyncInputFixture: Codable {
    var online, syncing: Bool?; var pendingWrites: Int?; var exportedAt, lastError, lastCheckedAt: String?
}
private struct Fixtures: Codable {
    var prefs: [String: String]
    var search: [String: [Card]]
    var browse: [String: BrowseDoc]
    var content: [String: ContentResponse]
    var detail: [String: EditionRow]
    var home: HomeFixture
    var nav: NavFixture
    var wishlist: WishlistPayload
    var wishlistActions: [JSONValue]
    var wishlistAddResponses: [WishlistAddResponse]
    var starredActions: [StarActionFixture]
    var reflowInputs: [String]     // JSON `reflow_inputs` (decoder converts snake_case)
    var chromeInputs: [ChromeInputFixture]   // JSON `chrome_inputs`
    var syncInputs: [SyncInputFixture]       // JSON `sync_inputs`
}

private struct StubData: DataPort {
    let fx: Fixtures
    func search(_ q: String) async throws -> [Card] { fx.search[q] ?? [] }
    func content(_ q: String) async throws -> ContentResponse { fx.content[q] ?? ContentResponse(q: q, books: [], available: true) }
    func detail(_ eid: Int) async throws -> EditionRow? { fx.detail[String(eid)] }
    func browse(_ q: String, only: String?) async throws -> BrowseDoc { fx.browse[q] ?? BrowseDoc(groups: []) }
}
private final class StubPrefs: PrefsPort, @unchecked Sendable {
    var d: [String: String]
    init(_ d: [String: String]) { self.d = d }
    func get(_ k: String) -> String? { d[k] }
    func set(_ k: String, _ v: String) { d[k] = v }
    func remove(_ k: String) { d[k] = nil }
}
private struct StubPlatform: Platform {
    let data: DataPort; let nav: NavPort; let prefs: PrefsPort; let offline: Bool
    func isOffline() -> Bool { offline }
}

/// U2 — Tier-2 view-model parity. The Swift view-models, given the shared fixtures, must produce the
/// SAME output the real `library-core.js` produced (captured in `goldens.json` by the Node generator).
/// U5 — search-normalization parity (`fold`) against the same JS source of truth.
final class ViewModelParityTests: XCTestCase {
    private var fx: Fixtures!
    private var goldens: JSONValue!
    private var platform: StubPlatform!

    override func setUpWithError() throws {
        fx = try CatalogueJSON.decode(Fixtures.self, from: resource("fixtures"))
        goldens = try CatalogueJSON.decode(JSONValue.self, from: resource("goldens"))
        platform = StubPlatform(data: StubData(fx: fx), nav: NativeNav(), prefs: StubPrefs(fx.prefs), offline: false)
    }

    private func resource(_ name: String) throws -> Data {
        let url = try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Goldens"))
        return try Data(contentsOf: url)
    }

    private func assertRef(_ ref: Ref?, _ g: JSONValue?, _ msg: String = "", file: StaticString = #filePath, line: UInt = #line) {
        guard let g, !g.isNull else { XCTAssertNil(ref, msg, file: file, line: line); return }
        let kind = g["kind"]?.stringValue
        switch ref {
        case .edition(let id): XCTAssertEqual(kind, "edition", msg, file: file, line: line); XCTAssertEqual(id, g["id"]?.intValue, msg, file: file, line: line)
        case .work(let id): XCTAssertEqual(kind, "work", msg, file: file, line: line); XCTAssertEqual(id, g["id"]?.intValue, msg, file: file, line: line)
        case .person(let id): XCTAssertEqual(kind, "person", msg, file: file, line: line); XCTAssertEqual(id, g["id"]?.intValue, msg, file: file, line: line)
        case .subject(let id, _): XCTAssertEqual(kind, "subject", msg, file: file, line: line); XCTAssertEqual(id, g["id"]?.intValue, msg, file: file, line: line)
        case .url(let u): XCTAssertEqual(kind, "url", msg, file: file, line: line); XCTAssertEqual(u, g["url"]?.stringValue, msg, file: file, line: line)
        case nil: XCTFail("expected a ref matching \(g) \(msg)", file: file, line: line)
        }
    }

    func testFoldParity() throws {
        let folds = try XCTUnwrap(goldens["fold"])
        guard case .object(let pairs) = folds else { return XCTFail("fold goldens not an object") }
        for (input, expected) in pairs {
            XCTAssertEqual(fold(input), expected.stringValue, "fold(\(input))")
        }
    }

    func testReflowPageTextParity() throws {
        let g = try XCTUnwrap(goldens["reflowPageText"]?.arrayValue)
        for (i, input) in fx.reflowInputs.enumerated() {
            let expected = g[i].arrayValue?.compactMap(\.stringValue) ?? []
            XCTAssertEqual(reflowPageText(input), expected, "reflowPageText[\(i)]")
        }
    }

    func testSubjectSectionsParity() throws {
        let g = try XCTUnwrap(goldens["subjectSections"]?.arrayValue)
        let sections = subjectSections(LibraryCore.subjectVM(fx.home.replica, "Buddhism"))
        XCTAssertEqual(sections.count, g.count, "subjectSections count")
        for (i, s) in sections.enumerated() where i < g.count {
            XCTAssertEqual(s.type, g[i]["type"]?.stringValue, "section[\(i)].type")
            XCTAssertEqual(s.title, g[i]["title"]?.stringValue, "section[\(i)].title")
            XCTAssertEqual(s.subject, g[i]["subject"]?.stringValue, "section[\(i)].subject")
            XCTAssertEqual(s.cards.map(\.eid), g[i]["cards"]?.arrayValue?.compactMap { $0["eid"]?.intValue },
                           "section[\(i)].cards")
            XCTAssertEqual(s.crumbs.map(\.name), g[i]["crumbs"]?.arrayValue?.compactMap { $0["name"]?.stringValue },
                           "section[\(i)].crumbs")
        }
    }

    func testReaderChromeVMParity() throws {
        let g = try XCTUnwrap(goldens["readerChromeVM"]?.arrayValue)
        for (i, input) in fx.chromeInputs.enumerated() {
            let cp = input.caps
            let caps = ReaderCaps(ready: cp.ready ?? false, search: cp.search ?? false, star: cp.star ?? false,
                                  resizeText: cp.resizeText ?? false, zoom: cp.zoom ?? false, reflow: cp.reflow ?? false,
                                  markText: cp.markText ?? false, strike: cp.strike ?? false,
                                  note: cp.note ?? false, draw: cp.draw ?? false, erase: cp.erase ?? false,
                                  annList: cp.annList ?? false, export: cp.export ?? false)
            let controls = readerChromeVM(format: input.format, caps: caps,
                                          reflow: input.state?.reflow ?? false, draw: input.state?.draw ?? false,
                                          compact: input.compact ?? false)
            let expected = g[i].arrayValue ?? []
            XCTAssertEqual(controls.count, expected.count, "chrome[\(i)] count")
            for (j, ctl) in controls.enumerated() where j < expected.count {
                XCTAssertEqual(ctl.id, expected[j]["id"]?.stringValue, "chrome[\(i)][\(j)].id")
                XCTAssertEqual(ctl.bar, expected[j]["bar"]?.stringValue, "chrome[\(i)][\(j)].bar")
                XCTAssertEqual(ctl.overflow, expected[j]["overflow"]?.boolValue, "chrome[\(i)][\(j)].overflow")
                XCTAssertEqual(ctl.active, expected[j]["active"]?.boolValue, "chrome[\(i)][\(j)].active")
                XCTAssertEqual(ctl.selectionAction, expected[j]["selectionAction"]?.boolValue,
                               "chrome[\(i)][\(j)].selectionAction")
            }
        }
    }

    func testSyncVMParity() throws {
        // Freshness chip parity: the shared `syncVM` must map each SyncState to the SAME spec the JS
        // `syncVM` produced (label/tone/detail/canPull), so every surface's status chip reads identically.
        let g = try XCTUnwrap(goldens["syncVM"]?.arrayValue)
        for (i, input) in fx.syncInputs.enumerated() {
            let s = SyncState(online: input.online ?? false, syncing: input.syncing ?? false,
                              lastError: input.lastError, exportedAt: input.exportedAt,
                              lastCheckedAt: input.lastCheckedAt, pendingWrites: input.pendingWrites ?? 0)
            let vm = syncVM(s)
            XCTAssertEqual(vm.state, g[i]["state"]?.stringValue, "sync[\(i)].state")
            XCTAssertEqual(vm.label, g[i]["label"]?.stringValue, "sync[\(i)].label")
            XCTAssertEqual(vm.tone, g[i]["tone"]?.stringValue, "sync[\(i)].tone")
            XCTAssertEqual(vm.detail, g[i]["detail"]?.stringValue, "sync[\(i)].detail")
            XCTAssertEqual(vm.canPull, g[i]["canPull"]?.boolValue, "sync[\(i)].canPull")
        }
    }

    func testSearchVMParity() async throws {
        let vm = await LibraryCore.searchVM(platform, "bodhi")
        let g = try XCTUnwrap(goldens["searchVM"]?["bodhi"])
        XCTAssertEqual(vm.q, g["q"]?.stringValue)
        XCTAssertEqual(vm.empty, g["empty"]?.boolValue)
        XCTAssertEqual(vm.cards.count, g["cards"]?.arrayValue?.count)
        XCTAssertEqual(vm.cards[0].eid, g["cards"]?[0]?["eid"]?.intValue)
        XCTAssertEqual(vm.cards[0].by, g["cards"]?[0]?["by"]?.stringValue)
        XCTAssertEqual(vm.cards[0].displayTitle, g["cards"]?[0]?["display_title"]?.stringValue)
        XCTAssertEqual(vm.cards[0].coverUrl, g["cards"]?[0]?["cover_url"]?.stringValue)

        let empty = await LibraryCore.searchVM(platform, "")
        XCTAssertTrue(empty.empty)
        XCTAssertEqual(empty.empty, goldens["searchVM"]?["empty"]?["empty"]?.boolValue)
    }

    func testBrowseVMParity() async throws {
        let vm = await LibraryCore.browseVM(platform, "shanti", only: nil)
        let g = try XCTUnwrap(goldens["browseVM"]?["shanti"])
        XCTAssertEqual(vm.empty, g["empty"]?.boolValue)
        XCTAssertEqual(vm.groups.count, g["groups"]?.arrayValue?.count)
        // group 0 — people, ref derived from /person/9
        XCTAssertEqual(vm.groups[0].label, g["groups"]?[0]?["label"]?.stringValue)
        XCTAssertEqual(vm.groups[0].labelPlural, g["groups"]?[0]?["labelPlural"]?.stringValue)
        XCTAssertEqual(vm.groups[0].count, g["groups"]?[0]?["count"]?.intValue)
        XCTAssertEqual(vm.groups[0].hits[0].type, g["groups"]?[0]?["hits"]?[0]?["type"]?.stringValue)
        XCTAssertEqual(vm.groups[0].hits[0].sublabel, g["groups"]?[0]?["hits"]?[0]?["sublabel"]?.stringValue)
        assertRef(vm.groups[0].hits[0].ref, g["groups"]?[0]?["hits"]?[0]?["ref"], "person hit")
        // group 1 — books; labelPlural falls back to label; count = hits.length; refs from urls
        XCTAssertEqual(vm.groups[1].labelPlural, g["groups"]?[1]?["labelPlural"]?.stringValue)  // "Book"
        XCTAssertEqual(vm.groups[1].count, g["groups"]?[1]?["count"]?.intValue)                 // 2
        assertRef(vm.groups[1].hits[0].ref, g["groups"]?[1]?["hits"]?[0]?["ref"], "edition 42")
        assertRef(vm.groups[1].hits[1].ref, g["groups"]?[1]?["hits"]?[1]?["ref"], "edition 43")
    }

    func testContentVMParity() async throws {
        let vm = await LibraryCore.contentVM(platform, "emptiness")
        let g = try XCTUnwrap(goldens["contentVM"]?["emptiness"])
        XCTAssertEqual(vm.available, g["available"]?.boolValue)
        XCTAssertEqual(vm.books.count, g["books"]?.arrayValue?.count)
        XCTAssertEqual(vm.books[0].eid, g["books"]?[0]?["eid"]?.intValue)
        XCTAssertEqual(vm.books[0].snippets, g["books"]?[0]?["snippets"]?.arrayValue?.compactMap(\.stringValue))
        assertRef(vm.books[0].ref, g["books"]?[0]?["ref"], "content edition ref")
    }

    func testDetailVMParity() async throws {
        let vm = await LibraryCore.detailVM(platform, 42)
        let g = try XCTUnwrap(goldens["detailVM"]?["42"])
        XCTAssertEqual(vm.title, g["title"]?.stringValue)
        XCTAssertEqual(vm.by, g["by"]?.stringValue)               // single author, no "no author"
        XCTAssertEqual(vm.workTitles, g["workTitles"]?.arrayValue?.compactMap(\.stringValue))
        // Connections — other editions of the contained works (FRBR siblings)
        XCTAssertEqual(vm.connections.map(\.eid), g["connections"]?.arrayValue?.compactMap { $0["eid"]?.intValue })
        XCTAssertEqual(vm.connections.first?.title, g["connections"]?[0]?["title"]?.stringValue)
        XCTAssertEqual(vm.coverUrl, g["coverUrl"]?.stringValue)
        XCTAssertEqual(vm.tradition, g["tradition"]?.stringValue)   // edition's Buddhist tradition
        XCTAssertEqual(vm.translators, g["translators"]?.arrayValue?.compactMap(\.stringValue))
        // holdings filtered to has_file only → the epub copy (holding 8) is dropped
        XCTAssertEqual(vm.holdings.count, g["holdings"]?.arrayValue?.count)
        XCTAssertEqual(vm.holdings.count, 1)
        XCTAssertEqual(vm.holdings[0].holdingId, g["holdings"]?[0]?["holding_id"]?.intValue)
        assertRef(vm.ref, g["ref"], "detail edition ref")

        let missing = await LibraryCore.detailVM(platform, 999)
        XCTAssertTrue(missing.missing)
        XCTAssertEqual(missing.missing, goldens["detailVM"]?["999"]?["missing"]?.boolValue)
    }

    func testHomeVMParity() throws {
        let vm = LibraryCore.homeVM(fx.home.replica, recentIds: fx.home.recentIds, starredIds: fx.home.starredIds)
        let g = try XCTUnwrap(goldens["homeVM"])
        XCTAssertEqual(vm.empty, g["empty"]?.boolValue)
        let gr = try XCTUnwrap(g["rails"]?.arrayValue)
        XCTAssertEqual(vm.rails.count, gr.count)
        // rail order + kinds (recent, starred, subject×3 by fullness/protected, series)
        XCTAssertEqual(vm.rails.map(\.kind), gr.map { $0["kind"]?.stringValue })
        XCTAssertEqual(vm.rails.map(\.title), gr.map { $0["title"]?.stringValue })
        // The merged Recent rail: opened first (no badge) then newly-added ('New'); per-card `starred`
        // + `badge` match the JS golden exactly (the feature's core parity).
        let recent = try XCTUnwrap(vm.rails.first { $0.kind == "recent" })
        let grecent = try XCTUnwrap(gr.first { $0["kind"]?.stringValue == "recent" })
        XCTAssertEqual(recent.cards.map { $0.badge ?? "" },
                       grecent["cards"]?.arrayValue?.map { $0["badge"]?.stringValue ?? "" })
        XCTAssertEqual(recent.cards.map { $0.starred ?? false },
                       grecent["cards"]?.arrayValue?.map { $0["starred"]?.boolValue ?? false })
        // The Starred rail exists, in the curated order.
        let starred = try XCTUnwrap(vm.rails.first { $0.kind == "starred" })
        XCTAssertEqual(starred.cards.map(\.eid), fx.home.starredIds)
        // subject rails carry id + count; ordering: Buddhism(2) → History(1) → Uncategorized(protected)
        let subjects = vm.rails.filter { $0.kind == "subject" }
        XCTAssertEqual(subjects.map(\.title), ["Buddhism", "History", "Uncategorized"])
        XCTAssertEqual(subjects.map(\.id), [1, 2, 3])
        XCTAssertEqual(subjects.map(\.count), [2, 1, 1])
        // every rail's card eids match the JS golden, in order
        for (i, rail) in vm.rails.enumerated() {
            XCTAssertEqual(rail.cards.map(\.eid), gr[i]["cards"]?.arrayValue?.compactMap { $0["eid"]?.intValue } ?? [],
                           "rail \(rail.kind) cards")
        }
        // the Series rail: one set "A Series", volumes ordered 1,2,10 (numeric-aware) → eids 101,103,102
        let series = try XCTUnwrap(vm.rails.first { $0.kind == "series" })
        XCTAssertEqual(series.sets.map(\.name), ["A Series"])
        XCTAssertEqual(series.sets[0].cards.map(\.eid), [101, 103, 102])
        XCTAssertEqual(series.sets[0].count, 3)
        let gset0 = g["rails"]?[gr.count - 1]?["sets"]?[0]
        XCTAssertEqual(series.sets[0].cards.map(\.eid), gset0?["cards"]?.arrayValue?.compactMap { $0["eid"]?.intValue })
    }

    func testWishlistVMParity() throws {
        let vm = LibraryCore.wishlistVM(fx.wishlist)
        let g = try XCTUnwrap(goldens["wishlistVM"])
        XCTAssertEqual(vm.empty, g["empty"]?.boolValue)
        XCTAssertEqual(vm.count, g["count"]?.intValue)
        let gg = try XCTUnwrap(g["groups"]?.arrayValue)
        // Group order + kinds (ambiguous → unresolved → resolved → owned; acquired absent here).
        XCTAssertEqual(vm.groups.map(\.kind), gg.map { $0["kind"]?.stringValue })
        XCTAssertEqual(vm.groups.map(\.title), gg.map { $0["title"]?.stringValue })
        XCTAssertEqual(vm.groups.map(\.kind), ["ambiguous", "suspected", "unresolved", "resolved", "owned"])
        // Every group's card ids + badges + key fields match the JS golden, in order.
        for (i, grp) in vm.groups.enumerated() {
            let gcards = try XCTUnwrap(gg[i]["cards"]?.arrayValue)
            XCTAssertEqual(grp.cards.map(\.id), gcards.compactMap { $0["id"]?.intValue }, "group \(grp.kind) ids")
            XCTAssertEqual(grp.cards.map(\.badge), gcards.compactMap { $0["badge"]?.stringValue }, "group \(grp.kind) badges")
            XCTAssertEqual(grp.cards.map(\.title), gcards.compactMap { $0["title"]?.stringValue }, "group \(grp.kind) titles")
            XCTAssertEqual(grp.cards.map(\.coverUrl), gcards.map { $0["cover_url"]?.stringValue }, "group \(grp.kind) covers")
            XCTAssertEqual(grp.cards.map(\.candidateCount), gcards.compactMap { $0["candidate_count"]?.intValue }, "group \(grp.kind) candidate counts")
        }
        // The unresolved bare-ISBN item still gets a derived cover + "ISBN …" title (never dropped).
        let unresolved = try XCTUnwrap(vm.groups.first { $0.kind == "unresolved" })
        XCTAssertTrue(unresolved.cards.contains { $0.title == "ISBN 9999999999999" })
    }

    func testWishlistCommandParity() throws {
        // wishlistRequest: each shared intent → identical {method,path,body} as JS (no surface
        // hardcodes endpoints). wishlistAddMessage: each response → identical user text as JS.
        let greq = try XCTUnwrap(goldens["wishlistRequest"]?.arrayValue)
        for (i, a) in fx.wishlistActions.enumerated() {
            let req = LibraryCore.wishlistRequest(wishlistAction(from: a))
            XCTAssertEqual(req.method, greq[i]["method"]?.stringValue, "action \(i) method")
            XCTAssertEqual(req.path, greq[i]["path"]?.stringValue, "action \(i) path")
            let gbody = greq[i]["body"]
            if let body = req.body {
                for (k, v) in body { XCTAssertEqual(gbody?[k], v, "action \(i) body[\(k)]") }
                if case .object(let go)? = gbody { XCTAssertEqual(body.count, go.count, "action \(i) body size") }
            } else {
                XCTAssertTrue(gbody == nil || gbody!.isNull, "action \(i) expected no body")
            }
        }
        let gmsg = try XCTUnwrap(goldens["wishlistAddMessage"]?.arrayValue?.compactMap(\.stringValue))
        XCTAssertEqual(fx.wishlistAddResponses.map(LibraryCore.wishlistAddMessage), gmsg)
    }

    func testStarredCommandParity() throws {
        // starredRequest: each shared star intent → identical {method,path,body} as JS.
        let greq = try XCTUnwrap(goldens["starredRequest"]?.arrayValue)
        for (i, a) in fx.starredActions.enumerated() {
            let action: StarredAction
            switch a.action {
            case "star":   action = .star(eid: a.eid ?? 0)
            case "unstar": action = .unstar(eid: a.eid ?? 0)
            default:       action = .list
            }
            let req = LibraryCore.starredRequest(action)
            XCTAssertEqual(req.method, greq[i]["method"]?.stringValue, "action \(i) method")
            XCTAssertEqual(req.path, greq[i]["path"]?.stringValue, "action \(i) path")
            if let body = req.body {
                for (k, v) in body { XCTAssertEqual(greq[i]["body"]?[k], v, "action \(i) body[\(k)]") }
            } else {
                let gbody = greq[i]["body"]
                XCTAssertTrue(gbody == nil || gbody!.isNull, "action \(i) expected no body")
            }
        }
    }

    private func wishlistAction(from a: JSONValue) -> WishlistAction {
        switch a["action"]?.stringValue {
        case "add":
            if case .object(let o)? = a["body"] { return .add(body: o) }
            return .add(body: [:])
        case "remove":  return .remove(id: a["id"]?.intValue ?? 0)
        case "pick":    return .pick(id: a["id"]?.intValue ?? 0, index: a["index"]?.intValue ?? 0)
        case "confirm": return .confirm(id: a["id"]?.intValue ?? 0, editionId: a["editionId"]?.intValue ?? 0)
        case "decline": return .decline(id: a["id"]?.intValue ?? 0)
        default:        return .list
        }
    }

    func testNameKeyParity() throws {
        let inputs = ["14th Dalai Lama", "Fourteenth Dalai Lama", "Dalai Lama XIV", "XIV", "Volume 14", "café"]
        let g = try XCTUnwrap(goldens["nameKey"]?.arrayValue?.compactMap(\.stringValue))
        XCTAssertEqual(inputs.map { nameKey($0) }, g)        // JS == Swift, per input
        // The three regnal-ordinal forms reduce to the same key SET (term-AND match, order-independent).
        XCTAssertEqual(Set(nameKey("14th Dalai Lama").split(separator: " ")), Set(nameKey("Dalai Lama XIV").split(separator: " ")))
    }

    func testReplicaSearchBrowseParity() throws {
        let replica = fx.home.replica
        // Search — same matched eids (over search_text) as the JS matcher, in the same order.
        for key in ["auth", "zen", "none", "eid", "ordinal"] {
            let q = key == "auth" ? "Auth A" : (key == "zen" ? "zen" : (key == "eid" ? "102" : (key == "ordinal" ? "fourteenth dalai lama" : "zzzz")))
            let eids = LibraryCore.searchReplica(replica, q).map(\.eid)
            XCTAssertEqual(eids, goldens["searchReplica"]?[key]?.arrayValue?.compactMap { $0.intValue }, "searchReplica(\(q))")
        }
        // Suggest — same top-8 book suggestions (label + url) as JS.
        let sug = LibraryCore.suggestReplica(replica, "Auth A")
        let gsug = try XCTUnwrap(goldens["suggestReplica"]?["auth"]?.arrayValue)
        XCTAssertEqual(sug.map(\.label), gsug.map { $0["label"]?.stringValue })
        XCTAssertEqual(sug.map(\.url), gsug.map { $0["url"]?.stringValue })
        // Browse — same groups (keys/counts) and Books hits as JS.
        let doc = LibraryCore.browseReplica(replica, "auth", only: nil)
        let g = try XCTUnwrap(goldens["browseReplica"]?["auth"])
        XCTAssertEqual(doc.groups.map(\.key), g["groups"]?.arrayValue?.compactMap { $0["key"]?.stringValue })
        XCTAssertEqual(doc.groups.map(\.count), g["groups"]?.arrayValue?.compactMap { $0["count"]?.intValue })
        let books = try XCTUnwrap(doc.groups.first { $0.key == "editions" })
        XCTAssertEqual(books.hits.map(\.label), g["groups"]?[0]?["hits"]?.arrayValue?.compactMap { $0["label"]?.stringValue })
        // Subject mode → the hit carries a /subject/<id> url (mapped from subject_forest) for navigation.
        let subDoc = LibraryCore.browseReplica(replica, "zen", only: "subjects")
        XCTAssertEqual(subDoc.groups.first?.hits.first?.url,
                       goldens["browseReplica"]?["zenSubjects"]?["groups"]?[0]?["hits"]?[0]?["url"]?.stringValue)
        XCTAssertEqual(subDoc.groups.first?.hits.first?.url, "/subject/4")
        // Work mode → matches a work by an ALIAS spelling not in its display title, shows the title.
        let workDoc = LibraryCore.browseReplica(replica, "alttitle", only: "works")
        XCTAssertEqual(workDoc.groups.first?.hits.map(\.label),
                       goldens["browseReplica"]?["workAlias"]?["groups"]?[0]?["hits"]?.arrayValue?.compactMap { $0["label"]?.stringValue })
        XCTAssertEqual(workDoc.groups.first?.hits.first?.label, "Bodhisattva Way")
    }

    func testSubjectVMParity() throws {
        let vm = LibraryCore.subjectVM(fx.home.replica, "Buddhism")
        let g = try XCTUnwrap(goldens["subjectVM"]?["buddhism"])
        XCTAssertEqual(vm.name, g["name"]?.stringValue)
        XCTAssertEqual(vm.count, g["count"]?.intValue)
        XCTAssertEqual(vm.crumbs.map(\.label), g["crumbs"]?.arrayValue?.compactMap { $0["label"]?.stringValue })
        XCTAssertEqual(vm.children.map(\.name), g["children"]?.arrayValue?.compactMap { $0["name"]?.stringValue })
        XCTAssertEqual(vm.children.first?.books.map(\.eid),
                       g["children"]?[0]?["books"]?.arrayValue?.compactMap { $0["eid"]?.intValue })
        XCTAssertEqual(vm.books.map(\.eid), g["books"]?.arrayValue?.compactMap { $0["eid"]?.intValue })
    }

    func testSettingsVMParity() throws {
        let vm = LibraryCore.settingsVM(platform)
        let g = try XCTUnwrap(goldens["settingsVM"])
        XCTAssertEqual(vm.theme, g["theme"]?.stringValue)           // "dark" from prefs
        XCTAssertEqual(vm.shelfArt, g["shelfArt"]?.stringValue)     // "spine" from prefs
        XCTAssertEqual(vm.seriesCoverStyle, g["seriesCoverStyle"]?.stringValue)  // "fan" (default, no pref)
        XCTAssertEqual(vm.shelfTitles, g["shelfTitles"]?.boolValue)              // false (default, no pref)
        XCTAssertEqual(vm.themeOptions.count, g["themeOptions"]?.arrayValue?.count)
        XCTAssertEqual(vm.shelfOptions.count, g["shelfOptions"]?.arrayValue?.count)
        XCTAssertEqual(vm.seriesCoverStyles.map(\.key), g["seriesCoverStyles"]?.arrayValue?.compactMap { $0["key"]?.stringValue })
    }

    func testAppContractParity() throws {
        // Nav sections — keys/labels/icons/protocol/order identical to the JS manifest.
        let gs = try XCTUnwrap(goldens["appSections"]?.arrayValue)
        XCTAssertEqual(APP_SECTIONS.map(\.key), gs.map { $0["key"]?.stringValue })
        XCTAssertEqual(APP_SECTIONS.map(\.label), gs.map { $0["label"]?.stringValue })
        XCTAssertEqual(APP_SECTIONS.map(\.icon), gs.map { $0["icon"]?.stringValue })
        XCTAssertEqual(APP_SECTIONS.map(\.protocol), gs.map { $0["protocol"]?.stringValue })
        XCTAssertEqual(sectionFor("books")?.label, "Books")        // the rename is in the shared layer
        XCTAssertEqual(sectionFor("search")?.label, "Search")
        // Cover contract — aspect, style keys/labels, box ratios, default.
        let cc = try XCTUnwrap(goldens["coverContract"])
        XCTAssertEqual(BOOK_COVER_ASPECT, cc["bookCoverAspect"]?.doubleValue)
        XCTAssertEqual(SERIES_COVER_DEFAULT, cc["seriesCoverDefault"]?.stringValue)
        let gstyles = try XCTUnwrap(cc["seriesCoverStyles"]?.arrayValue)
        XCTAssertEqual(SERIES_COVER_STYLES.map(\.key), gstyles.map { $0["key"]?.stringValue })
        XCTAssertEqual(SERIES_COVER_STYLES.map(\.label), gstyles.map { $0["label"]?.stringValue })
        XCTAssertEqual(SERIES_COVER_STYLES.map(\.wRatio), gstyles.map { $0["wRatio"]?.doubleValue })
        XCTAssertEqual(SERIES_COVER_STYLES.map(\.hRatio), gstyles.map { $0["hRatio"]?.doubleValue })
        // Search-screen component contract — search fields + detail-pane sections.
        let gf = try XCTUnwrap(goldens["searchFields"]?.arrayValue)
        XCTAssertEqual(SEARCH_FIELDS.map(\.key), gf.map { $0["key"]?.stringValue })
        XCTAssertEqual(SEARCH_FIELDS.map(\.label), gf.map { $0["label"]?.stringValue })
        XCTAssertEqual(SEARCH_FIELDS.map(\.suggest), gf.map { $0["suggest"]?.stringValue })
        let gd = try XCTUnwrap(goldens["bookDetailSections"]?.arrayValue)
        XCTAssertEqual(BOOK_DETAIL_SECTIONS.map(\.key), gd.map { $0["key"]?.stringValue })
        XCTAssertEqual(BOOK_DETAIL_SECTIONS.map(\.label), gd.map { $0["label"]?.stringValue })
    }

    func testNavVMParity() throws {
        let vm = LibraryCore.navVM(fx.nav.items, activeKey: fx.nav.activeKey, ctx: fx.nav.ctx)
        let g = try XCTUnwrap(goldens["navVM"])
        // the "scan" item (protocol: desktop) is filtered out under ctx {desktop:false} — same as JS
        XCTAssertEqual(vm.items.count, g["items"]?.arrayValue?.count)
        XCTAssertEqual(vm.items.count, 2)
        XCTAssertEqual(vm.items.map(\.key), ["home", "search"])
        XCTAssertEqual(vm.items[1].active, g["items"]?[1]?["active"]?.boolValue)   // search is active
        XCTAssertTrue(vm.items[1].active)
        XCTAssertFalse(vm.items[0].active)
    }
}
