import XCTest
import Foundation
@testable import CatalogueReaderWire
import Postilla

/// The neutral reader-wire layer's contract tests. `ReaderWireCodec` is pure, so these run under
/// `swift test`; the goldens (`Goldens/reader-wire-goldens.json`) are the cross-binding contract a
/// non-iOS frontend (Kotlin/JS) must also satisfy.
final class ReaderWireCodecTests: XCTestCase {

    private func goldens() throws -> [String: Any] {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "reader-wire-goldens", withExtension: "json", subdirectory: "Goldens"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    }

    private func pub(_ input: [String: Any]) -> String { "holding:\(input["holdingId"] as? Int ?? 0)" }
    private func date(_ input: [String: Any], _ key: String) -> Date {
        Date(timeIntervalSince1970: (input[key] as? NSNumber)?.doubleValue ?? 0)
    }

    // MARK: push ops (model → wire)

    func testPushOpsMatchGoldens() throws {
        let cases = try XCTUnwrap(goldens()["pushOps"] as? [[String: Any]])
        for c in cases {
            let name = c["name"] as? String ?? "?"
            let input = try XCTUnwrap(c["input"] as? [String: Any])
            let expected = try XCTUnwrap(c["expectedOp"] as? [String: Any])
            let hid = input["holdingId"] as? Int

            let op: ReaderWireOp
            if (input["kind"] as? String) == "bookmark" {
                op = ReaderWireCodec.op(from: bookmark(from: input), holdingId: hid)
            } else {
                op = ReaderWireCodec.op(from: annotation(from: input), holdingId: hid)
            }

            let enc = JSONEncoder(); enc.outputFormatting = [.withoutEscapingSlashes]
            let dict = try XCTUnwrap(JSONSerialization.jsonObject(with: enc.encode(op)) as? [String: Any])
            XCTAssertEqual(NSDictionary(dictionary: dict), NSDictionary(dictionary: expected), "op — \(name)")
        }
    }

    // MARK: pull records (wire → model)

    func testPullAnnotationsMatchGoldens() throws {
        let cases = try XCTUnwrap(goldens()["pullAnnotations"] as? [[String: Any]])
        for c in cases {
            let name = c["name"] as? String ?? "?"
            let recordJSON = try JSONSerialization.data(withJSONObject: try XCTUnwrap(c["record"]))
            let record = try JSONDecoder().decode(AnnotationRecord.self, from: recordJSON)
            let a = try XCTUnwrap(ReaderWireCodec.annotation(from: record, publicationId: "holding:7"), name)
            let exp = try XCTUnwrap(c["expectedModel"] as? [String: Any])

            XCTAssertEqual(a.kind.rawValue, exp["kind"] as? String, name)
            XCTAssertEqual(a.locator.locations.page, exp["page"] as? Int, name)
            XCTAssertEqual(a.locator.format.rawValue, exp["format"] as? String, name)
            XCTAssertEqual(a.quads, quadsValue(exp["quads"]), name)
            XCTAssertEqual(a.noteText, exp["noteText"] as? String, name)
            XCTAssertEqual(a.rev, exp["rev"] as? Int, name)
        }
    }

    // MARK: position round-trip + contract (pure)

    func testPositionRoundTrips() {
        let loc = Locator(publicationId: "holding:7", format: .pdf, locations: .init(page: 12))
        let rec = ReaderWireCodec.positionRecord(locator: loc, fraction: 0.4)
        XCTAssertEqual(rec.locator, "12")
        XCTAssertEqual(rec.fraction, 0.4)
        let back = ReaderWireCodec.position(from: rec, publicationId: "holding:7")
        XCTAssertEqual(back.locator?.locations.page, 12)
        XCTAssertEqual(back.fraction, 0.4)
    }

    func testHoldingIdParsing() {
        XCTAssertEqual(ReaderWireCodec.holdingId(from: "holding:7"), 7)
        XCTAssertEqual(ReaderWireCodec.holdingId(from: "42"), 42)
        XCTAssertNil(ReaderWireCodec.holdingId(from: "holding:abc"))
    }

    func testContractVersion() {
        XCTAssertEqual(ReaderSyncContract.builtFor, 1)
        XCTAssertTrue(ReaderSyncContract.compatible(1))
        XCTAssertTrue(ReaderSyncContract.compatible(2))
        XCTAssertFalse(ReaderSyncContract.compatible(nil))
        XCTAssertFalse(ReaderSyncContract.compatible(0))
    }

    // MARK: builders

    /// Decode a `[[Double]]` from a JSON `Any` (array of arrays of numbers).
    private func quadsValue(_ any: Any?) -> [[Double]]? {
        (any as? [Any])?.map { ($0 as? [Any])?.compactMap { ($0 as? NSNumber)?.doubleValue } ?? [] }
    }

    private func annotation(from input: [String: Any]) -> Annotation {
        let id = UUID(uuidString: input["id"] as! String)!
        let loc = Locator(publicationId: pub(input), format: .pdf,
                          locations: .init(page: input["page"] as? Int))
        return Annotation(
            id: id, publicationId: pub(input),
            kind: AnnotationKind(rawValue: input["kind"] as! String)!, locator: loc,
            cfiRange: input["cfiRange"] as? String, quads: quadsValue(input["quads"]),
            region: (input["region"] as? [Any])?.compactMap { ($0 as? NSNumber)?.doubleValue },
            color: input["color"] as? String, noteText: input["noteText"] as? String,
            createdAt: date(input, "createdAt"), updatedAt: date(input, "updatedAt"), rev: 0)
    }

    private func bookmark(from input: [String: Any]) -> Bookmark {
        let id = UUID(uuidString: input["id"] as! String)!
        let loc = Locator(publicationId: pub(input), format: .pdf,
                          locations: .init(page: input["page"] as? Int))
        return Bookmark(
            id: id, publicationId: pub(input), locator: loc,
            fraction: (input["fraction"] as? NSNumber)?.doubleValue, label: input["label"] as? String,
            createdAt: date(input, "createdAt"), updatedAt: date(input, "updatedAt"), rev: 0)
    }
}
