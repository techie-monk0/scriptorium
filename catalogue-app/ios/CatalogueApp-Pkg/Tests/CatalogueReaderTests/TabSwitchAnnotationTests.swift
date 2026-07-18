import XCTest
#if canImport(UIKit)
import PDFKit
import CoreGraphics
import Octavo
import Postilla
import PostillaRender
@testable import CatalogueReader

/// Simulates a tab switch: a book's marks are pushed to the store (session 1), then a FRESH store +
/// render stack is built over the same file (the remount) and must re-pull and re-render them. This is
/// the exact chain a tab switch runs — `LocalAnnotationStore` (per-ReaderView, file-backed) →
/// `pull(since:0)` → `CompositeRenderLayer.render` → page annotations — so a "highlight/ink didn't stick
/// on tab switch" bug reproduces here in CI instead of only on-device.
@MainActor
final class TabSwitchAnnotationTests: XCTestCase {

    private func tempDir() -> URL {
        let d = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }
    private func annFile(_ dir: URL) -> URL { dir.appendingPathComponent("annotations.json") }

    private func makePdf(pages: Int) throws -> PDFDocument {
        let data = NSMutableData()
        guard let consumer = CGDataConsumer(data: data as CFMutableData) else { throw XCTSkip("no consumer") }
        var box = CGRect(x: 0, y: 0, width: 300, height: 400)
        guard let ctx = CGContext(consumer: consumer, mediaBox: &box, nil) else { throw XCTSkip("no ctx") }
        for _ in 0..<pages { ctx.beginPDFPage(nil); ctx.endPDFPage() }
        ctx.closePDF()
        return try XCTUnwrap(PDFDocument(data: data as Data))
    }

    private func highlight(_ pub: String, page: Int, id: UUID = UUID()) -> Annotation {
        Annotation(id: id, publicationId: pub, kind: .highlight,
                   locator: Locator(publicationId: pub, format: .pdf, locations: .init(page: page)),
                   quads: [[0.1, 0.1, 0.6, 0.05]], color: "#ffd54a",
                   createdAt: Date(timeIntervalSince1970: 1), updatedAt: Date(timeIntervalSince1970: 1), rev: 1)
    }
    private func inkMark(_ pub: String, page: Int) -> Annotation {
        let stroke = InkStroke(points: [InkPoint(x: 0.2, y: 0.2), InkPoint(x: 0.5, y: 0.35)], width: 3, color: "#1565c0")
        return Annotation(publicationId: pub, kind: .ink,
                          locator: Locator(publicationId: pub, format: .pdf, locations: .init(page: page)),
                          ink: Ink(strokes: [stroke]),
                          createdAt: Date(timeIntervalSince1970: 1), updatedAt: Date(timeIntervalSince1970: 1), rev: 1)
    }

    /// Render a pulled set exactly as `renderMarks` does (drop notes; feed the layer), on a fresh retained
    /// host + PDFView — the remounted reader's view. Returns page-0 annotation count.
    private func renderPulled(_ ops: [Annotation], pages: Int) throws -> Int {
        let doc = try makePdf(pages: pages)
        let pdfView = PDFView(); pdfView.document = doc
        let host = PdfDecorationHost(pdfView: pdfView)
        let layer = CompositeRenderLayer(decorations: host, ink: PdfInkHost(pdfView: pdfView), inkPlacement: .fixedPage)
        layer.render(ops.filter { !$0.isTombstone && $0.kind != .note })
        defer { withExtendedLifetime(host) {} }
        return doc.page(at: 0)?.annotations.count ?? -1
    }

    // MARK: the actual "tab switch" simulations

