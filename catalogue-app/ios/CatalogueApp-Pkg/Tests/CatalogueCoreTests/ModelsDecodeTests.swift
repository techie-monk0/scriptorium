import XCTest
@testable import CatalogueCore

/// U1 — Codable contract decode. Decode representative `/api/v1` + replica JSON (real shapes traced to
/// routes/api.py + services/*) into the Swift models and assert fields populate, unknown fields are
/// tolerated, and missing optionals don't throw. Guards drift between server JSON and Swift structs.
final class ModelsDecodeTests: XCTestCase {
    private func decode<T: Decodable>(_ t: T.Type, _ json: String) throws -> T {
        try CatalogueJSON.decode(t, from: Data(json.utf8))
    }

    func testHealthDecodes() throws {
        let h = try decode(Health.self, #"{"ok":true,"service":"catalogue","api":1,"role":"editor","can_edit":true,"can_download":false}"#)
        XCTAssertTrue(h.ok)
        XCTAssertEqual(h.role, "editor")
        XCTAssertEqual(h.canEdit, true)
        XCTAssertEqual(h.canDownload, false)
    }

    func testLibraryRowsDecodeWithSnakeCase() throws {
        let doc = try decode(LibraryResponse.self, #"""
        {"q":"bodhi","rows":[
          {"id":42,"title":"Bodhicaryāvatāra","display_title":"Bodhicaryāvatāra","subtitle":"Śāntideva · 1w · reviewed","done":true,"holding_id":7,"has_file":true,"file_ext":"pdf"}
        ]}
        """#)
        XCTAssertEqual(doc.rows.count, 1)
        let r = doc.rows[0]
        XCTAssertEqual(r.id, 42)
        XCTAssertEqual(r.displayTitle, "Bodhicaryāvatāra")
        XCTAssertEqual(r.holdingId, 7)
        XCTAssertTrue(r.hasFile)
        XCTAssertEqual(r.fileExt, "pdf")
    }

    func testLibraryRowToleratesMissingOptionals() throws {
        // Only the required id/title/has_file present — everything else absent.
        let doc = try decode(LibraryResponse.self, #"{"rows":[{"id":9,"title":"x","has_file":false}]}"#)
        let r = doc.rows[0]
        XCTAssertNil(r.displayTitle)
        XCTAssertNil(r.subtitle)
        XCTAssertNil(r.holdingId)
        XCTAssertNil(r.fileExt)
        XCTAssertFalse(r.hasFile)
        XCTAssertNil(doc.q)
    }

    func testEditionRowDecodesAndToleratesUnknownFields() throws {
        let row = try decode(EditionRow.self, #"""
        {"edition_id":42,"title":"Bodhicaryāvatāra","display_title":"Bodhicaryāvatāra","subtitle":null,
         "volume":null,"publisher":"Vidyā","year":1998,"cover_url":"/edition/42/cover.jpg",
         "spine_url":"/edition/42/spine.svg","authors":["Śāntideva"],"translators":["Crosby"],
         "isbns":["9780192835802"],"subjects":["Buddhism/Emptiness"],"work_titles":["Bodhicaryāvatāra"],
         "holdings":[{"holding_id":7,"format":"pdf","kind":"pdf","has_file":true,"storage":{"provider":"kdrive","path":"/x.pdf"}}],
         "search_text":"bodhicaryavatara santideva","a_future_field_we_dont_model":123}
        """#)
        XCTAssertEqual(row.editionId, 42)
        XCTAssertEqual(row.year, 1998)
        XCTAssertEqual(row.authors, ["Śāntideva"])
        XCTAssertEqual(row.subjects, ["Buddhism/Emptiness"])
        XCTAssertEqual(row.holdings.count, 1)
        XCTAssertEqual(row.holdings[0].kind, "pdf")
        XCTAssertTrue(row.holdings[0].hasFile)
        XCTAssertNotNil(row.holdings[0].storage)   // opaque, but present
        XCTAssertNil(row.subtitle)                 // explicit null → nil
    }

    func testReplicaDocDecodes() throws {
        let rep = try decode(Replica.self, #"""
        {"schema_version":3,"exported_at":"2026-06-27T00:00:00+00:00","provider":"kdrive","count":1,
         "editions":[{"edition_id":1,"title":"t","authors":[],"translators":[],"isbns":[],"subjects":[],
                      "work_titles":[],"holdings":[]}]}
        """#)
        XCTAssertEqual(rep.schemaVersion, 3)
        XCTAssertEqual(rep.count, 1)
        XCTAssertEqual(rep.editions.first?.editionId, 1)
    }

    func testContentResponseDecodes() throws {
        let doc = try decode(ContentResponse.self, #"""
        {"q":"emptiness","books":[{"eid":42,"title":"Bodhicaryāvatāra","authors":["Śāntideva"],
         "snippets":["…the [match]emptiness[/match] of…"]}],"available":true}
        """#)
        XCTAssertEqual(doc.books.first?.eid, 42)
        XCTAssertEqual(doc.books.first?.snippets.count, 1)
        XCTAssertTrue(doc.available)
    }

    func testSubjectsAndSubjectPageDecode() throws {
        let subs = try decode(SubjectsResponse.self, #"""
        {"kind":"topic","tree":[{"id":1,"name":"Buddhism","leaf_label":"Buddhism","depth":0,
          "parent_id":null,"has_children":true,"is_protected":false,"n_works":3,"n_editions":5,
          "n_books_direct":2,"n_books_total":5}]}
        """#)
        XCTAssertEqual(subs.tree.first?.name, "Buddhism")
        XCTAssertEqual(subs.tree.first?.nBooksTotal, 5)
        XCTAssertNil(subs.tree.first?.parentId)

        let page = try decode(SubjectPage.self, #"""
        {"subject":{"id":1,"name":"Buddhism","kind":"topic","leaf_label":"Buddhism"},"crumbs":[],
         "children":[],"n_books":1,"books":[{"eid":42,"title":"Bodhicaryāvatāra",
          "display_title":"Bodhicaryāvatāra","by":"Śāntideva","holding_id":7,"has_file":true,
          "cover_url":"/edition/42/cover.jpg","spine_url":"/edition/42/spine.svg"}]}
        """#)
        XCTAssertEqual(page.subject.name, "Buddhism")
        XCTAssertEqual(page.books.first?.by, "Śāntideva")
        XCTAssertEqual(page.nBooks, 1)
    }

    func testEncodeDecodeRoundTripForCleanModel() throws {
        // ContentResponse has no opaque fields → exact round-trip through the snake_case coders.
        let original = ContentResponse(q: "x", books: [ContentBook(eid: 1, title: "t", authors: ["a"], snippets: ["s"])], available: true)
        let data = try CatalogueJSON.encoder.encode(original)
        let back = try CatalogueJSON.decode(ContentResponse.self, from: data)
        XCTAssertEqual(original, back)
    }
}
