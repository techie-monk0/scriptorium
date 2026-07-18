import XCTest
@testable import CatalogueReader
import CatalogueReaderWire
import Octavo

/// Drives the outline editor's testable core through each scenario and asserts BOTH the resulting draft
/// AND the structured `outline.*` console lines it prints — so the editor's behavior is pinned even
/// though the SwiftUI sheet's rendering is only observable in the simulator.
final class OutlineEditorModelTests: XCTestCase {
    /// Collects the model's console lines (the same strings printed to stdout / the unified log).
    private final class Trace { var lines: [String] = [] }

    private func model() -> (OutlineEditorModel, Trace) {
        let t = Trace()
        return (OutlineEditorModel(trace: { t.lines.append($0) }), t)
    }
    private func toc(_ title: String, page: Int, _ kids: [TocItem] = []) -> TocItem {
        TocItem(title: title,
                locator: Locator(publicationId: "holding:7", format: .pdf, locations: .init(page: page)),
                children: kids)
    }

    // ── open: which source seeds the draft ────────────────────────────────────
    func testOpenPrefersAuthored() {
        let (m, t) = model()
        m.open(authored: [OutlineEntry(level: 1, title: "Mine", page: 3)],
               embedded: [toc("Embedded", page: 1)])
        XCTAssertEqual(m.entries.map(\.title), ["Mine"])
        XCTAssertEqual(t.lines, ["outline.open source=authored authored=1 embedded=1 draft=1"])
    }

    func testOpenFallsBackToEmbedded() {
        let (m, t) = model()
        m.open(authored: [], embedded: [toc("Part I", page: 1, [toc("Ch 1", page: 2)])])
        XCTAssertEqual(m.entries.map(\.title), ["Part I", "Ch 1"])
        XCTAssertEqual(t.lines, ["outline.open source=embedded authored=0 embedded=1 draft=2"])
    }

    func testOpenEmptyWhenNeither() {
        let (m, t) = model()
        m.open(authored: [], embedded: [])
        XCTAssertTrue(m.entries.isEmpty)
        XCTAssertEqual(t.lines, ["outline.open source=empty authored=0 embedded=0 draft=0"])
    }

    // ── edit operations ───────────────────────────────────────────────────────
    func testAddDeleteMoveEmitLines() {
        let (m, t) = model()
        m.add(atPage: 5)
        m.add(atPage: 9)
        m.move(from: IndexSet(integer: 1), to: 0)
        m.delete(at: IndexSet(integer: 0))
        XCTAssertEqual(m.entries.count, 1)
        XCTAssertEqual(t.lines, ["outline.add page=5 total=1",
                                 "outline.add page=9 total=2",
                                 "outline.move total=2",
                                 "outline.delete total=1"])
    }

    func testAddClampsPageAndSettersGuardBounds() {
        let (m, _) = model()
        m.add(atPage: 0)                      // clamps to 1
        XCTAssertEqual(m.entries.first?.page, 1)
        m.setPage(-4, at: 0)                  // clamps to 1
        XCTAssertEqual(m.entries.first?.page, 1)
        m.setTitle("Hi", at: 0); m.setTitle("nope", at: 99)   // out-of-range is a no-op, not a crash
        XCTAssertEqual(m.entries.first?.title, "Hi")
    }

    // ── save: blank rows dropped ──────────────────────────────────────────────
    func testCleanedDropsBlankTitlesAndReportsCounts() {
        let (m, t) = model()
        m.open(authored: [OutlineEntry(level: 1, title: "Keep", page: 1),
                          OutlineEntry(level: 1, title: "   ", page: 2),
                          OutlineEntry(level: 1, title: "", page: 3)], embedded: [])
        let kept = m.cleaned()
        XCTAssertEqual(kept.map(\.title), ["Keep"])
        XCTAssertEqual(t.lines.last, "outline.save entries=1 dropped=2")
    }

    // ── bake outcome classification ───────────────────────────────────────────
    func testBakeResultClassifiesStatuses() {
        let (m, t) = model()
        XCTAssertEqual(m.bakeResult(status: 200, bytes: 4096), "ok")
        XCTAssertEqual(m.bakeResult(status: 409, bytes: 0), "empty")
        XCTAssertEqual(m.bakeResult(status: 403, bytes: 0), "forbidden")
        XCTAssertEqual(m.bakeResult(status: 500, bytes: 0), "error")
        XCTAssertEqual(t.lines, ["outline.bake status=200 bytes=4096 outcome=ok",
                                 "outline.bake status=409 bytes=0 outcome=empty",
                                 "outline.bake status=403 bytes=0 outcome=forbidden",
                                 "outline.bake status=500 bytes=0 outcome=error"])
    }
}
