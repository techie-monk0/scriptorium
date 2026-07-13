import Foundation
import CoreGraphics
import Postilla

/// The **canonical** ink renderer (`postilla.md` §4): a deterministic
/// perfect-freehand-style port. The same stored stroke renders pixel-stable on
/// web, on device, and in the flattened PDF — so we render via *this*, never via
/// PencilKit's native ink.
///
/// CoreGraphics-only (no UIKit), so it builds and is tested on macOS.
public enum FreehandRenderer {

    /// Stroke shaping options (perfect-freehand vocabulary).
    public struct Options: Sendable {
        /// Base diameter in canvas points before pressure thinning.
        public var size: Double
        /// `0...1` — how much pressure narrows the line (0 = constant width).
        public var thinning: Double
        /// `0...1` — input streamline (low-pass) applied to the raw points.
        public var streamline: Double

        public init(size: Double = 8, thinning: Double = 0.5, streamline: Double = 0.5) {
            self.size = size
            self.thinning = thinning
            self.streamline = streamline
        }
    }

    /// Build the filled outline (a closed ring of points, canvas-space) for a
    /// stroke. `canvas` maps the normalized `0...1` points into device points.
    ///
    /// Deterministic: identical input + options always yield the identical
    /// array — the property the parity gate relies on.
    public static func outline(
        for stroke: InkStroke,
        canvas: CGSize,
        options: Options = Options()
    ) -> [CGPoint] {
        let pts = streamlined(
            stroke.points.map {
                CGPoint(x: $0.x * canvas.width, y: $0.y * canvas.height)
            },
            pressures: stroke.points.map { $0.pressure },
            streamline: options.streamline
        )

        guard pts.count > 1 else {
            if let only = pts.first {
                return circle(center: only.point, radius: radius(options, pressure: only.pressure))
            }
            return []
        }

        var left: [CGPoint] = []
        var right: [CGPoint] = []
        for i in 0..<pts.count {
            let curr = pts[i].point
            // Forward direction (last point reuses the previous segment's).
            let ref = i < pts.count - 1 ? pts[i + 1].point : pts[i - 1].point
            var dx = (i < pts.count - 1) ? ref.x - curr.x : curr.x - ref.x
            var dy = (i < pts.count - 1) ? ref.y - curr.y : curr.y - ref.y
            let len = (dx * dx + dy * dy).squareRoot()
            if len > 0 { dx /= len; dy /= len } else { dx = 1; dy = 0 }
            // Perpendicular.
            let nx = -dy
            let ny = dx
            let r = radius(options, pressure: pts[i].pressure)
            left.append(CGPoint(x: curr.x + nx * r, y: curr.y + ny * r))
            right.append(CGPoint(x: curr.x - nx * r, y: curr.y - ny * r))
        }
        // Forward along the left, back along the right → closed outline.
        return left + right.reversed()
    }

    /// The outline as a closed `CGPath`, ready to fill.
    public static func path(
        for stroke: InkStroke,
        canvas: CGSize,
        options: Options = Options()
    ) -> CGPath {
        let ring = outline(for: stroke, canvas: canvas, options: options)
        let path = CGMutablePath()
        guard let first = ring.first else { return path }
        path.move(to: first)
        for p in ring.dropFirst() { path.addLine(to: p) }
        path.closeSubpath()
        return path
    }

    // MARK: - Internals

    private static func radius(_ options: Options, pressure: Double) -> Double {
        let p = min(max(pressure, 0), 1)
        // thinning=0 → constant; thinning=1 → fully pressure-scaled.
        let scale = (1 - options.thinning) + options.thinning * p
        return (options.size / 2) * scale
    }

    private struct Sample { var point: CGPoint; var pressure: Double }

    /// Exponential low-pass on the points (perfect-freehand's `streamline`).
    private static func streamlined(
        _ points: [CGPoint],
        pressures: [Double],
        streamline: Double
    ) -> [Sample] {
        guard !points.isEmpty else { return [] }
        let t = 0.15 + 0.85 * (1 - min(max(streamline, 0), 1))
        var out: [Sample] = [Sample(point: points[0], pressure: pressures[0])]
        for i in 1..<points.count {
            let prev = out[i - 1].point
            let curr = points[i]
            let x = prev.x + (curr.x - prev.x) * t
            let y = prev.y + (curr.y - prev.y) * t
            out.append(Sample(point: CGPoint(x: x, y: y), pressure: pressures[i]))
        }
        return out
    }

    /// A dot (single-sample stroke) as a 16-gon ring.
    private static func circle(center: CGPoint, radius r: Double) -> [CGPoint] {
        let segments = 16
        return (0..<segments).map { i in
            let a = (Double(i) / Double(segments)) * 2 * Double.pi
            return CGPoint(x: center.x + cos(a) * r, y: center.y + sin(a) * r)
        }
    }
}