    func testHighlightSurvivesRemount() async throws {
        let dir = tempDir(); let pub = "holding:7"
        // Session 1: make a highlight (LocalAnnotationStore.push, offline — no remote).
        let s1 = LocalAnnotationStore(fileURL: annFile(dir), remote: nil)
        _ = try await s1.push(publicationId: pub, ops: [highlight(pub, page: 1)])

        // Remount: a brand-new store over the same file re-pulls it (since:0).
        let s2 = LocalAnnotationStore(fileURL: annFile(dir), remote: nil)
        let pulled = try await s2.pull(publicationId: pub, since: 0)
        let live = pulled.ops.filter { !$0.isTombstone }
        XCTAssertEqual(live.count, 1, "highlight must survive the remount in the store")

        XCTAssertEqual(try renderPulled(pulled.ops, pages: 3), 1, "highlight must re-render on the remounted view")
    }

    func testHighlightAndInkSurviveRemount() async throws {
        let dir = tempDir(); let pub = "holding:8"
        let s1 = LocalAnnotationStore(fileURL: annFile(dir), remote: nil)
        _ = try await s1.push(publicationId: pub, ops: [highlight(pub, page: 1), inkMark(pub, page: 1)])

        let s2 = LocalAnnotationStore(fileURL: annFile(dir), remote: nil)
        let pulled = try await s2.pull(publicationId: pub, since: 0)
        XCTAssertEqual(pulled.ops.filter { !$0.isTombstone }.count, 2)
        // Both a text mark and ink land on page 1.
        XCTAssertEqual(try renderPulled(pulled.ops, pages: 3), 2, "highlight + ink must both re-render on remount")
    }

    /// Offline-first: a slow/unreachable server must NOT delay the render — `pull` returns local marks
    /// immediately and reconciles the remote in the background. (Regression for "marks only appear after
    /// the 45s poll's network call finally completes, minutes later".)
    func testPullReturnsLocalWithoutWaitingForSlowRemote() async throws {
        let dir = tempDir(); let pub = "holding:slow"
        // Seed a local mark via a nil-remote store over the same file.
        let seed = LocalAnnotationStore(fileURL: annFile(dir), remote: nil)
        _ = try await seed.push(publicationId: pub, ops: [highlight(pub, page: 1)])

        // A store whose remote hangs for a minute — pull must still return the local mark right away.
        let store = LocalAnnotationStore(fileURL: annFile(dir), remote: HangingRemote())
        let pulled = try await store.pull(publicationId: pub, since: 0)
        XCTAssertEqual(pulled.ops.filter { !$0.isTombstone }.count, 1, "local mark must render without the network")
    }

    /// A remote whose calls hang — to prove `pull` never awaits it.
    private actor HangingRemote: AnnotationStore {
        func pull(publicationId: String, since rev: Int) async throws -> PullResult {
            try? await Task.sleep(nanoseconds: 60_000_000_000)
            return PullResult(rev: 0, ops: [])
        }
        func push(publicationId: String, ops: [Annotation]) async throws -> PushResult {
            try? await Task.sleep(nanoseconds: 60_000_000_000)
            return PushResult(rev: 0, applied: [])
        }
    }

    func testUndoneHighlightStaysGoneAfterRemount() async throws {
        let dir = tempDir(); let pub = "holding:9"
        let id = UUID()
        let s1 = LocalAnnotationStore(fileURL: annFile(dir), remote: nil)
        _ = try await s1.push(publicationId: pub, ops: [highlight(pub, page: 1, id: id)])
        // Undo = tombstone (bumped rev) pushed.
        var tomb = highlight(pub, page: 1, id: id)
        tomb.deletedAt = Date(timeIntervalSince1970: 2); tomb.updatedAt = Date(timeIntervalSince1970: 2); tomb.rev = 2
        _ = try await s1.push(publicationId: pub, ops: [tomb])

        let s2 = LocalAnnotationStore(fileURL: annFile(dir), remote: nil)
        let pulled = try await s2.pull(publicationId: pub, since: 0)
        XCTAssertEqual(pulled.ops.filter { !$0.isTombstone }.count, 0, "undone highlight must stay gone")
        XCTAssertEqual(try renderPulled(pulled.ops, pages: 3), 0, "no annotation should render for a tombstoned mark")
    }
}
#endif
