import XCTest
import CoreGraphics
import PostillaRender
@testable import Postilla

/// `InkLayerRenderer` actually paints (the "canvas-actually-painted" guard,
/// postilla plan §5 PS-S2). CoreGraphics-only → runs on macOS via `swift test`.
final class InkLayerRendererTests: XCTestCase {

    private let size = CGSize(width: 64, height: 64)

    private func bitmap() -> CGContext {
        let cs = CGColorSpaceCreateDeviceRGB()
        let ctx = CGContext(
            data: nil,
            width: Int(size.width),
            height: Int(size.height),
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: cs,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )!
        ctx.clear(CGRect(origin: .zero, size: size))
        return ctx
    }

    /// Count pixels with any non-zero alpha (something was drawn).
    private func paintedPixels(_ ctx: CGContext) -> Int {
        guard let data = ctx.data else { return 0 }
        let bytesPerRow = ctx.bytesPerRow
        let ptr = data.bindMemory(to: UInt8.self, capacity: bytesPerRow * ctx.height)
        var painted = 0
        for y in 0..<ctx.height {
            for x in 0..<ctx.width {
                // premultipliedLast → RGBA; alpha is the 4th byte.
                if ptr[y * bytesPerRow + x * 4 + 3] != 0 { painted += 1 }
            }
        }
        return painted
    }

    private func diagonalStroke() -> InkStroke {
        InkStroke(
            points: [
                InkPoint(x: 0.1, y: 0.1, pressure: 0.6),
                InkPoint(x: 0.5, y: 0.5, pressure: 0.9),
                InkPoint(x: 0.9, y: 0.9, pressure: 0.5),
            ],
            width: 6, color: "#ff0000", mode: .draw
        )
    }

    func testEmptyStrokesLeaveCanvasClear() {
        let ctx = bitmap()
        InkLayerRenderer.draw([], in: size, into: ctx)
        XCTAssertEqual(paintedPixels(ctx), 0)
    }

    func testDrawStrokePaintsPixels() {
        let ctx = bitmap()
        InkLayerRenderer.draw([diagonalStroke()], in: size, into: ctx)
        XCTAssertGreaterThan(paintedPixels(ctx), 0)
    }

    /// A highlight (multiply @ 0.4α) paints too — the mode switch is exercised.
    func testHighlightModePaints() {
        let ctx = bitmap()
        var s = diagonalStroke()
        s.mode = .highlight
        InkLayerRenderer.draw([s], in: size, into: ctx)
        XCTAssertGreaterThan(paintedPixels(ctx), 0)
    }

    /// Deterministic: the same strokes paint the same pixel count twice.
    func testDeterministicPaintCount() {
        let a = bitmap(); InkLayerRenderer.draw([diagonalStroke()], in: size, into: a)
        let b = bitmap(); InkLayerRenderer.draw([diagonalStroke()], in: size, into: b)
        XCTAssertEqual(paintedPixels(a), paintedPixels(b))
    }
}
