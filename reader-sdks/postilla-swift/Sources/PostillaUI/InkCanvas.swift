import Foundation
import CoreGraphics
import Postilla

/// Pure capture math — available on every platform (no UIKit), so it's testable
/// on macOS. Converts a device-space sample `(location, force, bounds)` into a
/// normalized `0...1` `InkPoint` (the input layer of the canonical-ink rule).
public enum InkSampler {

    /// Normalize a captured point into page space. `force` is the raw pressure
    /// (e.g. `UITouch.force` or `PKStrokePoint.force`); `maxForce` scales it to
    /// `0...1` (pass `1` for already-normalized force).
    public static func point(
        location: CGPoint,
        in bounds: CGRect,
        force: Double,
        maxForce: Double = 1,
        t: Double? = nil
    ) -> InkPoint {
        let w = bounds.width == 0 ? 1 : bounds.width
        let h = bounds.height == 0 ? 1 : bounds.height
        let pressure = maxForce > 0 ? force / maxForce : 1
        return InkPoint(
            x: Double((location.x - bounds.minX) / w),
            y: Double((location.y - bounds.minY) / h),
            pressure: pressure,
            t: t
        ).normalized()
    }

    /// The ±700 ms palm-rejection grace (`postilla.md` §4): a touch landing
    /// within `graceMs` of pen activity is rejected as palm.
    public static func isPalm(
        touchTimestamp: TimeInterval,
        lastPenTimestamp: TimeInterval?,
        graceMs: Double = 700
    ) -> Bool {
        guard let pen = lastPenTimestamp else { return false }
        return abs(touchTimestamp - pen) <= graceMs / 1000.0
    }
}

#if canImport(PencilKit) && canImport(UIKit)
import PencilKit
import UIKit

/// PencilKit-backed capture surface (iOS). `PKDrawing` is used only as an
/// **input** signal — it is converted to raw `[x, y, pressure]` and **never
/// persisted** (canonical-ink rule). Thin by design; the heavy lifting is in
/// `InkSampler` + `FreehandRenderer`.
@MainActor
public final class InkCanvas {
    /// Convert a finished `PKStroke` to a normalized `InkStroke` in `bounds`.
    public static func strokeFrom(
        _ stroke: PKStroke,
        in bounds: CGRect,
        color: String,
        mode: InkMode = .draw
    ) -> InkStroke {
        let path = stroke.path
        var points: [InkPoint] = []
        points.reserveCapacity(path.count)
        for i in 0..<path.count {
            let sp = path[i]
            points.append(
                InkSampler.point(
                    location: sp.location,
                    in: bounds,
                    force: Double(sp.force),
                    maxForce: 1,
                    // `timeOffset` is seconds from the stroke start → ms (online HWR).
                    t: sp.timeOffset * 1000
                )
            )
        }
        let width = Double(stroke.ink.inkType == .pencil ? 4 : 6)
        return InkStroke(points: points, width: width, color: color, mode: mode)
    }

    /// Convert a whole `PKDrawing` to an `Ink` payload (input only).
    public static func inkFrom(_ drawing: PKDrawing, in bounds: CGRect, color: String) -> Ink {
        Ink(strokes: drawing.strokes.map { strokeFrom($0, in: bounds, color: color) })
    }
}
#endif
