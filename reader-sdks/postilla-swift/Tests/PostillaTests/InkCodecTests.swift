import XCTest
@testable import Postilla

/// PS-U2 — Ink / coord codec round-trip + byte-parity golden.
final class InkCodecTests: XCTestCase {

    /// The fixed golden JSON (sorted keys) the web binding must also produce.
    /// `points` are compact `[x, y, pressure]` arrays; coords are `0...1`.
    private let golden =
        ##"{"strokes":[{"color":"#ff0000","mode":"draw","points":[[0.1,0.2,0.5],[0.3,0.4,0.75]],"width":4}]}"##

    func testEncodeMatchesGolden() throws {
        let ink = Ink(strokes: [Fix.inkStroke()])
        let json = String(decoding: try ink.canonicalJSONData(), as: UTF8.self)
        XCTAssertEqual(json, golden)
    }

    func testDecodeFromGolden() throws {
        let ink = try Ink.from(jsonData: Data(golden.utf8))
        XCTAssertEqual(ink.strokes.count, 1)
        let s = ink.strokes[0]
        XCTAssertEqual(s.color, "#ff0000")
        XCTAssertEqual(s.mode, .draw)
        XCTAssertEqual(s.width, 4, accuracy: 1e-12)
        XCTAssertEqual(s.points[0], InkPoint(x: 0.1, y: 0.2, pressure: 0.5))
        XCTAssertEqual(s.points[1], InkPoint(x: 0.3, y: 0.4, pressure: 0.75))
    }

    /// Round-trip is lossless and coords stay in `0...1`.
    func testRoundTripLossless() throws {
        let original = Ink(strokes: [Fix.inkStroke()])
        let data = try original.canonicalJSONData()
        let back = try Ink.from(jsonData: data)
        XCTAssertEqual(original, back)
        XCTAssertTrue(back.isNormalized)
    }

    /// A pressure-less `[x, y]` point decodes with default full pressure.
    func testPressureOptionalOnWire() throws {
        let json = ##"{"strokes":[{"color":"#000","mode":"draw","points":[[0.5,0.5]],"width":2}]}"##
        let ink = try Ink.from(jsonData: Data(json.utf8))
        XCTAssertEqual(ink.strokes[0].points[0].pressure, 1.0)
    }

    /// A timed point adds a 4th slot `[x,y,pressure,t]` (ms from stroke start) —
    /// additive, so untimed ink (the golden above) is unchanged. Cross-binding
    /// contract for online HWR.
    private let timedGolden =
        ##"{"strokes":[{"color":"#ff0000","mode":"draw","points":[[0.1,0.2,0.5,0],[0.3,0.4,0.75,16]],"width":4}]}"##

    func testTimestampEncodesAsFourthSlot() throws {
        let stroke = InkStroke(
            points: [InkPoint(x: 0.1, y: 0.2, pressure: 0.5, t: 0),
                     InkPoint(x: 0.3, y: 0.4, pressure: 0.75, t: 16)],
            width: 4, color: "#ff0000", mode: .draw)
        let json = String(decoding: try Ink(strokes: [stroke]).canonicalJSONData(), as: UTF8.self)
        XCTAssertEqual(json, timedGolden)
    }

    func testTimestampRoundTrips() throws {
        let ink = try Ink.from(jsonData: Data(timedGolden.utf8))
        XCTAssertEqual(ink.strokes[0].points[0].t, 0)
        XCTAssertEqual(ink.strokes[0].points[1].t, 16)
        // Re-encode is byte-stable.
        XCTAssertEqual(String(decoding: try ink.canonicalJSONData(), as: UTF8.self), timedGolden)
    }

    /// Untimed ink (`t == nil`) must still encode as 3-slot points — the
    /// pre-timestamp golden is unchanged.
    func testUntimedStaysThreeSlots() throws {
        XCTAssertEqual(
            String(decoding: try Ink(strokes: [Fix.inkStroke()]).canonicalJSONData(), as: UTF8.self),
            golden)
    }
}
