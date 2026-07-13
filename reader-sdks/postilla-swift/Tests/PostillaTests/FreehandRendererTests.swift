import XCTest
import CoreGraphics
import PostillaRender
@testable import Postilla

/// FreehandRenderer determinism (the canonical-ink parity gate, CoreGraphics —
/// runs on macOS).
final class FreehandRendererTests: XCTestCase {

    private let canvas = CGSize(width: 100, height: 100)

    private func stroke() -> InkStroke {
        InkStroke(
            points: [
                InkPoint(x: 0.1, y: 0.1, pressure: 0.4),
                InkPoint(x: 0.5, y: 0.2, pressure: 0.8),
                InkPoint(x: 0.8, y: 0.6, pressure: 0.6),
            ],
            width: 6,
            color: "#222",
            mode: .draw
        )
    }

    /// Identical input → byte-identical outline (the property parity relies on).
    func testDeterministicOutline() {
        let a = FreehandRenderer.outline(for: stroke(), canvas: canvas)
        let b = FreehandRenderer.outline(for: stroke(), canvas: canvas)
        XCTAssertFalse(a.isEmpty)
        XCTAssertEqual(a, b)
    }

    /// Outline is a closed ring: left side forward + right side back = 2N points.
    func testOutlineIsClosedRing() {
        let s = stroke()
        let ring = FreehandRenderer.outline(for: s, canvas: canvas)
        XCTAssertEqual(ring.count, s.points.count * 2)
    }

    /// The path is non-empty and stays within (a margin around) the canvas.
    func testPathBoundsWithinCanvas() {
        let path = FreehandRenderer.path(for: stroke(), canvas: canvas)
        XCTAssertFalse(path.isEmpty)
        let box = path.boundingBoxOfPath
        // Allow a half-width margin past the edges for the stroke radius.
        let margin: CGFloat = 6
        XCTAssertGreaterThanOrEqual(box.minX, -margin)
        XCTAssertGreaterThanOrEqual(box.minY, -margin)
        XCTAssertLessThanOrEqual(box.maxX, canvas.width + margin)
        XCTAssertLessThanOrEqual(box.maxY, canvas.height + margin)
    }

    /// A single-sample stroke renders as a dot (non-empty ring).
    func testSinglePointRendersDot() {
        let dot = InkStroke(
            points: [InkPoint(x: 0.5, y: 0.5, pressure: 1)],
            width: 8, color: "#000", mode: .draw
        )
        let ring = FreehandRenderer.outline(for: dot, canvas: canvas)
        XCTAssertGreaterThan(ring.count, 3)
    }

    /// Scaling the canvas scales the outline coordinates (page-relative input).
    func testScalesWithCanvas() {
        let small = FreehandRenderer.outline(for: stroke(), canvas: CGSize(width: 100, height: 100))
        let big = FreehandRenderer.outline(for: stroke(), canvas: CGSize(width: 200, height: 200))
        // The first centerline-derived point should roughly double in x.
        XCTAssertGreaterThan(big[0].x, small[0].x)
    }
}
