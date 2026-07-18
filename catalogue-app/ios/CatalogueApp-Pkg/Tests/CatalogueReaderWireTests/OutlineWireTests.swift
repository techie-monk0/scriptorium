import XCTest
import Foundation
@testable import CatalogueReaderWire

/// Unit tests for the authored-outline wire mapping (`ReaderWireCodec` outline funcs + records). Pure,
/// so they run under `swift test`. Pins the `[OutlineEntry] ⇄ /sync/reader outline op/record` shape
/// (entries as a JSON string, `type:"outline"`, stable per-copy id) that the server contract (v2) expects.
final class OutlineWireTests: XCTestCase {
    private let entries = [OutlineEntry(level: 1, title: "Chapter One", page: 1),
                           OutlineEntry(level: 2, title: "Section 1.1", page: 2)]

    func testOutlineOpEncodesEntriesAsJSONStringAndDropsOtherFields() throws {
        let op = ReaderWireCodec.outlineOp(entries: entries, id: "outline:holding:7", holdingId: 7,
                                           updatedAt: Date(timeIntervalSince1970: 1))
        XCTAssertEqual(op.type, "outline")
        XCTAssertEqual(op.id, "outline:holding:7")
        XCTAssertEqual(op.holding_id, 7)
        XCTAssertNil(op.kind); XCTAssertNil(op.locator)      // not an annotation/bookmark op

        let enc = JSONEncoder(); enc.outputFormatting = [.withoutEscapingSlashes]
        let json = String(data: try enc.encode(ReaderPushRequest(ops: [op])), encoding: .utf8) ?? ""
        XCTAssertTrue(json.contains("\"type\":\"outline\""))
        XCTAssertTrue(json.contains("\"entries\":"))
        XCTAssertFalse(json.contains("\"kind\""))            // nil optionals dropped on encode
        XCTAssertFalse(json.contains("\"locator\""))
    }

    func testEntriesRoundTripThroughTheWire() throws {
        // op.entries (JSON string) decodes back to the same OutlineEntry list via a record
        let op = ReaderWireCodec.outlineOp(entries: entries, id: "x", holdingId: 7,
                                           updatedAt: Date(timeIntervalSince1970: 1))
        let record = OutlineRecord(id: "x", holding_id: 7, entries: op.entries,
                                   created_at: nil, updated_at: nil, deleted_at: nil, rev: 3)
        XCTAssertEqual(ReaderWireCodec.entries(from: record), entries)
    }

    func testTombstoneRecordYieldsNoEntries() {
        let record = OutlineRecord(id: "x", holding_id: 7,
                                   entries: ReaderWireCodec.entriesJSON(entries),
                                   created_at: nil, updated_at: nil,
                                   deleted_at: "2026-07-18T00:00:00Z", rev: 4)
        XCTAssertEqual(ReaderWireCodec.entries(from: record), [], "a tombstoned outline reads as empty")
    }

    func testBadEntriesPayloadIsEmptyNotACrash() {
        let record = OutlineRecord(id: "x", holding_id: 7, entries: "not json",
                                   created_at: nil, updated_at: nil, deleted_at: nil, rev: 1)
        XCTAssertEqual(ReaderWireCodec.entries(from: record), [])
    }
}
